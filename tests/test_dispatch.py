import json
import shutil
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

# Never touch the real ~/.agent-relay: every test run gets its own home.
os.environ["AGENT_RELAY_HOME"] = tempfile.mkdtemp(prefix="agent-relay-test-home-")

from agent_relay import cli, dispatch, doorbell, envelope, signing  # noqa: E402
from agent_relay.store import Store, StoreError  # noqa: E402

NL = chr(10)

# A stand-in worker: reads the prompt from stdin, writes a file, commits it,
# prints a claude-style JSON result. Never a real claude / codex / agy.
FAKE_WORKER = (
    "import json, subprocess, pathlib, sys;"
    "prompt = sys.stdin.read();"
    "pathlib.Path('proof.txt').write_text(prompt, encoding='utf-8');"
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
                    "dispatch": {"runtimes": ["claude"], "repo_roots": [str(self.dir / "repos")],
                                 "senders": ["commander"], "require_signature": False}}
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
            self.assertNotIn("--allowedTools", argv)  # no shell for a claude worker by default
            argv, _, stdin = dispatch.build_argv({"runtime": "codex", "repo": "x", "mode": "plan"}, "P", "s", Path("L"), wt, {})
            self.assertIn("read-only", argv)
            self.assertEqual(stdin, "P")
            argv, _, _ = dispatch.build_argv({"runtime": "codex", "repo": "x"}, "P", "s", Path("L"), wt, {})
            self.assertIn("workspace-write", argv)
            # The user's codex config may widen workspace-write; relay pins it back to the worktree.
            self.assertEqual(argv[-len(dispatch.CODEX_SANDBOX_PINS):], dispatch.CODEX_SANDBOX_PINS)
            self.assertIn("sandbox_workspace_write.writable_roots=[]", argv)
            self.assertNotIn("--add-dir", argv)  # workers never get git's own directories
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
        self.assertNotIn("--allowedTools", argv)
        self.assertEqual(argv.count("--add-dir"), 1)  # only the worktree, no .git directories
        argv, _, _ = dispatch.build_argv({"runtime": "codex", "repo": "x"}, "P", "s", Path("L"), wt, cfg)
        self.assertIn("workspace-write", argv)
        self.assertEqual(argv[-len(dispatch.CODEX_SANDBOX_PINS):], dispatch.CODEX_SANDBOX_PINS)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)


