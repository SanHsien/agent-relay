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

from agent_relay import dispatch, doorbell, envelope  # noqa: E402
from agent_relay.store import Store  # noqa: E402

# A stand-in worker: writes a file, commits it, prints a claude-style JSON result.
FAKE_WORKER = (
    "import json, subprocess, pathlib;"
    "pathlib.Path('proof.txt').write_text('done', encoding='utf-8');"
    "subprocess.run(['git', 'add', 'proof.txt'], check=True);"
    "subprocess.run(['git', 'commit', '-q', '-m', 'relay: fake'], check=True);"
    "print(json.dumps({'type': 'result', 'result': 'wrote proof.txt'}))"
)


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class DispatchBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        patch = mock.patch.dict(os.environ, {"AGENT_RELAY_HOME": str(self.dir / "home"),
                                             "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                                             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"})
        patch.start()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(patch.stop)
        self.repo = self.dir / "repos" / "demo"
        self.repo.mkdir(parents=True)
        git("init", "--quiet", cwd=self.repo)
        (self.repo / "README.md").write_text("demo\n", encoding="utf-8")
        git("add", ".", cwd=self.repo)
        git("commit", "--quiet", "-m", "init", cwd=self.repo)
        self.cfg = {"store": str(self.dir / "store"), "agent": "claude-code", "doorbells": [],
                    "dispatch": {"runtimes": ["claude"], "repo_roots": [str(self.dir / "repos")]}}
        self.store = Store(self.cfg["store"])

    def send_task(self, **spec):
        spec = {"runtime": "claude", "repo": str(self.repo), "timeout_minutes": 1, **spec}
        env = envelope.build(sender="commander", to="claude-code", subject="make proof", body="write proof.txt",
                             kind="task", dispatch=spec)
        self.store.deliver(env)
        return env

    def reply_for(self, env):
        replies = [e for _, e in self.store.list("commander") if e.get("reply_to") == env["id"]]
        self.assertEqual(len(replies), 1)
        return replies[0]


class ContractTests(unittest.TestCase):
    def test_spec_validation(self):
        good = {"runtime": "claude", "repo": "C:/x"}
        self.assertIs(dispatch.validate_spec(good), good)
        for bad in ({"runtime": "bash", "repo": "x"}, {"runtime": "claude"}, {"runtime": "claude", "repo": "x", "mode": "yolo"},
                    {"runtime": "claude", "repo": "x", "timeout_minutes": 999},
                    {"runtime": "claude", "repo": "x", "timeout_minutes": True},
                    {"runtime": "claude", "repo": "x", "model": "a" + chr(10) + "b"}, "not-a-dict"):
            with self.subTest(bad=bad), self.assertRaises(envelope.EnvelopeError):
                dispatch.validate_spec(bad)

    def test_only_tasks_carry_dispatch(self):
        with self.assertRaises(envelope.EnvelopeError):
            envelope.build(sender="a", to="b", subject="x", body="", kind="note",
                           dispatch={"runtime": "claude", "repo": "x"})

    def test_fallback_argv_is_conservative(self):
        with mock.patch.object(dispatch, "_cortex_launcher", return_value=None):
            argv, source = dispatch.build_argv({"runtime": "claude", "repo": "x"}, "P", "s", Path("L"), Path("W"), {})
            self.assertEqual(source, "built-in")
            self.assertIn("acceptEdits", argv)
            argv, _ = dispatch.build_argv({"runtime": "codex", "repo": "x", "mode": "plan"}, "P", "s", Path("L"), Path("W"), {})
            self.assertIn("read-only", argv)
            argv, _ = dispatch.build_argv({"runtime": "agy", "repo": "x"}, "P", "s", Path("L"), Path("W"), {})
            self.assertEqual(argv[-3:], ["--mode", "plan", "--sandbox"])
        for argv in (argv,):
            self.assertNotIn("--dangerously-skip-permissions", argv)

    @unittest.skipUnless(Path("C:/GitHub/agent-cortex/paulsha_cortex").is_dir(), "agent-cortex not checked out")
    def test_cortex_builder_used_when_available(self):
        cfg = {"dispatch": {"cortex_path": "C:/GitHub/agent-cortex"}}
        argv, source = dispatch.build_argv({"runtime": "claude", "repo": "x"}, "P", "s", Path("L"), Path("C:/W"), cfg)
        self.assertEqual(source, "agent-cortex")
        self.assertIn("acceptEdits", argv)
        self.assertNotIn("bypassPermissions", argv)
        self.assertNotIn("--remote-control", argv)


