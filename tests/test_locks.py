import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent_relay import dispatch, envelope, gitsync, locks  # noqa: E402
from agent_relay.store import Store  # noqa: E402

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        patch = mock.patch.dict(os.environ, {"AGENT_RELAY_HOME": str(self.dir / "home"), **GIT_ENV})
        patch.start()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(patch.stop)
        self.repo = self.dir / "repos" / "demo"
        self.repo.mkdir(parents=True)
        git("init", "-q", cwd=self.repo)
        (self.repo / "calc.py").write_text("x = 1\n", encoding="utf-8")
        (self.repo / "other.py").write_text("y = 1\n", encoding="utf-8")
        git("add", ".", cwd=self.repo)
        git("commit", "-q", "-m", "init", cwd=self.repo)
        self.store = self.dir / "store"


class PatternTests(unittest.TestCase):
    def test_overlap(self):
        self.assertTrue(locks.may_overlap("calc.py", "calc.py"))
        self.assertTrue(locks.may_overlap("src/", "src/api/x.py"))
        self.assertTrue(locks.may_overlap("src/**/*.py", "src/a.py"))
        self.assertTrue(locks.may_overlap("*.py", "docs/x.md"))  # root wildcard: assume overlap
        self.assertTrue(locks.may_overlap("CALC.py", "calc.py"))
        self.assertFalse(locks.may_overlap("calc.py", "other.py"))
        self.assertFalse(locks.may_overlap("src/api/", "src/web/"))

    def test_validate_paths(self):
        self.assertEqual(locks.validate_paths(["a\\b.py", "src/"]), ["a/b.py", "src/"])
        for bad in ([], "calc.py", ["../x"], ["a/../../x"], ["C:/x"], ["a" + chr(10)], [""], ["x"] * 51):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                locks.validate_paths(bad)


class IdentityTests(Base):
    def test_origin_url_without_credentials(self):
        git("remote", "add", "origin", "https://user:pw@github.com/SanHsien/Demo.git", cwd=self.repo)
        self.assertEqual(locks.repo_id(self.repo), "https://github.com/sanhsien/demo")
        git("remote", "set-url", "origin", "git@github.com:SanHsien/Demo.git", cwd=self.repo)
        self.assertEqual(locks.repo_id(self.repo), "https://github.com/sanhsien/demo")

    def test_worktree_shares_identity(self):
        wt = self.dir / "wt"
        git("worktree", "add", "-q", "-b", "w", str(wt), cwd=self.repo)
        self.assertEqual(locks.repo_id(wt), locks.repo_id(self.repo))