class HermesTests(DispatchBase):
    def test_argv_keeps_prompt_off_the_command_line(self):
        wt = dispatch.make_worktree(self.repo, "h1")[0]
        log_dir = self.dir / "runs" / "h1"
        prompt = "P" + NL + "line2 \"quoted\""
        launcher = mock.Mock()  # hermes never goes through agent-cortex
        with mock.patch.object(dispatch, "_cortex_launcher", return_value=launcher):
            argv, source, stdin = dispatch.build_argv(
                {"runtime": "hermes", "repo": "x", "model": "m1", "effort": "high"}, prompt, "s", log_dir, wt, {})
        self.assertEqual((argv[0], argv[1], source, stdin), ("hermes", "-z", "built-in", None))
        self.assertFalse(launcher.method_calls)
        self.assertNotIn(prompt, " ".join(argv))
        self.assertEqual((log_dir / "prompt.md").read_text(encoding="utf-8"), prompt)
        self.assertIn(str(log_dir / "prompt.md"), argv[2])
        self.assertEqual(argv[argv.index("--in") + 1], str(wt))
        self.assertEqual(argv[argv.index("-t") + 1], "file")  # no terminal, no code execution
        self.assertEqual(argv[argv.index("--usage-file") + 1], str(log_dir / dispatch.HERMES_USAGE))
        self.assertEqual(argv[argv.index("-m") + 1], "m1")
        self.assertEqual(argv[argv.index("--reasoning") + 1], "high")
        for flag in ("--yolo", "--accept-hooks", "terminal", "code_execution"):
            self.assertNotIn(flag, argv)

    def test_write_root_is_the_worktree_or_an_empty_dir_for_plan(self):
        wt, run_dir = self.dir / "wt", self.dir / "run"
        env = dispatch.worker_env({"runtime": "hermes"}, wt, run_dir, "h")
        self.assertEqual(env, {"AGENT_RELAY_HOLDER": "h", "HERMES_WRITE_SAFE_ROOT": str(wt)})
        env = dispatch.worker_env({"runtime": "hermes", "mode": "plan"}, wt, run_dir, "h")
        root = Path(env["HERMES_WRITE_SAFE_ROOT"])
        self.assertTrue(root.is_dir())
        self.assertEqual(root.parent, run_dir)
        self.assertEqual(dispatch.worker_env({"runtime": "claude"}, wt, run_dir, "h"), {"AGENT_RELAY_HOLDER": "h"})

    def test_usage_report(self):
        run_dir = self.dir / "run"
        self.assertIsNone(dispatch.hermes_usage(run_dir))
        run_dir.mkdir()
        (run_dir / dispatch.HERMES_USAGE).write_text("not json", encoding="utf-8")
        self.assertIsNone(dispatch.hermes_usage(run_dir))
        (run_dir / dispatch.HERMES_USAGE).write_text(json.dumps(
            {"model": "m", "total_tokens": 10, "session_id": "s", "auxiliary": {}}), encoding="utf-8")
        self.assertEqual(dispatch.hermes_usage(run_dir), {"model": "m", "total_tokens": 10})

    def test_dispatch_passes_write_root_and_reports_usage(self):
        self.cfg["dispatch"]["runtimes"] = ["hermes"]
        env = self.send_task(runtime="hermes")
        # Stand-in for hermes.exe: writes inside its write root and leaves a usage report.
        code = (
            "import json, os, pathlib, sys;"
            "root = pathlib.Path(os.environ['HERMES_WRITE_SAFE_ROOT']);"
            "(root / 'proof.txt').write_text('hermes', encoding='utf-8');"
            "pathlib.Path(sys.argv[1]).write_text(json.dumps({'model': 'fake', 'total_tokens': 42}), encoding='utf-8');"
            "print('done by fake hermes')"
        )

        def builder(spec, prompt, slice_id, log_dir, worktree, cfg):
            return ["hermes", "-c", code, str(log_dir / dispatch.HERMES_USAGE)], "built-in", None

        with mock.patch.object(dispatch, "build_argv", side_effect=builder), \
                mock.patch.object(dispatch, "resolve_executable", return_value=sys.executable):
            report = dispatch.dispatch(env["id"], "claude-code", self.cfg, sync=False)
        self.assertEqual(report["status"], "ok", report)
        self.assertEqual((Path(report["worktree"]) / "proof.txt").read_text(encoding="utf-8"), "hermes")
        self.assertTrue(report["commit"].startswith("committed:"))
        self.assertEqual(report["usage"], [{"attempt": 1, "model": "fake", "total_tokens": 42}])
        body = self.reply_for(env)["body"]
        self.assertIn("Hermes --usage-file", body)
        self.assertIn("done by fake hermes", body)

    def test_stale_usage_dropped_and_early_stop_is_a_failure(self):
        log_dir = self.dir / "runs" / "h2"
        log_dir.mkdir(parents=True)
        (log_dir / dispatch.HERMES_USAGE).write_text('{"model": "old"}', encoding="utf-8")
        dispatch.build_hermes_argv({"runtime": "hermes"}, "P", log_dir, self.repo)
        self.assertFalse((log_dir / dispatch.HERMES_USAGE).exists())

        self.cfg["dispatch"]["runtimes"] = ["hermes"]
        env = self.send_task(runtime="hermes")
        # exit 0, but Hermes reports it stopped before finishing (iteration budget used up).
        code = ("import json, pathlib, sys;"
                "pathlib.Path(sys.argv[1]).write_text(json.dumps({'completed': False}), encoding='utf-8')")

        def builder(spec, prompt, slice_id, log_dir, worktree, cfg):
            return ["hermes", "-c", code, str(log_dir / dispatch.HERMES_USAGE)], "built-in", None

        with mock.patch.object(dispatch, "build_argv", side_effect=builder), \
                mock.patch.object(dispatch, "resolve_executable", return_value=sys.executable):
            report = dispatch.dispatch(env["id"], "claude-code", self.cfg, sync=False)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["usage"], [{"attempt": 1, "completed": False}])


