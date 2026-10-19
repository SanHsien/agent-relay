import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent_relay import cli, dispatch, doorbell, envelope  # noqa: E402
from agent_relay.store import Store, StoreError  # noqa: E402

NL = chr(10)

# A stand-in worker: reads the prompt from stdin, writes a file, commits it,
# prints a claude-style JSON result. Never a real claude / codex / agy.
FAKE_WORKER = (
    "import json, subprocess, pathlib, sys;"
    "prompt = sys.stdin.read();"
    "pathlib.Path('proof.txt').write_text(prompt, encoding='utf-8');"
    "subprocess.run(['git', 'add', 'proof.txt'], check=True);"
    "subprocess.run(['git', 'commit', '-q', '-m', 'relay: fake'], check=True);"
    "print(json.dumps({'type': 'result', 'result': 'wrote proof.txt'}))"
)


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def fake(code: str, stdin: bool = True):
    """Patch the builder and the executable lookup so no real agent CLI can start."""
    return (
        mock.patch.object(dispatch, "build_argv", return_value=(["claude", "-c", code], "test", "PROMPT" if stdin else None)),
        mock.patch.object(dispatch, "resolve_executable", return_value=sys.executable),
    )


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

    def send_task(self, sender="commander", **spec):
        spec = {"runtime": "claude", "repo": str(self.repo), "timeout_minutes": 1, **spec}
        env = envelope.build(sender=sender, to="claude-code", subject="make proof", body="write proof.txt",
                             kind="task", dispatch=spec)
        self.store.deliver(env)
        return env

    def replies_for(self, env):
        return [e for _, e in self.store.list(env["from"]) if e.get("reply_to") == env["id"]]

    def reply_for(self, env):
        replies = self.replies_for(env)
        self.assertEqual(len(replies), 1)
        return replies[0]

    def run_fake(self, env, code=FAKE_WORKER, **kw):
        builder, resolver = fake(code)
        with builder, resolver:
            return dispatch.dispatch(env["id"], "claude-code", self.cfg, sync=False, **kw)


class ContractTests(unittest.TestCase):
    def test_spec_validation(self):
        good = {"runtime": "claude", "repo": "C:/x", "model": "claude-haiku-5-5", "effort": "low"}
        self.assertIs(dispatch.validate_spec(good), good)
        for bad in ({"runtime": "bash", "repo": "x"}, {"runtime": "claude"}, {"runtime": "claude", "repo": "x", "mode": "yolo"},
                    {"runtime": "claude", "repo": "x", "timeout_minutes": 999},
                    {"runtime": "claude", "repo": "x", "timeout_minutes": True},
                    {"runtime": "claude", "repo": "x", "model": "a" + NL + "b"},
                    {"runtime": "claude", "repo": "x", "model": "--dangerously-skip-permissions"},
                    {"runtime": "codex", "repo": "x", "effort": "bogus"}, "not-a-dict"):
            with self.subTest(bad=bad), self.assertRaises(envelope.EnvelopeError):
                dispatch.validate_spec(bad)

    def test_only_tasks_carry_dispatch(self):
        with self.assertRaises(envelope.EnvelopeError):
            envelope.build(sender="a", to="b", subject="x", body="", kind="note",
                           dispatch={"runtime": "claude", "repo": "x"})


class ArgvTests(DispatchBase):
    def worktree(self):
        return dispatch.make_worktree(self.repo, "w1")[0]

    def test_fallback_argv_is_conservative_and_uses_stdin(self):
        wt = self.worktree()
        with mock.patch.object(dispatch, "_cortex_launcher", return_value=None):
            argv, source, stdin = dispatch.build_argv({"runtime": "claude", "repo": "x"}, "P" + NL + "line2", "s", Path("L"), wt, {})
            self.assertEqual(source, "built-in")
            self.assertIn("acceptEdits", argv)
            self.assertNotIn("P" + NL + "line2", argv)
            self.assertEqual(stdin, "P" + NL + "line2")
            self.assertIn("--allowedTools", argv)
            argv, _, stdin = dispatch.build_argv({"runtime": "codex", "repo": "x", "mode": "plan"}, "P", "s", Path("L"), wt, {})
            self.assertIn("read-only", argv)
            self.assertEqual(stdin, "P")
            argv, _, _ = dispatch.build_argv({"runtime": "codex", "repo": "x"}, "P", "s", Path("L"), wt, {})
            self.assertIn("workspace-write", argv)
            self.assertIn("--add-dir", argv)  # git dirs outside the worktree, so the commit works
            argv, _, stdin = dispatch.build_argv({"runtime": "agy", "repo": "x"}, "P", "s", Path("L"), wt, {})
            self.assertEqual(argv[-3:], ["--mode", "plan", "--sandbox"])
            self.assertIsNone(stdin)
        for flag in ("--dangerously-skip-permissions", "bypassPermissions", "--dangerously-bypass-approvals-and-sandbox"):
            self.assertNotIn(flag, argv)

    @unittest.skipUnless(Path("C:/GitHub/agent-cortex/paulsha_cortex").is_dir(), "agent-cortex not checked out")
    def test_cortex_builder_used_when_available(self):
        wt = self.worktree()
        cfg = {"dispatch": {"cortex_path": "C:/GitHub/agent-cortex"}}
        argv, source, stdin = dispatch.build_argv({"runtime": "claude", "repo": "x"}, "P", "s", Path("L"), wt, cfg)
        self.assertEqual(source, "agent-cortex")
        self.assertIn("acceptEdits", argv)
        self.assertNotIn("bypassPermissions", argv)
        self.assertNotIn("--remote-control", argv)
        self.assertNotIn("P", argv)
        self.assertEqual(stdin, "P")
        self.assertIn("Bash(git commit *)", argv)