class RunTests(DispatchBase):
    def test_success_commits_in_worktree_and_replies(self):
        env = self.send_task()
        with mock.patch.object(dispatch, "build_argv", return_value=([sys.executable, "-c", FAKE_WORKER], "test")):
            report = dispatch.dispatch(env["id"], "claude-code", self.cfg, sync=False)
        self.assertEqual(report["status"], "ok", report)
        worktree = Path(report["worktree"])
        self.assertEqual((worktree / "proof.txt").read_text(encoding="utf-8"), "done")
        self.assertFalse((self.repo / "proof.txt").exists(), "main checkout must stay untouched")
        reply = self.reply_for(env)
        self.assertEqual(reply["kind"], "result")
        self.assertEqual(reply["thread"], env["thread"])
        self.assertIn("wrote proof.txt", reply["body"])
        self.assertIn("relay: fake", reply["body"])
        self.assertEqual([s for s, _ in self.store.list("claude-code", ("done",))], ["done"])
        excluded = (self.repo / ".git" / "info" / "exclude").read_text(encoding="utf-8")
        self.assertIn("/.relay-worktrees/", excluded)

    def test_refused_runtime_and_repo(self):
        env = self.send_task(runtime="codex")
        report = dispatch.dispatch(env["id"], "claude-code", self.cfg, sync=False)
        self.assertEqual(report["status"], "refused")
        self.assertIn("runtimes", self.reply_for(env)["body"])
        outside = self.dir / "elsewhere"
        outside.mkdir()
        env = self.send_task(repo=str(outside))
        self.assertEqual(dispatch.dispatch(env["id"], "claude-code", self.cfg, sync=False)["status"], "refused")
        self.assertFalse((outside / ".relay-worktrees").exists())

    def test_worker_failure_still_replies(self):
        env = self.send_task()
        with mock.patch.object(dispatch, "build_argv", return_value=([sys.executable, "-c", "raise SystemExit(3)"], "t")):
            report = dispatch.dispatch(env["id"], "claude-code", self.cfg, sync=False)
        self.assertEqual((report["status"], report["exit_code"]), ("failed", 3))
        self.assertEqual(self.reply_for(env)["priority"], "high")

    def test_missing_runtime_reported(self):
        env = self.send_task()
        with mock.patch.object(dispatch, "build_argv", return_value=(["definitely-not-installed-xyz"], "t")):
            report = dispatch.dispatch(env["id"], "claude-code", self.cfg, sync=False)
        self.assertTrue(report["status"].startswith("not-installed"))

    def test_timeout_kills_worker(self):
        log = self.dir / "out.log"
        status, code = dispatch.run([sys.executable, "-c", "import time; time.sleep(60)"], self.dir, log, 1)
        self.assertEqual((status, code), ("timeout", None))


class DoorbellTests(DispatchBase):
    def test_dispatch_bell_starts_detached_without_window(self):
        env = self.send_task()
        with mock.patch("subprocess.Popen") as popen:
            result = doorbell.ring(env, [{"type": "dispatch", "window": False}])["0:dispatch"]
        self.assertEqual(result, "ok: detached process")
        argv = popen.call_args.args[0]
        self.assertEqual(argv[-4:], ["dispatch", env["id"], "--agent", "claude-code"])

    def test_dispatch_bell_ignores_plain_mail(self):
        env = envelope.build(sender="a", to="claude-code", subject="hi", body="", kind="task")
        with mock.patch("subprocess.Popen") as popen:
            self.assertTrue(doorbell.ring(env, [{"type": "dispatch"}])["0:dispatch"].startswith("skipped"))
        popen.assert_not_called()


class FinalTextTests(unittest.TestCase):
    def test_reads_claude_and_codex_shapes(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "o.log"
            log.write_text("noise\n" + json.dumps({"type": "result", "result": "claude says"}) + "\n", encoding="utf-8")
            self.assertEqual(dispatch.final_text(log), "claude says")
            log.write_text(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "codex says"}}) + "\n",
                           encoding="utf-8")
            self.assertEqual(dispatch.final_text(log), "codex says")


if __name__ == "__main__":
    unittest.main()