@unittest.skipUnless(shutil.which("ssh-keygen"), "ssh-keygen not installed")
class SenderTrustTests(DispatchBase):
    """T-20261021-07: deny-by-default senders and signed envelopes."""

    def setUp(self):
        super().setUp()
        keys = self.dir / "keys"
        self.commander_key, self.mallory_key, self.receiver_key = (keys / n for n in ("commander", "mallory", "receiver"))
        self.allowed = self.dir / "allowed_signers"  # the receiver's trust list
        signing.trust(self.allowed, "commander", signing.keygen(self.commander_key, "commander"))
        signing.keygen(self.mallory_key, "mallory")
        self.commander_trusts = self.dir / "commander_allowed_signers"
        signing.trust(self.commander_trusts, "claude-code", signing.keygen(self.receiver_key, "claude-code"))
        self.cfg["dispatch"].pop("require_signature")
        self.cfg["signing"] = {"key": str(self.receiver_key), "allowed_signers": str(self.allowed)}

    def send_signed(self, key):
        env = envelope.build(sender="commander", to="claude-code", subject="make proof", body="write proof.txt",
                             kind="task", dispatch={"runtime": "claude", "repo": str(self.repo), "timeout_minutes": 1})
        env = signing.sign(env, key) if key else env
        self.store.deliver(env)
        return env

    def test_signed_task_runs_and_reply_is_signed(self):
        env = self.send_signed(self.commander_key)
        report = self.run_fake(env)
        self.assertEqual(report["status"], "ok", report)
        reply = self.reply_for(env)
        self.assertIsNone(signing.verify(reply, self.commander_trusts))

    def test_unsigned_and_forged_tasks_refused(self):
        for key, reason in ((None, "unsigned"), (self.mallory_key, "bad signature")):
            with self.subTest(reason=reason):
                env = self.send_signed(key)
                report = self.run_fake(env)
                self.assertEqual(report["status"], "refused")
                self.assertIn(reason, self.reply_for(env)["body"])
                self.assertFalse((self.repo / dispatch.WORKTREE_DIR / env["id"]).exists())

    def test_replay_of_a_done_task_refused(self):
        env = self.send_signed(self.commander_key)
        self.assertEqual(self.run_fake(env)["status"], "ok")
        # Someone with push access moves the signed, done task back to new/.
        done = next((self.dir / "store" / "mailboxes" / "claude-code" / "done").glob(env["id"] + "*"))
        os.replace(done, done.parent.parent / "new" / done.name)
        with self.assertRaises(dispatch.DispatchError) as ctx:
            self.run_fake(env)
        self.assertIn("already run and answered", str(ctx.exception))

    def test_old_task_refused(self):
        env = self.send_signed(self.commander_key)
        self.cfg["dispatch"]["max_age_hours"] = 1
        with mock.patch.object(dispatch.time, "time", return_value=time.time() + 7200):
            report = self.run_fake(env)
        self.assertEqual(report["status"], "refused")
        self.assertIn("max_age_hours", self.reply_for(env)["body"])

    def test_no_senders_means_no_one(self):
        self.cfg["dispatch"].pop("senders")
        env = self.send_signed(self.commander_key)
        self.assertEqual(self.run_fake(env)["status"], "refused")
        self.assertIn("accepts dispatch tasks from no one", self.reply_for(env)["body"])


class BaseAndResultTests(DispatchBase):
    """T-20261021-08: the sender pins the start commit; the reply carries machine-readable fields."""

    def head(self, repo):
        return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()

    def test_base_format(self):
        for bad in ("HEAD", "main", "--upload-pack=x", "abc", "c" * 10, "A" * 40, 7):
            with self.subTest(bad=bad), self.assertRaises(envelope.EnvelopeError):
                dispatch.validate_spec({"runtime": "claude", "repo": "x", "base": bad})

    def test_worktree_starts_at_pinned_base_and_reply_has_result(self):
        pinned = self.head(self.repo)
        (self.repo / "local.txt").write_text("receiver-only commit\n", encoding="utf-8")
        git("add", ".", cwd=self.repo)
        git("commit", "--quiet", "-m", "local", cwd=self.repo)
        env = self.send_task(base=pinned)
        report = self.run_fake(env)
        self.assertEqual(report["status"], "ok", report)
        self.assertEqual(report["base"], pinned)
        self.assertFalse((Path(report["worktree"]) / "local.txt").exists())
        result = self.reply_for(env)["result"]
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["base"], pinned)
        self.assertEqual(result["branch"], f"relay/{env['id']}")
        self.assertEqual(result["commit"], self.head(report["worktree"]))
        self.assertEqual(len(result["commit"]), 40)

    def test_cli_resolves_base_to_full_sha(self):
        full = self.head(self.repo)
        cwd = os.getcwd()
        os.chdir(self.repo)
        try:
            self.assertEqual(cli._full_sha("HEAD"), full)
            self.assertEqual(cli._full_sha(full[:8]), full)
            self.assertEqual(cli._full_sha(full.upper()), full)
            with self.assertRaises(SystemExit):
                cli._full_sha("no-such-ref")
        finally:
            os.chdir(cwd)

    def test_base_fetched_from_origin(self):
        upstream = self.dir / "upstream"
        subprocess.run(["git", "clone", "--quiet", str(self.repo), str(upstream)], check=True, capture_output=True)
        (upstream / "new.txt").write_text("only upstream has this\n", encoding="utf-8")
        git("add", ".", cwd=upstream)
        git("commit", "--quiet", "-m", "upstream", cwd=upstream)
        git("remote", "add", "origin", str(upstream), cwd=self.repo)
        env = self.send_task(base=self.head(upstream))
        report = self.run_fake(env)
        self.assertEqual(report["status"], "ok", report)
        self.assertTrue((Path(report["worktree"]) / "new.txt").exists())

    def test_unknown_base_refused(self):
        env = self.send_task(base="0" * 40)
        report = self.run_fake(env)
        self.assertEqual(report["status"], "refused")
        self.assertIn("not a commit", self.reply_for(env)["body"])
        self.assertEqual(self.reply_for(env)["result"], {"status": "refused"})
        self.assertFalse((self.repo / dispatch.WORKTREE_DIR / env["id"]).exists())

    def test_result_only_on_result_kind(self):
        with self.assertRaises(envelope.EnvelopeError):
            envelope.build(sender="a", to="b", subject="x", body="", kind="note", result={"status": "ok"})
        with self.assertRaises(envelope.EnvelopeError):
            envelope.build(sender="a", to="b", subject="x", body="", kind="result", result={"branch": "x"})


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
        self.assertIn(f"relay: {env['id']}", reply["body"])  # committed by relay, not the worker
        self.assertTrue(report["commit"].startswith("committed:"))
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
        self.assertIn(f"relay: {env['id']}", self.replies_for(env)[-1]["body"])  # base is the branch's merge-base

    def test_stray_folder_is_not_used_as_worktree(self):
        env = self.send_task()
        (self.repo / ".relay-worktrees" / env["id"]).mkdir(parents=True)
        report = self.run_fake(env)
        self.assertEqual(report["status"], "refused")
        self.assertIn("is not the", self.reply_for(env)["body"])

    def test_broken_bundle_does_not_stop_run(self):
        env = self.send_task()
        with mock.patch.object(dispatch.context, "bundle", side_effect=TypeError("bad vault")):
            self.assertEqual(self.run_fake(env)["status"], "ok")

    def test_session_tail_off_for_dispatch_by_default(self):
        seen = {}

        def capture(repo, cfg, **kw):
            seen.update(kw)
            return ""

        env = self.send_task()
        with mock.patch.object(dispatch.context, "bundle", side_effect=capture):
            self.run_fake(env)
        self.assertIs(seen["sessions"], False)
        self.cfg["dispatch"]["context_sessions"] = True
        env = self.send_task()
        with mock.patch.object(dispatch.context, "bundle", side_effect=capture):
            self.run_fake(env)
        self.assertIs(seen["sessions"], True)

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