class ExecutableTests(DispatchBase):
    def test_batch_shim_refused(self):
        shim = self.dir / "claude.cmd"
        shim.write_text("@echo off", encoding="utf-8")
        cfg = {"dispatch": {"executables": {"claude": str(shim)}}}
        with self.assertRaises(dispatch.DispatchError) as ctx:
            dispatch.resolve_executable("claude", cfg)
        self.assertIn("batch shim", str(ctx.exception))

    def test_configured_exe_used(self):
        cfg = {"dispatch": {"executables": {"claude": sys.executable}}}
        self.assertEqual(dispatch.resolve_executable("claude", cfg), sys.executable)

    def test_missing_exe(self):
        cfg = {"dispatch": {"executables": {"claude": str(self.dir / "nope.exe")}}}
        with self.assertRaises(dispatch.DispatchError):
            dispatch.resolve_executable("claude", cfg)


class RunTests(DispatchBase):
    def test_success_commits_in_worktree_and_replies(self):
        env = self.send_task()
        report = self.run_fake(env)
        self.assertEqual(report["status"], "ok", report)
        worktree = Path(report["worktree"])
        self.assertEqual((worktree / "proof.txt").read_text(encoding="utf-8"), "PROMPT")  # prompt came on stdin
        self.assertFalse((self.repo / "proof.txt").exists(), "main checkout must stay untouched")
        reply = self.reply_for(env)
        self.assertEqual((reply["kind"], reply["thread"]), ("result", env["thread"]))
        self.assertIn("wrote proof.txt", reply["body"])
        self.assertIn("relay: fake", reply["body"])
        self.assertEqual([s for s, _ in self.store.list("claude-code", ("done",))], ["done"])
        self.assertIn("/.relay-worktrees/", (self.repo / ".git" / "info" / "exclude").read_text(encoding="utf-8"))

    def test_refused_runtime_repo_and_sender(self):
        env = self.send_task(runtime="codex")
        self.assertEqual(dispatch.dispatch(env["id"], "claude-code", self.cfg, sync=False)["status"], "refused")
        self.assertIn("runtimes", self.reply_for(env)["body"])
        outside = self.dir / "elsewhere"
        outside.mkdir()
        env = self.send_task(repo=str(outside))
        self.assertEqual(dispatch.dispatch(env["id"], "claude-code", self.cfg, sync=False)["status"], "refused")
        self.assertFalse((outside / ".relay-worktrees").exists())
        self.cfg["dispatch"]["senders"] = ["commander"]
        env = self.send_task(sender="stranger")
        self.assertEqual(dispatch.dispatch(env["id"], "claude-code", self.cfg, sync=False)["status"], "refused")
        self.assertIn("senders", self.reply_for(env)["body"])

    def test_worker_failure_still_replies(self):
        env = self.send_task()
        report = self.run_fake(env, "raise SystemExit(3)")
        self.assertEqual((report["status"], report["exit_code"]), ("failed", 3))
        self.assertEqual(self.reply_for(env)["priority"], "high")

    def test_unexpected_exception_still_replies_and_acks(self):
        env = self.send_task()
        with mock.patch.object(dispatch, "build_argv", side_effect=ValueError("cortex said no")):
            report = dispatch.dispatch(env["id"], "claude-code", self.cfg, sync=False)
        self.assertEqual(report["status"], "error")
        self.assertIn("cortex said no", self.reply_for(env)["body"])
        self.assertEqual(self.store.list("claude-code"), [])

    def test_missing_runtime_refused_with_reply(self):
        env = self.send_task()
        with mock.patch.object(dispatch, "resolve_executable", side_effect=dispatch.DispatchError("not installed")):
            report = dispatch.dispatch(env["id"], "claude-code", self.cfg, sync=False)
        self.assertEqual(report["status"], "refused")
        self.assertEqual(len(self.replies_for(env)), 1)

    def test_done_task_not_rerun_without_force(self):
        env = self.send_task()
        self.run_fake(env)
        with self.assertRaises(dispatch.DispatchError):
            self.run_fake(env)
        self.assertEqual(len(self.replies_for(env)), 1)
        report = self.run_fake(env, "print('again')", force=True)
        self.assertEqual(report["status"], "ok")
        self.assertIn("relay: fake", self.replies_for(env)[-1]["body"])  # base is the branch's merge-base

    def test_stray_folder_is_not_used_as_worktree(self):
        env = self.send_task()
        (self.repo / ".relay-worktrees" / env["id"]).mkdir(parents=True)
        report = self.run_fake(env)
        self.assertEqual(report["status"], "refused")
        self.assertIn("is not the", self.reply_for(env)["body"])

    def test_timeout_kills_worker(self):
        status, code = dispatch.run([sys.executable, "-c", "import time; time.sleep(60)"], self.dir, self.dir / "o.log", 1)
        self.assertEqual((status, code), ("timeout", None))

    def test_secrets_redacted_in_reply(self):
        env = self.send_task()
        leak = "print('token ghp_' + 'a' * 36 + ' and sk-' + 'b' * 30)"
        self.run_fake(env, leak)
        body = self.reply_for(env)["body"]
        self.assertNotIn("ghp_" + "a" * 36, body)
        self.assertIn("[REDACTED]", body)


