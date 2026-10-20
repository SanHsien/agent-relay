import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent_relay import dispatch, envelope, gates  # noqa: E402
from agent_relay.store import Store  # noqa: E402

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.com", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e.com"}
UNITTEST = [sys.executable, "-m", "unittest", "discover", "-s", ".", "-p", "test_*.py"]


class StubTests(unittest.TestCase):
    def check(self, source):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "m.py").write_text(source, encoding="utf-8")
            return gates.find_stubs(Path(tmp), ["m.py"])

    def test_placeholders_found(self):
        for body in ("pass", "...", "raise NotImplementedError", "raise NotImplementedError('todo')", '"""doc"""'):
            with self.subTest(body=body):
                self.assertEqual(len(self.check(f"def f(a):\n    {body}\n")), 1)

    def test_real_and_allowed_bodies(self):
        src = ("import abc\nfrom typing import Protocol, overload\n"
               "def f(a):\n    return a\n"
               "class P(Protocol):\n    def g(self): ...\n"
               "class A(abc.ABC):\n    @abc.abstractmethod\n    def h(self): pass\n"
               "@overload\ndef k(x: int) -> int: ...\n")
        self.assertEqual(self.check(src), [])

    def test_test_files_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "test_m.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
            self.assertEqual(gates.find_stubs(Path(tmp), ["test_m.py"]), [])


class CommandTests(unittest.TestCase):
    def test_loop_policy_quick_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "loop-policy.toml").write_text('[verification]\nquick_gate = "python -m unittest"\n', encoding="utf-8")
            self.assertEqual(gates.verify_command(Path(tmp), {"command": "loop-policy"}), ["python", "-m", "unittest"])
            self.assertIsNone(gates.verify_command(Path(tmp) / "missing", {"command": "loop-policy"}))
        self.assertEqual(gates.verify_command(Path("."), {"command": ["a", "b"]}), ["a", "b"])
        self.assertIsNone(gates.verify_command(Path("."), {}))

    def test_test_file_patterns(self):
        for yes in ("tests/x.py", "test_calc.py", "pkg/test_a.py", "a_test.py", "features/x.feature", "src/a.spec.ts"):
            self.assertTrue(gates.is_test_file(yes), yes)
        for no in ("calc.py", "contest.py", "docs/testing.md"):
            self.assertFalse(gates.is_test_file(no), no)


class DispatchGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = Path(self.tmp.name)
        patch = mock.patch.dict(os.environ, {"AGENT_RELAY_HOME": str(d / "home"), **GIT_ENV})
        patch.start()
        self.addCleanup(patch.stop)
        self.repo = d / "repos" / "calc"
        self.repo.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        (self.repo / "calc.py").write_text("def sub(a, b):\n    return a - b\n", encoding="utf-8")
        (self.repo / "test_calc.py").write_text(
            "import unittest\nfrom calc import sub\n\nclass T(unittest.TestCase):\n"
            "    def test_sub(self):\n        self.assertEqual(sub(3, 1), 2)\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "i"], check=True)
        self.cfg = {"store": str(d / "store"), "agent": "cc", "doorbells": [],
                    "dispatch": {"runtimes": ["claude"], "repo_roots": [str(d / "repos")], "context": False,
                                 "verify": {"command": UNITTEST}}}
        self.store = Store(self.cfg["store"])

    def go(self, worker_code, tdd=False):
        spec = {"runtime": "claude", "repo": str(self.repo), "timeout_minutes": 1}
        if tdd:
            spec["tdd"] = True
        env = envelope.build(sender="commander", to="cc", subject="t", body="b", kind="task", dispatch=spec)
        self.store.deliver(env)
        with mock.patch.object(dispatch, "build_argv", return_value=(["x", "-c", worker_code], "t", None)), \
                mock.patch.object(dispatch, "resolve_executable", return_value=sys.executable):
            report = dispatch.dispatch(env["id"], "cc", self.cfg, sync=False)
        reply = next(e for _, e in self.store.list("commander") if e.get("reply_to") == env["id"])
        return report, reply["body"]

    ADD_MUL = "open('calc.py','a').write('\\n\\ndef mul(a, b):\\n    return a * b\\n')"
    ADD_TEST = ("open('test_calc.py','a').write('\\n\\nfrom calc import mul\\n\\nclass M(unittest.TestCase):\\n"
                "    def test_mul(self):\\n        self.assertEqual(mul(2, 3), 6)\\n')")

    def test_tdd_red_then_green_commits(self):
        report, body = self.go(f"{self.ADD_MUL};{self.ADD_TEST}", tdd=True)
        self.assertEqual(report["status"], "ok", body)
        self.assertTrue(report["commit"].startswith("committed:"))
        self.assertEqual([r["phase"] for r in report["gate"]["logs"]], ["red", "green"])
        self.assertNotEqual(report["gate"]["logs"][0]["exit"], 0)
        self.assertIn("TDD 紅燈", body)
        self.assertIn("sha256：", body)
        green_log = Path(os.environ["AGENT_RELAY_HOME"]) / "runs"
        self.assertTrue(any(p.name == "verify-green.log" for p in green_log.rglob("*.log")))

    def test_tdd_without_new_test_not_proven(self):
        report, body = self.go(self.ADD_MUL, tdd=True)
        self.assertEqual(report["status"], "tdd-not-proven")
        self.assertIn("沒有新增或修改任何測試檔", body)
        self.assertIn("沒有 commit", report["commit"])

    def test_test_that_already_passes_is_not_a_red(self):
        passing = ("open('test_calc.py','a').write('\\n\\nclass N(unittest.TestCase):\\n"
                   "    def test_trivial(self):\\n        self.assertTrue(True)\\n')")
        report, _ = self.go(f"{self.ADD_MUL};{passing}", tdd=True)
        self.assertEqual(report["status"], "tdd-not-proven")

    def test_stub_rejected(self):
        report, body = self.go("open('calc.py','a').write('\\n\\ndef mul(a, b):\\n    pass\\n')")
        self.assertEqual(report["status"], "stub-rejected")
        self.assertIn("calc.py:", body)
        self.assertIn("mul()", body)

    def test_green_failure_blocks_commit(self):
        report, body = self.go("open('calc.py','w').write('def sub(a, b):\\n    return a + b\\n')")
        self.assertEqual(report["status"], "verification-failed")
        self.assertIn("沒有 commit", report["commit"])
        self.assertIn("FAILED", body)  # the real unittest output is in the reply

    def test_no_gate_but_required_is_unverified(self):
        self.cfg["dispatch"].pop("verify")
        self.cfg["dispatch"]["require_verification"] = True
        report, _ = self.go(self.ADD_MUL)
        self.assertEqual(report["status"], "unverified")
        self.cfg["dispatch"].pop("require_verification")
        report, _ = self.go(self.ADD_MUL)
        self.assertEqual(report["status"], "ok")  # gates are opt-in

    def test_mail_cannot_supply_a_command(self):
        with self.assertRaises(envelope.EnvelopeError):
            envelope.build(sender="a", to="b", subject="x", body="", kind="task",
                           dispatch={"runtime": "claude", "repo": "x", "tdd": "yes"})
        spec = dispatch.validate_spec({"runtime": "claude", "repo": "x", "verify": {"command": ["rm", "-rf", "/"]}})
        self.assertNotIn("command", (self.cfg["dispatch"].get("verify") or {}).get("command", []))
        self.assertIn("verify", spec)  # unknown keys are kept as data, but only config.dispatch.verify is ever run

    def test_verify_audit_event(self):
        self.go(f"{self.ADD_MUL};{self.ADD_TEST}", tdd=True)
        events = [json.loads(p.read_text(encoding="utf-8")) for p in Path(self.cfg["store"], "audit").rglob("*verify*.json")]
        self.assertEqual(events[0]["status"], "ok")
        self.assertEqual([r["phase"] for r in events[0]["runs"]], ["red", "green"])


if __name__ == "__main__":
    unittest.main()