class ClaimTests(Base):
    def test_conflict_names_holder_and_audits(self):
        locks.checkout(self.store, self.repo, "relay:a", ["calc.py"], 600)
        with self.assertRaises(locks.ClaimConflict) as ctx:
            locks.checkout(self.store, self.repo, "relay:b", ["calc.py", "new.py"], 600)
        self.assertIn("relay:a", str(ctx.exception))
        events = sorted(json.loads(p.read_text(encoding="utf-8"))["event"] for p in (self.store / "audit").rglob("*.json"))
        self.assertEqual(events, ["blocked", "checkout"])

    def test_disjoint_same_holder_and_expired(self):
        locks.checkout(self.store, self.repo, "relay:a", ["calc.py"], 600)
        locks.checkout(self.store, self.repo, "relay:b", ["other.py"], 600)
        locks.checkout(self.store, self.repo, "relay:a", ["calc.py"], 600)  # same holder again
        locks.checkout(self.store, self.repo, "relay:old", ["x.py"], -1)  # already expired
        locks.checkout(self.store, self.repo, "relay:c", ["x.py"], 600)

    def test_release_frees_and_records_changes(self):
        claim = locks.checkout(self.store, self.repo, "relay:a", ["calc.py"], 600)
        locks.release(self.store, claim, changed=["calc.py"])
        locks.checkout(self.store, self.repo, "relay:b", ["calc.py"], 600)
        checkins = [json.loads(p.read_text(encoding="utf-8")) for p in (self.store / "audit").rglob("*checkin*.json")]
        self.assertEqual(checkins[0]["changed"], ["calc.py"])

    def test_concurrent_checkouts_one_winner(self):
        results = []

        def take(holder):
            try:
                locks.checkout(self.store, self.repo, holder, ["calc.py"], 600)
                results.append("won")
            except locks.ClaimConflict:
                results.append("blocked")

        threads = [threading.Thread(target=take, args=(f"relay:{i}",)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(results), ["blocked"] * 5 + ["won"])

    def test_out_of_scope(self):
        self.assertEqual(locks.out_of_scope(["calc.py", "src/a.py", "x.md"], ["calc.py", "src/"]), ["x.md"])

    def test_gitsync_commits_claims_and_audit(self):
        git("init", "-q", str(self.store), cwd=self.dir)
        locks.checkout(self.store, self.repo, "relay:a", ["calc.py"], 600)
        self.assertTrue(gitsync.commit(self.store, "claim"))
        tracked = subprocess.run(["git", "-C", str(self.store), "ls-files"], capture_output=True, text=True).stdout
        self.assertIn("claims/", tracked)
        self.assertIn("audit/", tracked)
        self.assertNotIn(".lock", tracked)


class DispatchClaimTests(Base):
    def setUp(self):
        super().setUp()
        self.cfg = {"store": str(self.store), "agent": "cc", "doorbells": [],
                    "dispatch": {"runtimes": ["claude"], "repo_roots": [str(self.dir / "repos")], "context": False}}
        self.mailbox = Store(self.store)

    def task(self, paths):
        env = envelope.build(sender="commander", to="cc", subject="edit", body="b", kind="task",
                             dispatch={"runtime": "claude", "repo": str(self.repo), "timeout_minutes": 1, "paths": paths})
        self.mailbox.deliver(env)
        return env

    def run_worker(self, env, code):
        with mock.patch.object(dispatch, "build_argv", return_value=(["claude", "-c", code], "t", None)), \
                mock.patch.object(dispatch, "resolve_executable", return_value=sys.executable):
            return dispatch.dispatch(env["id"], "cc", self.cfg, sync=False)

    def reply(self, env):
        return next(e for _, e in self.mailbox.list("commander") if e.get("reply_to") == env["id"])

    def test_dispatch_blocked_while_someone_holds_the_file(self):
        blocker = locks.checkout(self.store, self.repo, "relay:someone", ["calc.py"], 600)
        env = self.task(["calc.py"])
        report = self.run_worker(env, "open('calc.py','a').write('# c')")
        self.assertEqual(report["status"], "refused")
        body = self.reply(env)["body"]
        self.assertIn("blocked", body)
        self.assertIn("relay:someone", body)
        self.assertFalse((self.repo / ".relay-worktrees" / env["id"]).exists())  # nothing started
        locks.release(self.store, blocker)

    def test_claim_held_during_run_and_released_after(self):
        env = self.task(["calc.py"])
        key = locks.repo_key(locks.repo_id(self.repo))
        code = ("import os, json, pathlib;"
                f"held = [c['holder'] for c in map(lambda p: json.loads(p.read_text()), pathlib.Path(r'{self.store}/claims/{key}').glob('*.json'))];"
                "open('calc.py','a').write('# ' + os.environ['AGENT_RELAY_HOLDER'] + ' ' + ','.join(held))")
        report = self.run_worker(env, code)
        self.assertEqual(report["status"], "ok")
        text = (Path(report["worktree"]) / "calc.py").read_text(encoding="utf-8")
        self.assertIn(f"relay:{env['id']} relay:{env['id']}", text)  # env passed, claim live during the run
        self.assertEqual(locks.active_claims(self.store, key), [])

    def test_out_of_scope_change_not_committed(self):
        env = self.task(["calc.py"])
        report = self.run_worker(env, "open('calc.py','a').write('#');open('other.py','a').write('#')")
        self.assertEqual(report["status"], "out-of-scope")
        self.assertIn("other.py", report["commit"])
        self.assertIn("status: out-of-scope", self.reply(env)["body"])
        self.assertEqual(locks.active_claims(self.store, locks.repo_key(locks.repo_id(self.repo))), [])

    def test_conflict_prediction_and_release(self):
        env = self.task(["calc.py"])
        # main line moves calc.py while the worker runs
        code = ("import subprocess, pathlib;"
                f"pathlib.Path(r'{self.repo}/calc.py').write_text('x = 2\\n');"
                f"subprocess.run(['git','-C',r'{self.repo}','commit','-qam','main moved'], check=True);"
                "open('calc.py','a').write('# worker')")
        report = self.run_worker(env, code)
        self.assertEqual(report["status"], "ok")
        body = self.reply(env)["body"]
        self.assertIn("合併可能衝突：calc.py", body)
        events = [json.loads(p.read_text(encoding="utf-8"))["event"] for p in (self.store / "audit").rglob("*.json")]
        self.assertIn("checkout", events)
        self.assertIn("checkin", events)

    def test_require_paths_policy(self):
        self.cfg["dispatch"]["require_paths"] = True
        env = envelope.build(sender="commander", to="cc", subject="x", body="b", kind="task",
                             dispatch={"runtime": "claude", "repo": str(self.repo), "timeout_minutes": 1})
        self.mailbox.deliver(env)
        self.assertEqual(self.run_worker(env, "pass")["status"], "refused")

    def test_bad_paths_rejected_at_the_envelope(self):
        with self.assertRaises(envelope.EnvelopeError):
            envelope.build(sender="a", to="b", subject="x", body="", kind="task",
                           dispatch={"runtime": "claude", "repo": "x", "paths": ["../../etc"]})


if __name__ == "__main__":
    unittest.main()
