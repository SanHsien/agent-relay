"""T-20261021-05: the receiver pushes relay/<id>; the commander fetches, re-verifies and merges."""

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

# Never touch the real ~/.agent-relay: every test run gets its own home.
os.environ["AGENT_RELAY_HOME"] = tempfile.mkdtemp(prefix="agent-relay-test-home-")

from agent_relay import accept, dispatch, envelope  # noqa: E402
from agent_relay.store import Store  # noqa: E402

GOOD_WORKER = (
    "import pathlib;"
    "pathlib.Path('calc.py').write_text('def add(a, b):\\n    return a + b\\n', encoding='utf-8');"
    "pathlib.Path('test_calc.py').write_text("
    "'import unittest\\nfrom calc import add\\n\\n\\nclass T(unittest.TestCase):\\n'"
    "'    def test_add(self):\\n        self.assertEqual(add(1, 2), 3)\\n', encoding='utf-8')"
)
BAD_WORKER = GOOD_WORKER.replace("return a + b", "return a - b")


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class AcceptBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        patch = mock.patch.dict(os.environ, {"AGENT_RELAY_HOME": str(self.dir / "home"),
                                             "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                                             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"})
        patch.start()
        self.addCleanup(patch.stop)
        seed = self.dir / "seed"
        seed.mkdir()
        git("init", "--quiet", "-b", "main", cwd=seed)
        (seed / "README.md").write_text("demo\n", encoding="utf-8")
        git("add", ".", cwd=seed)
        git("commit", "--quiet", "-m", "init", cwd=seed)
        self.remote = self.dir / "remote.git"
        git("clone", "--quiet", "--bare", str(seed), str(self.remote), cwd=self.dir)
        self.receiver = self.dir / "repos" / "demo"  # "machine B"
        self.commander = self.dir / "commander" / "demo"  # "machine A"
        for clone in (self.receiver, self.commander):
            clone.parent.mkdir(parents=True, exist_ok=True)
            git("clone", "--quiet", str(self.remote), str(clone), cwd=self.dir)
        self.base = git("rev-parse", "HEAD", cwd=self.commander)
        store = str(self.dir / "store")
        self.receiver_cfg = {"store": store, "agent": "claude-code", "doorbells": [],
                             "dispatch": {"runtimes": ["claude"], "repo_roots": [str(self.dir / "repos")],
                                          "senders": ["commander"], "require_signature": False, "context": False,
                                          "push_branch": True, "push_remotes": [str(self.remote)]}}
        self.commander_cfg = {"store": store, "agent": "commander", "doorbells": [],
                              "accept": {"require_signature": False,
                                         "verify": {"command": [sys.executable, "-m", "unittest"], "timeout_minutes": 2}}}
        self.store = Store(store)

    def dispatch_task(self, worker=GOOD_WORKER):
        env = envelope.build(sender="commander", to="claude-code", subject="add add()", body="add add()",
                             kind="task", dispatch={"runtime": "claude", "repo": str(self.receiver),
                                                    "timeout_minutes": 1, "base": self.base})
        self.store.deliver(env)
        with mock.patch.object(dispatch, "build_argv", return_value=(["claude", "-c", worker], "test", None)), \
                mock.patch.object(dispatch, "resolve_executable", return_value=sys.executable):
            report = dispatch.dispatch(env["id"], "claude-code", self.receiver_cfg, sync=False)
        reply = next(e for _, e in self.store.list("commander") if e.get("reply_to") == env["id"])
        return report, reply

    def accept(self, reply, **kw):
        return accept.accept(self.commander_cfg, "commander", reply["id"], self.commander, sync=False, **kw)