class CommitGuardTests(DispatchBase):
    def test_default_claude_tools_grant_no_shell(self):
        self.assertEqual(dispatch.DEFAULT_CLAUDE_TOOLS, ())

    def test_relay_commit_skips_hooks(self):
        hook = self.repo / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\necho ran > \"$(git rev-parse --show-toplevel)/HOOK_RAN\"\n", encoding="utf-8")
        hook.chmod(0o755)
        env = self.send_task()
        report = self.run_fake(env)
        self.assertTrue(report["commit"].startswith("committed:"), report)
        self.assertFalse((Path(report["worktree"]) / "HOOK_RAN").exists())

    def test_tampered_git_pointer_blocks_commit(self):
        env = self.send_task()
        # Windows marks the .git file hidden, so a plain overwrite fails; delete and recreate.
        tamper = "import os, pathlib; os.remove('.git'); pathlib.Path('.git').write_text('gitdir: C:/evil', encoding='utf-8')"
        report = self.run_fake(env, tamper)
        self.assertIn(".git pointer was changed", report["commit"])
        self.assertIn(".git pointer was changed", self.reply_for(env)["body"])

    def test_plan_mode_commits_nothing(self):
        env = self.send_task(mode="plan")
        report = self.run_fake(env, "open('x.txt', 'w').write('x')")
        self.assertEqual(report["commit"], "plan mode: nothing to commit")


class ReaperRobustnessTests(DispatchBase):
    def test_garbage_lease_does_not_stop_watch(self):
        env = self.send_task()
        self.store.read("claude-code", env["id"])
        dispatch.write_state(env["id"], phase="running", deadline="garbage", started="x")
        cli.watch_once("claude-code", self.cfg, sync=False)  # must not raise
        self.assertEqual(self.replies_for(env), [])

    def test_never_started_run_reaped_only_with_dispatch_bell(self):
        env = self.send_task()
        later = time.time() + dispatch.LEASE_GRACE_S * 4
        self.assertEqual(dispatch.reap_interrupted("claude-code", self.cfg, now=later, rung=[env["id"]]), [])
        self.cfg["doorbells"] = [{"type": "dispatch"}]
        self.assertEqual(dispatch.reap_interrupted("claude-code", self.cfg, now=time.time(), rung=[env["id"]]), [])
        self.assertEqual(dispatch.reap_interrupted("claude-code", self.cfg, now=later, rung=[env["id"]]), [env["id"]])
        self.assertIn("沒有啟動", self.reply_for(env)["body"])


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