class LeaseTests(DispatchBase):
    def test_interrupted_run_is_answered_once(self):
        env = self.send_task()
        self.store.read("claude-code", env["id"])  # the run had moved it to cur/
        dispatch.write_state(env["id"], phase="running", pid=1, started=time.time() - 9999, deadline=time.time() - 1)
        self.assertEqual(dispatch.reap_interrupted("claude-code", self.cfg), [env["id"]])
        self.assertEqual(self.reply_for(env)["subject"].split(":")[0], "interrupted")
        self.assertEqual(dispatch.reap_interrupted("claude-code", self.cfg), [])

    def test_live_lease_left_alone(self):
        env = self.send_task()
        self.store.read("claude-code", env["id"])
        dispatch.write_state(env["id"], phase="running", pid=1, started=time.time(), deadline=time.time() + 600)
        self.assertEqual(dispatch.reap_interrupted("claude-code", self.cfg), [])

    def test_watch_reaps(self):
        env = self.send_task()
        self.store.read("claude-code", env["id"])
        dispatch.write_state(env["id"], phase="running", pid=1, started=0, deadline=1)
        cli.watch_once("claude-code", self.cfg, sync=False)
        self.assertEqual(len(self.replies_for(env)), 1)


class StoreReadTests(DispatchBase):
    def test_read_checks_name_and_mailbox(self):
        env = envelope.build(sender="a", to="hermes", subject="x", body="")
        box = self.store.box("claude-code", "new")
        box.mkdir(parents=True)
        (box / "fake-id.json").write_text(json.dumps(env), encoding="utf-8")
        with self.assertRaises(StoreError):
            self.store.read("claude-code", "fake-id")


class DoorbellTests(DispatchBase):
    def test_dispatch_bell_starts_detached_with_home_argument(self):
        env = self.send_task()
        with mock.patch("subprocess.Popen") as popen:
            result = doorbell.ring(env, [{"type": "dispatch", "window": False}])["0:dispatch"]
        self.assertEqual(result, "ok: detached process")
        argv = popen.call_args.args[0]
        self.assertEqual(argv[3:7], ["dispatch", env["id"], "--agent", "claude-code"])
        self.assertEqual(argv[argv.index("--home") + 1], str(self.dir / "home"))

    def test_dispatch_bell_ignores_plain_mail(self):
        env = envelope.build(sender="a", to="claude-code", subject="hi", body="", kind="task")
        with mock.patch("subprocess.Popen") as popen:
            self.assertTrue(doorbell.ring(env, [{"type": "dispatch"}])["0:dispatch"].startswith("skipped"))
        popen.assert_not_called()

    def test_console_python_for_visible_tab(self):
        fake_dir = self.dir / "py"
        fake_dir.mkdir()
        (fake_dir / "python.exe").write_text("", encoding="utf-8")
        with mock.patch.object(sys, "executable", str(fake_dir / "pythonw.exe")):
            self.assertEqual(dispatch._console_python(), str(fake_dir / "python.exe"))


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