class PushTests(AcceptBase):
    def test_push_only_to_allowed_remotes(self):
        self.assertTrue(dispatch.push_allowed("https://github.com/SanHsien/agent-relay.git", {}))
        self.assertTrue(dispatch.push_allowed("git@github.com:SanHsien/x.git", {}))
        self.assertTrue(dispatch.push_allowed("https://github.com/SanHsien/agent-relay", {}))
        self.assertTrue(dispatch.push_allowed("ssh://git@github.com/SanHsien/a.b_c-d.git", {}))
        for url in ("https://github.com/someone/x.git", "https://github.com/SanHsienX/x.git",
                    "https://evil.example/https://github.com/SanHsien/", str(self.remote),
                    "https://github.com/SanHsien/../other/x.git", "https://github.com/SanHsien/..",
                    "https://github.com/SanHsien/.", "https://github.com/SanHsien/x/../../other/y",
                    "https://github.com/SanHsien/%2e%2e/other/x", "https://github.com/SanHsien/",
                    "https://user@github.com/SanHsien/x.git", "https://github.com/SanHsien/x.git?y"):
            with self.subTest(url=url):
                self.assertFalse(dispatch.push_allowed(url, {}))
        self.assertTrue(dispatch.push_allowed(str(self.remote), {"push_remotes": [str(self.remote)]}))

    def test_ok_run_is_pushed_and_named_in_the_reply(self):
        report, reply = self.dispatch_task()
        self.assertEqual(report["status"], "ok", report)
        pushed = reply["result"]["pushed"]
        self.assertEqual(pushed["ref"], f"refs/heads/relay/{reply['reply_to']}")
        self.assertEqual(git("rev-parse", pushed["ref"], cwd=self.remote), reply["result"]["commit"])

    def test_not_pushed_when_remote_not_allowed_or_run_failed(self):
        self.receiver_cfg["dispatch"]["push_remotes"] = []
        report, reply = self.dispatch_task()
        self.assertNotIn("pushed", reply["result"])
        self.assertIn("not pushing", report["push_error"])
        self.receiver_cfg["dispatch"]["push_remotes"] = [str(self.remote)]
        # A second push url that is not allowed: git would push there too.
        git("remote", "set-url", "--add", "--push", "origin", str(self.remote), cwd=self.receiver)
        git("remote", "set-url", "--add", "--push", "origin", "https://github.com/someone/x.git", cwd=self.receiver)
        report, reply = self.dispatch_task()
        self.assertIn("someone", report["push_error"])
        git("remote", "set-url", "--delete", "--push", "origin", "someone", cwd=self.receiver)
        report, reply = self.dispatch_task(worker=GOOD_WORKER + ";raise SystemExit(2)")
        self.assertEqual(report["status"], "failed")
        self.assertNotIn("pushed", reply["result"])
        self.assertEqual(git("branch", "--list", "relay/*", cwd=self.remote), "")


