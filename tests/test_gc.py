"""T-20261021-11: relay gc removes finished work only."""

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

from agent_relay import dispatch, envelope, gc  # noqa: E402
from agent_relay.store import Store  # noqa: E402

WORKER = "import pathlib; pathlib.Path('proof.txt').write_text('x')"


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class GcTests(unittest.TestCase):
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
        self.repo = self.dir / "repos" / "demo"
        self.repo.parent.mkdir()
        git("clone", "--quiet", str(self.remote), str(self.repo), cwd=self.dir)
        self.cfg = {"store": str(self.dir / "store"), "agent": "claude-code", "doorbells": [],
                    "dispatch": {"runtimes": ["claude"], "repo_roots": [str(self.dir / "repos")],
                                 "senders": ["commander"], "require_signature": False, "context": False,
                                 "push_branch": True, "push_remotes": [str(self.remote)]}}
        self.store = Store(self.cfg["store"])

    def run_task(self):
        env = envelope.build(sender="commander", to="claude-code", subject="t", body="t", kind="task",
                             dispatch={"runtime": "claude", "repo": str(self.repo), "timeout_minutes": 1})
        self.store.deliver(env)
        with mock.patch.object(dispatch, "build_argv", return_value=(["claude", "-c", WORKER], "test", None)), \
                mock.patch.object(dispatch, "resolve_executable", return_value=sys.executable):
            report = dispatch.dispatch(env["id"], "claude-code", self.cfg, sync=False)
        self.assertEqual(report["status"], "ok", report)
        return env, report

    def test_removes_finished_work_and_keeps_running_work(self):
        answered_env, answered = self.run_task()
        open_env, opened = self.run_task()
        # The commander accepted the first one (relay accept leaves this note behind).
        self.store.deliver(envelope.build(sender="commander", to="claude-code", subject="accepted: t", body="",
                                          kind="note", reply_to=answered["reply_id"]))
        # A third task is still running: its worktree and branch must survive.
        running_wt, running_branch, _ = dispatch.make_worktree(self.repo, "20991231T000000000Z-aaaaaa")
        dispatch.write_state("20991231T000000000Z-aaaaaa", phase="running", pid=os.getpid(),
                             started=time.time(), deadline=time.time() + 600)

        later = time.time() + 15 * 86400
        self.assertEqual(gc.plan(self.cfg, "claude-code", now=time.time()), [])  # nothing old enough yet
        actions = gc.plan(self.cfg, "claude-code", now=later)
        kinds = sorted(a["kind"] for a in actions)
        self.assertEqual(kinds, ["branch", "branch", "done-mail", "done-mail", "remote-branch",
                                 "run-dir", "run-dir", "worktree", "worktree"])
        self.assertTrue(all(a["id"] != "20991231T000000000Z-aaaaaa" for a in actions))
        self.assertIn("would remove", gc.render(actions, applied=False))

        before = {p.name for p in (self.repo / dispatch.WORKTREE_DIR).iterdir()}
        gc.apply(actions)
        self.assertTrue(all(a["done"] for a in actions), actions)
        after = {p.name for p in (self.repo / dispatch.WORKTREE_DIR).iterdir()}
        self.assertEqual(before - after, {answered_env["id"], open_env["id"]})
        self.assertTrue(running_wt.is_dir())
        branches = git("for-each-ref", "--format=%(refname:short)", "refs/heads/relay/", cwd=self.repo).split()
        self.assertEqual(branches, [running_branch])
        remote = git("for-each-ref", "--format=%(refname:short)", "refs/heads/relay/", cwd=self.remote).split()
        self.assertEqual(remote, [f"relay/{open_env['id']}"])  # not answered yet: its branch stays on origin
        self.assertFalse((Path(os.environ["AGENT_RELAY_HOME"]) / "runs" / answered_env["id"]).exists())
        self.assertEqual(self.store.list("claude-code", ("done",)), [])

    def test_work_that_exists_nowhere_else_is_kept_unless_forced(self):
        self.cfg["dispatch"]["push_branch"] = False
        env, report = self.run_task()
        (Path(report["worktree"]) / "scratch.txt").write_text("uncommitted", encoding="utf-8")
        later = time.time() + 15 * 86400
        actions = gc.apply(gc.plan(self.cfg, "claude-code", now=later))
        kept = {a["kind"] for a in actions if a.get("kept")}
        self.assertEqual(kept, {"worktree", "branch"})
        self.assertFalse(any(a.get("error") for a in actions))
        self.assertTrue(Path(report["worktree"]).is_dir())
        self.assertIn("kept:", gc.render(actions, applied=True))
        # The first pass removed the run record; restore it so the forced pass sees the run again.
        dispatch.write_state(env["id"], phase="replied", status="ok", finished=time.time() - 30 * 86400)
        forced = gc.apply(gc.plan(self.cfg, "claude-code", now=later, force=True))
        self.assertTrue(all(a["done"] for a in forced if a["kind"] in ("worktree", "branch")), forced)
        self.assertFalse(Path(report["worktree"]).exists())

    def test_cutoff_never_shorter_than_the_replay_window(self):
        for days, cfg_age in ((3, None), (0, None), (-1, None), (30, 0), (30, "x")):
            if cfg_age is not None:
                self.cfg["dispatch"]["max_age_hours"] = cfg_age
            with self.subTest(days=days, max_age=cfg_age), self.assertRaises(gc.GcError):
                gc.plan(self.cfg, "claude-code", days=days)


if __name__ == "__main__":
    unittest.main()