class AcceptTests(AcceptBase):
    def test_accepts_and_merges_after_verifying_here(self):
        _, reply = self.dispatch_task()
        before = git("rev-parse", "HEAD", cwd=self.commander)
        report = self.accept(reply)
        self.assertEqual(report["status"], "accepted", report)
        self.assertEqual(report["verify"]["exit"], 0)
        self.assertEqual(git("rev-parse", "HEAD^1", cwd=self.commander), before)  # --no-ff: a real merge commit
        self.assertEqual(git("rev-parse", "HEAD^2", cwd=self.commander), reply["result"]["commit"])
        self.assertTrue((self.commander / "calc.py").is_file())
        answer = next(e for _, e in self.store.list("claude-code") if e.get("reply_to") == reply["id"])
        self.assertTrue(answer["subject"].startswith("accepted:"))
        audits = [json.loads(p.read_text(encoding="utf-8")) for p in (self.dir / "store" / "audit").rglob("*accept*.json")]
        self.assertEqual([a["status"] for a in audits], ["accepted"])
        self.assertEqual([s for s, e in self.store.list("commander", ("done",)) if e["id"] == reply["id"]], ["done"])

    def test_push_after_merge_only_to_allowed_remotes(self):
        _, reply = self.dispatch_task()
        report = self.accept(reply, push=True)  # origin is a local path not in accept.push_remotes
        self.assertEqual(report["status"], "accepted", report)
        self.assertNotIn("pushed", report)
        self.assertTrue(any("not pushed" in n for n in report["notes"]), report["notes"])
        self.assertNotEqual(git("rev-parse", "main", cwd=self.remote), git("rev-parse", "HEAD", cwd=self.commander))
        self.commander_cfg["accept"].update(push=True, push_remotes=[str(self.remote)])
        _, reply = self.dispatch_task()
        report = self.accept(reply)
        self.assertEqual(report["status"], "accepted", report)
        self.assertEqual(report["pushed"], "origin/main")
        self.assertEqual(git("rev-parse", "main", cwd=self.remote), git("rev-parse", "HEAD", cwd=self.commander))

    def test_rejects_when_verification_fails_here(self):
        _, reply = self.dispatch_task(worker=BAD_WORKER)  # the receiver had no gates, so it reported ok
        before = git("rev-parse", "HEAD", cwd=self.commander)
        report = self.accept(reply)
        self.assertEqual(report["status"], "rejected")
        self.assertIn("verification failed", report["reason"])
        self.assertNotEqual(report["verify"]["exit"], 0)
        self.assertEqual(git("rev-parse", "HEAD", cwd=self.commander), before)
        answer = next(e for _, e in self.store.list("claude-code") if e.get("reply_to") == reply["id"])
        self.assertTrue(answer["subject"].startswith("rejected:"))

    def test_rejects_a_branch_moved_after_the_reply(self):
        _, reply = self.dispatch_task()
        other = self.dir / "intruder"
        git("clone", "--quiet", "-b", reply["result"]["branch"], str(self.remote), str(other), cwd=self.dir)
        (other / "extra.py").write_text("print('not verified by the receiver')\n", encoding="utf-8")
        git("add", ".", cwd=other)
        git("commit", "--quiet", "-m", "sneaky", cwd=other)
        git("push", "--quiet", "origin", "HEAD", cwd=other)
        before = git("rev-parse", "HEAD", cwd=self.commander)
        report = self.accept(reply)
        self.assertEqual(report["status"], "rejected")
        self.assertIn("the reply names", report["reason"])
        self.assertNotIn("verify", report)  # never ran the moved branch's code
        self.assertEqual(git("rev-parse", "HEAD", cwd=self.commander), before)

    def test_local_problems_raise_and_record_nothing(self):
        _, reply = self.dispatch_task()
        (self.commander / "README.md").write_text("local edit\n", encoding="utf-8")
        with self.assertRaisesRegex(accept.AcceptError, "uncommitted changes"):
            self.accept(reply)
        git("checkout", "--", "README.md", cwd=self.commander)
        with self.assertRaisesRegex(accept.AcceptError, "not the checked-out branch"):
            self.accept(reply, into="release")
        verify = self.commander_cfg["accept"].pop("verify")
        with self.assertRaisesRegex(accept.AcceptError, "no accept.verify.command"):
            self.accept(reply)
        # Nothing told the receiver, the reply is still open, and it can be accepted once fixed.
        self.assertEqual([e for _, e in self.store.list("claude-code") if e.get("reply_to") == reply["id"]], [])
        self.commander_cfg["accept"]["verify"] = verify
        self.assertEqual(self.accept(reply)["status"], "accepted")
        with self.assertRaisesRegex(accept.AcceptError, "already merged"):
            self.accept(reply)

    def test_verifies_the_merge_with_a_moved_target_branch(self):
        _, reply = self.dispatch_task()
        # The commander's main moved on after the task was sent: a test that the merge must also pass.
        (self.commander / "test_later.py").write_text(
            "import unittest\nfrom calc import add\n\n\nclass L(unittest.TestCase):\n"
            "    def test_negative(self):\n        self.assertEqual(add(-1, -1), -2)\n", encoding="utf-8")
        git("add", ".", cwd=self.commander)
        git("commit", "--quiet", "-m", "later test", cwd=self.commander)
        head = git("rev-parse", "HEAD", cwd=self.commander)
        report = self.accept(reply)
        self.assertEqual(report["status"], "accepted", report)
        self.assertIn("Ran 2 tests", report["verify"]["tail"])  # the merge was tested, not the branch alone
        self.assertEqual(git("rev-parse", "HEAD", cwd=self.commander), report["merge"])
        self.assertEqual(git("rev-parse", "HEAD^1", cwd=self.commander), head)

    def test_rejects_a_reply_naming_another_tasks_branch(self):
        _, first = self.dispatch_task()
        _, second = self.dispatch_task()
        forged = {**second, "result": first["result"]}  # a consistent result, but for another task
        self.store.ack("commander", second["id"])
        self.store.deliver({**forged, "id": second["id"] + "x"})
        report = accept.accept(self.commander_cfg, "commander", second["id"] + "x", self.commander, sync=False)
        self.assertEqual(report["status"], "rejected")
        self.assertIn("names branch", report["reason"])

    def test_unsigned_reply_rejected_by_default(self):
        _, reply = self.dispatch_task()
        self.commander_cfg["accept"].pop("require_signature")
        report = self.accept(reply)
        self.assertEqual(report["status"], "rejected")
        self.assertIn("signature", report["reason"])

    def test_reply_shape_checks(self):
        result = {"status": "ok", "branch": "relay/x1", "commit": "a" * 40, "base": "b" * 40,
                  "pushed": {"ref": "refs/heads/relay/x1", "commit": "a" * 40}}
        env = {"from": "claude-code", "result": result}
        cfg = {"accept": {"require_signature": False}}
        self.assertEqual(accept.check_reply(env, cfg), result)
        for change in ({"status": "failed"}, {"commit": "a" * 12}, {"pushed": None},
                       {"pushed": {"ref": "refs/heads/main", "commit": "a" * 40}},
                       {"branch": "main", "pushed": {"ref": "refs/heads/main", "commit": "a" * 40}}):
            with self.subTest(change=change), self.assertRaises(accept.AcceptError):
                accept.check_reply({**env, "result": {**result, **change}}, cfg)


if __name__ == "__main__":
    unittest.main()
