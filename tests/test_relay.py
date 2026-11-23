import io
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

# Never touch the real ~/.agent-relay: every test run gets its own home.
os.environ["AGENT_RELAY_HOME"] = tempfile.mkdtemp(prefix="agent-relay-test-home-")
import atexit, shutil  # noqa: E402
atexit.register(shutil.rmtree, os.environ["AGENT_RELAY_HOME"], ignore_errors=True)

from agent_relay import __version__, cli, doorbell, envelope, gitsync, poller  # noqa: E402
from agent_relay.store import Store, StoreError  # noqa: E402


def git(*args, cwd=None):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.env_patch = mock.patch.dict(os.environ, {"AGENT_RELAY_HOME": str(self.dir / "home")})
        self.env_patch.start()

    def tearDown(self):
        self.env_patch.stop()
        self.tmp.cleanup()


class EnvelopeTests(unittest.TestCase):
    def test_build_round_trips(self):
        env = envelope.build(sender="commander", to="hermes", subject="hi", body="中文內容", kind="task")
        self.assertEqual(envelope.loads(envelope.dumps(env)), env)
        self.assertEqual(env["thread"], env["id"])

    def test_reply_inherits_thread(self):
        env = envelope.build(sender="hermes", to="commander", subject="re", body="", reply_to="X1", kind="reply")
        self.assertEqual(env["thread"], "X1")

    def test_rejects_bad_names_and_ids(self):
        with self.assertRaises(envelope.EnvelopeError):
            envelope.build(sender="../evil", to="hermes", subject="x", body="")
        env = envelope.build(sender="a", to="b", subject="x", body="")
        env["id"] = "../x"
        with self.assertRaises(envelope.EnvelopeError):
            envelope.validate(env)

    def test_rejects_non_string_names_and_ids(self):
        """5 looks like a legal name once str()-ed; it must be refused, not crash later in Path()."""
        for key in ("from", "to", "id", "thread", "reply_to"):
            env = envelope.build(sender="a", to="b", subject="s", body="x")
            env[key] = 5
            with self.subTest(key=key), self.assertRaises(envelope.EnvelopeError):
                envelope.validate(env)

    def test_rejects_unknown_schema(self):
        env = envelope.build(sender="a", to="b", subject="x", body="")
        env["schema"] = "other@9"
        with self.assertRaises(envelope.EnvelopeError):
            envelope.validate(env)


class StoreTests(Base):
    def test_lifecycle(self):
        store = Store(self.dir / "store")
        env = envelope.build(sender="commander", to="codex", subject="do it", body="b")
        store.deliver(env)
        self.assertEqual([s for s, _ in store.list("codex")], ["new"])
        self.assertEqual(store.read("codex", env["id"])["subject"], "do it")
        self.assertEqual([s for s, _ in store.list("codex")], ["cur"])
        store.ack("codex", env["id"])
        self.assertEqual(store.list("codex"), [])
        self.assertEqual([s for s, _ in store.list("codex", ("done",))], ["done"])

    def test_duplicate_id_refused(self):
        store = Store(self.dir / "store")
        env = envelope.build(sender="a", to="b", subject="x", body="")
        store.deliver(env)
        with self.assertRaises(StoreError):
            store.deliver(env)

    def test_corrupt_file_skipped(self):
        store = Store(self.dir / "store")
        box = store.box("b", "new")
        box.mkdir(parents=True)
        (box / "junk.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(store.list("b"), [])


class TwoMachineGitTests(Base):
    """Two clones of one remote stand in for two machines."""

    def setUp(self):
        super().setUp()
        remote = self.dir / "remote.git"
        git("init", "--bare", "--quiet", "--initial-branch=mailbox", str(remote))
        seed = self.dir / "seed"
        git("init", "--quiet", "--initial-branch=mailbox", str(seed))
        for repo in [seed]:
            git("config", "user.email", "t@example.com", cwd=repo)
            git("config", "user.name", "t", cwd=repo)
        (seed / "README.md").write_text("store\n")
        git("add", ".", cwd=seed)
        git("commit", "--quiet", "-m", "init", cwd=seed)
        git("remote", "add", "origin", str(remote), cwd=seed)
        git("push", "--quiet", "-u", "origin", "mailbox", cwd=seed)
        self.a = self.dir / "machine-a"
        self.b = self.dir / "machine-b"
        for path in (self.a, self.b):
            gitsync.clone(str(remote), path, "mailbox")
            git("config", "user.email", "t@example.com", cwd=path)
            git("config", "user.name", "t", cwd=path)

    def test_mail_crosses_machines_and_ack_comes_back(self):
        env = envelope.build(sender="commander", to="hermes", subject="cross", body="b", kind="task")
        Store(self.a).deliver(env)
        self.assertTrue(gitsync.sync(self.a, "send"))
        gitsync.pull(self.b)
        self.assertEqual([e["id"] for _, e in Store(self.b).list("hermes")], [env["id"]])
        Store(self.b).ack("hermes", env["id"])
        gitsync.sync(self.b, "ack")
        gitsync.pull(self.a)
        self.assertEqual(Store(self.a).list("hermes"), [])

    def test_concurrent_pushes_rebase(self):
        Store(self.a).deliver(envelope.build(sender="a", to="x", subject="1", body=""))
        Store(self.b).deliver(envelope.build(sender="b", to="x", subject="2", body=""))
        gitsync.sync(self.a)
        gitsync.sync(self.b)  # must pull --rebase and retry, not fail
        gitsync.pull(self.a)
        self.assertEqual(len(Store(self.a).list("x")), 2)

    def test_ack_of_last_new_message_and_concurrent_mail_do_not_conflict(self):
        # Seen live: B acked the only message in x/new/ (git: "new/ renamed to done/") while A
        # delivered more mail to x/new/; the rebase then failed with a file-location conflict.
        first = envelope.build(sender="a", to="x", subject="1", body="")
        Store(self.a).deliver(first)
        gitsync.sync(self.a)
        gitsync.pull(self.b)
        Store(self.b).ack("x", first["id"])
        gitsync.commit(self.b, "ack")
        later = [envelope.build(sender="a", to="x", subject=str(n), body="") for n in (2, 3)]
        for env in later:
            Store(self.a).deliver(env)
        gitsync.sync(self.a)
        gitsync.sync(self.b)  # must rebase cleanly
        gitsync.pull(self.a)
        for store in (self.a, self.b):
            self.assertEqual(sorted(e["id"] for _, e in Store(store).list("x")), sorted(e["id"] for e in later))
            self.assertEqual([e["id"] for _, e in Store(store).list("x", ("done",))], [first["id"]])

    def test_concurrent_processes_on_one_clone(self):
        # Seen live (T-09): five dispatch tabs pulling one clone at once; four failed with
        # "Cannot rebase onto multiple branches". The store lock serialises them.
        for n in range(3):
            Store(self.a).deliver(envelope.build(sender="a", to="x", subject=str(n), body=""))
            gitsync.sync(self.a)
        code = ("import sys; sys.path.insert(0, sys.argv[1]);"
                "from agent_relay import gitsync, envelope; from agent_relay.store import Store;"
                "from pathlib import Path; b = Path(sys.argv[2]);"
                "Store(b).deliver(envelope.build(sender='b', to='y', subject='p', body=''));"
                "gitsync.sync(b, 'concurrent')")
        procs = [subprocess.Popen([sys.executable, "-c", code, str(ROOT), str(self.b)],
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) for _ in range(5)]
        outputs = [p.communicate(timeout=300)[0] for p in procs]
        self.assertEqual([p.returncode for p in procs], [0] * 5, outputs)
        self.assertFalse((self.b / ".git" / "agent-relay-sync.lock").exists())
        gitsync.pull(self.a)
        self.assertEqual(len(Store(self.a).list("y")), 5)
        self.assertEqual(len(Store(self.a).list("x")), 3)

    def test_stale_store_lock_is_broken(self):
        lock = self.b / ".git" / "agent-relay-sync.lock"
        lock.write_text("crashed", encoding="utf-8")
        old = time.time() - gitsync.LOCK_STALE_S - 10
        os.utime(lock, (old, old))
        gitsync.pull(self.b)
        self.assertFalse(lock.exists())

    def test_only_mailboxes_committed(self):
        (self.a / "other.txt").write_text("not mine")
        Store(self.a).deliver(envelope.build(sender="a", to="x", subject="1", body=""))
        gitsync.sync(self.a)
        status = subprocess.run(["git", "-C", str(self.a), "status", "--porcelain"], capture_output=True, text=True)
        self.assertIn("other.txt", status.stdout)


class DoorbellTests(Base):
    def cfg(self, bells):
        return {"store": str(self.dir / "store"), "agent": "hermes", "doorbells": bells}

    def test_rings_once_per_message(self):
        log = self.dir / "rang.txt"
        bell = {
            "type": "command",
            "argv": [sys.executable, "-c", "import sys; open(sys.argv[1],'a',encoding='utf-8').write(sys.argv[2]+'\\n')", str(log), "{summary}"],
        }
        cfg = self.cfg([bell])
        Store(cfg["store"]).deliver(envelope.build(sender="commander", to="hermes", subject="ring me", body=""))
        first = cli.watch_once("hermes", cfg, sync=False)
        second = cli.watch_once("hermes", cfg, sync=False)
        self.assertEqual(first[0]["bells"]["0:command"], "ok")
        self.assertEqual(second, [])
        self.assertIn("ring me", log.read_text(encoding="utf-8"))

    def test_broken_bell_does_not_block_others(self):
        project = self.dir / "proj"
        project.mkdir()
        cfg = self.cfg([{"type": "command", "argv": ["definitely-not-a-binary-xyz"]}, {"type": "handoff", "project_roots": [str(self.dir)]}])
        env = envelope.build(sender="commander", to="hermes", subject="fix", body="steps", kind="task", project=str(project))
        Store(cfg["store"]).deliver(env)
        bells = cli.watch_once("hermes", cfg, sync=False)[0]["bells"]
        self.assertTrue(bells["0:command"].startswith("error"))
        self.assertTrue(bells["1:handoff"].startswith("ok"))
        cards = list((project / ".claude" / "handoffs").glob("*.md"))
        self.assertEqual(len(cards), 1)
        text = cards[0].read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\nstatus: pending\n"))
        self.assertIn(env["id"], text)

    def test_handoff_resolves_asr_session(self):
        project = self.dir / "proj"
        project.mkdir()
        registry = self.dir / "claude-sessions.json"
        registry.write_text(json.dumps({"version": 1, "sessions": {
            "old": {"session_id": "old", "name": "inbox", "cwd": str(self.dir / "gone"), "status": "closed"},
            "s1": {"session_id": "s1", "name": "inbox", "cwd": str(project), "status": "active"},
        }}), encoding="utf-8")
        env = envelope.build(sender="commander", to="claude-code", subject="t", body="", kind="task", project="asr:inbox")
        with mock.patch.dict(os.environ, {"CLAUDE_SESSION_REGISTRY_FILE": str(registry)}):
            self.assertTrue(doorbell.ring(env, [{"type": "handoff", "project_roots": [str(self.dir)]}])["0:handoff"].startswith("ok"))
            cards = list((project / ".claude" / "handoffs").glob("*.md"))
            self.assertEqual(len(cards), 1)
            self.assertIn(f"to: {project}\n", cards[0].read_text(encoding="utf-8"))
            env["project"] = "asr:nobody"
            self.assertTrue(doorbell.ring(env, [{"type": "handoff", "project_roots": [str(self.dir)]}])["0:handoff"].startswith("error"))

    def recorder(self, name="rang.txt", **extra):
        log = self.dir / name
        argv = [sys.executable, "-c", "import sys; open(sys.argv[1],'a',encoding='utf-8').write(sys.argv[2]+'\\n')",
                str(log), "{summary}"]
        return log, {"type": "command", "argv": argv, **extra}

    def test_min_priority_keeps_low_priority_mail_quiet(self):
        _, bell = self.recorder(min_priority="high")
        normal = envelope.build(sender="a", to="b", subject="fyi", body="")
        high = envelope.build(sender="a", to="b", subject="broken", body="", priority="high")
        self.assertEqual(doorbell.ring(normal, [bell])["0:command"], "skipped: priority filter")
        self.assertEqual(doorbell.ring(high, [bell])["0:command"], "ok")

    def test_min_priority_is_a_threshold_not_an_exact_match(self):
        _, bell = self.recorder(min_priority="normal")
        for prio, want in (("low", "skipped: priority filter"), ("normal", "ok"), ("high", "ok")):
            env = envelope.build(sender="a", to="b", subject=prio, body="", priority=prio)
            self.assertEqual(doorbell.ring(env, [bell])["0:command"], want, prio)

    def test_without_min_priority_every_priority_rings(self):
        log, bell = self.recorder()
        env = envelope.build(sender="a", to="b", subject="quiet", body="", priority="low")
        self.assertEqual(doorbell.ring(env, [bell])["0:command"], "ok")
        self.assertIn("quiet", log.read_text(encoding="utf-8"))

    def test_min_priority_of_the_wrong_type_is_a_bell_error_not_a_crash(self):
        for bad in (["high"], {"high": 1}, 2, True):
            _, bell = self.recorder(min_priority=bad)
            env = envelope.build(sender="a", to="b", subject="x", body="", priority="high")
            self.assertTrue(doorbell.ring(env, [bell])["0:command"].startswith("error"), bad)

    def test_bad_min_priority_does_not_stop_watch_or_other_bells(self):
        log, good = self.recorder()
        _, bad = self.recorder(name="bad.txt", min_priority=["high"])
        cfg = {"store": str(self.dir / "store"), "agent": "hermes", "doorbells": [bad, good, {"type": "toast", "min_priority": {}}]}
        for s in ("one", "two"):
            Store(cfg["store"]).deliver(envelope.build(sender="commander", to="hermes", subject=s, body=""))
        with mock.patch.dict(doorbell.CHANNELS, {"toast": lambda env, bell: "ok"}):
            report = cli.watch_once("hermes", cfg, sync=False)
        self.assertEqual(len(report), 2)
        self.assertTrue(all(r["bells"]["0:command"].startswith("error") for r in report))
        self.assertTrue(all(r["bells"]["2:toast"].startswith("error") for r in report))
        self.assertEqual(len(log.read_text(encoding="utf-8").splitlines()), 2)

    def test_bad_min_priority_is_an_error_not_silence(self):
        _, bell = self.recorder(min_priority="urgent")
        env = envelope.build(sender="a", to="b", subject="x", body="", priority="high")
        self.assertTrue(doorbell.ring(env, [bell])["0:command"].startswith("error"))

    def test_command_placeholders_are_not_shell(self):
        env = envelope.build(sender="a", to="b", subject="x; rm -rf /", body="")
        out = self.dir / "out.txt"
        bell = {"type": "command", "argv": [sys.executable, "-c", "import sys; open(sys.argv[1],'w').write(sys.argv[2])", str(out), "{subject}"]}
        self.assertEqual(doorbell.ring(env, [bell])["0:command"], "ok")
        self.assertEqual(out.read_text(), "x; rm -rf /")


class WatchBatchTests(Base):
    """R2: several mails in one tick ring a human-facing bell once, with a short summary."""

    def setUp(self):
        super().setUp()
        self.toasts = []
        patch = mock.patch.dict(doorbell.CHANNELS, {"toast": lambda env, bell: self.toasts.append(env) or "ok"})
        patch.start()
        self.addCleanup(patch.stop)

    def cfg(self, bells):
        return {"store": str(self.dir / "store"), "agent": "cc", "doorbells": bells}

    def deliver(self, cfg, *subjects, **kw):
        envs = [envelope.build(sender="commander", to="cc", subject=s, body="", **kw) for s in subjects]
        for env in envs:
            Store(cfg["store"]).deliver(env)
        return envs

    def recorder(self, **extra):
        log = self.dir / "rang.txt"
        argv = [sys.executable, "-c", "import sys; open(sys.argv[1],'a',encoding='utf-8').write(' '.join(sys.argv[2:])+'\\n')",
                str(log), "{count}", "{summary}"]
        return log, {"type": "command", "argv": argv, **extra}

    def test_three_mails_ring_one_toast_naming_all_of_them(self):
        cfg = self.cfg([{"type": "toast"}])
        self.deliver(cfg, "alpha", "beta", "gamma")
        report = cli.watch_once("cc", cfg, sync=False)
        self.assertEqual(len(self.toasts), 1)
        text = doorbell.summary(self.toasts[0])
        self.assertIn("3", text)
        for subject in ("alpha", "beta", "gamma"):
            self.assertIn(subject, text)
        self.assertEqual([r["bells"]["0:toast"] for r in report], ["ok"] * 3)  # report stays one entry per mail
        self.assertEqual(len(doorbell.load_rung("cc")), 3)

    def test_a_single_mail_rings_exactly_as_before(self):
        cfg = self.cfg([{"type": "toast"}])
        (env,) = self.deliver(cfg, "solo")
        cli.watch_once("cc", cfg, sync=False)
        self.assertEqual([t["id"] for t in self.toasts], [env["id"]])
        self.assertEqual(doorbell.summary(self.toasts[0]), doorbell.summary(env))

    def test_command_bell_rings_per_mail_unless_batch_is_set(self):
        log, bell = self.recorder()
        cfg = self.cfg([bell])
        self.deliver(cfg, "one", "two")
        cli.watch_once("cc", cfg, sync=False)
        self.assertEqual(len(log.read_text(encoding="utf-8").splitlines()), 2)  # {id}-style consumers keep working

    def test_command_batch_opt_in_fills_count_and_summary(self):
        log, bell = self.recorder(batch=True)
        cfg = self.cfg([bell])
        self.deliver(cfg, "one", "two")
        cli.watch_once("cc", cfg, sync=False)
        lines = log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("2 "), lines)
        self.assertIn("one", lines[0])
        self.assertIn("two", lines[0])

    def test_toast_batch_false_restores_one_toast_per_mail(self):
        cfg = self.cfg([{"type": "toast", "batch": False}])
        self.deliver(cfg, "a", "b")
        cli.watch_once("cc", cfg, sync=False)
        self.assertEqual(len(self.toasts), 2)

    def test_batch_summary_is_truncated(self):
        cfg = self.cfg([{"type": "toast"}])
        self.deliver(cfg, *[f"subject number {i} " + "x" * 200 for i in range(12)])
        cli.watch_once("cc", cfg, sync=False)
        text = doorbell.summary(self.toasts[0])
        self.assertLessEqual(len(text), doorbell.BATCH_SUMMARY_MAX)
        self.assertIn("12", text)
        self.assertIn("more", text)

    def test_handoff_is_never_batched(self):
        project = self.dir / "proj"
        project.mkdir()
        cfg = self.cfg([{"type": "handoff", "project_roots": [str(self.dir)], "batch": True}])
        self.deliver(cfg, "t1", "t2", kind="task", project=str(project))
        cli.watch_once("cc", cfg, sync=False)
        self.assertEqual(len(list((project / ".claude" / "handoffs").glob("*.md"))), 2)

    def test_filters_apply_before_batching(self):
        cfg = self.cfg([{"type": "toast", "min_priority": "high"}])
        self.deliver(cfg, "noise1", "noise2")
        (urgent,) = self.deliver(cfg, "urgent", priority="high")
        report = cli.watch_once("cc", cfg, sync=False)
        self.assertEqual([t["id"] for t in self.toasts], [urgent["id"]])
        self.assertEqual(sorted(r["bells"]["0:toast"] for r in report),
                         ["ok", "skipped: priority filter", "skipped: priority filter"])


class WatchRetryTests(Base):
    """R4: a bell that failed is retried later with a bounded count, and only then recorded as rung."""

    def cfg(self, bells):
        return {"store": str(self.dir / "store"), "agent": "cc", "doorbells": bells}

    def flaky(self, **extra):
        """A command bell that fails until the 'fixed' file exists; every call is logged."""
        log, fixed = self.dir / "calls.txt", self.dir / "fixed"
        code = ("import sys, os; open(sys.argv[1],'a').write('x\\n'); "
                "sys.exit(0 if os.path.exists(sys.argv[2]) else 1)")
        # Retries are opt-in (a command that already notified and then exits non-zero must not re-send).
        return log, fixed, {"type": "command", "argv": [sys.executable, "-c", code, str(log), str(fixed)],
                            "retries": 3, **extra}

    def calls(self, log):
        return len(log.read_text().splitlines()) if log.exists() else 0

    def deliver(self, cfg, subject="s"):
        env = envelope.build(sender="commander", to="cc", subject=subject, body="")
        Store(cfg["store"]).deliver(env)
        return env

    def test_failed_bell_is_not_recorded_as_rung_and_is_retried(self):
        log, fixed, bell = self.flaky()
        cfg = self.cfg([bell])
        env = self.deliver(cfg)
        first = cli.watch_once("cc", cfg, sync=False, now=1000.0)
        self.assertTrue(first[0]["bells"]["0:command"].startswith("error"))
        self.assertEqual(first[0]["retry"], ["0:command"])
        self.assertNotIn(env["id"], doorbell.load_rung("cc"))
        fixed.touch()
        second = cli.watch_once("cc", cfg, sync=False, now=1000.0 + doorbell.DEFAULT_RETRY_BACKOFF_S)
        self.assertEqual(second[0]["bells"], {"0:command": "ok"})
        self.assertIn(env["id"], doorbell.load_rung("cc"))
        self.assertEqual(cli.watch_once("cc", cfg, sync=False, now=10_000.0), [])
        self.assertEqual(self.calls(log), 2)

    def test_retry_waits_for_backoff(self):
        log, _, bell = self.flaky()
        cfg = self.cfg([bell])
        self.deliver(cfg)
        cli.watch_once("cc", cfg, sync=False, now=1000.0)
        self.assertEqual(cli.watch_once("cc", cfg, sync=False, now=1001.0), [])  # too soon: nothing rings
        self.assertEqual(self.calls(log), 1)

    def test_backoff_doubles(self):
        log, _, bell = self.flaky(retries=3, retry_backoff_seconds=10)
        cfg = self.cfg([bell])
        self.deliver(cfg)
        cli.watch_once("cc", cfg, sync=False, now=0.0)       # fail 1, next at 10
        cli.watch_once("cc", cfg, sync=False, now=10.0)      # fail 2, next at 30
        self.assertEqual(cli.watch_once("cc", cfg, sync=False, now=29.0), [])
        cli.watch_once("cc", cfg, sync=False, now=30.0)      # fail 3
        self.assertEqual(self.calls(log), 3)

    def test_only_the_failed_bell_is_rung_again(self):
        ok_log = self.dir / "ok.txt"
        ok_bell = {"type": "command", "argv": [sys.executable, "-c", "import sys; open(sys.argv[1],'a').write('x\\n')", str(ok_log)]}
        _, fixed, bad = self.flaky()
        cfg = self.cfg([ok_bell, bad])
        self.deliver(cfg)
        cli.watch_once("cc", cfg, sync=False, now=0.0)
        fixed.touch()
        again = cli.watch_once("cc", cfg, sync=False, now=10_000.0)
        self.assertEqual(again[0]["bells"], {"1:command": "ok"})
        self.assertEqual(self.calls(ok_log), 1)  # the bell that worked does not ring twice

    def test_permanent_failure_is_surfaced_then_recorded(self):
        log, _, bell = self.flaky(retries=1)
        cfg = self.cfg([bell])
        env = self.deliver(cfg)
        cli.watch_once("cc", cfg, sync=False, now=0.0)
        last = cli.watch_once("cc", cfg, sync=False, now=10_000.0)
        self.assertEqual(last[0]["gave_up"], ["0:command"])
        self.assertNotIn("retry", last[0])
        self.assertIn(env["id"], doorbell.load_rung("cc"))
        self.assertIn("gave up", (self.dir / "home" / "relay.log").read_text(encoding="utf-8"))
        self.assertEqual(cli.watch_once("cc", cfg, sync=False, now=99_999.0), [])
        self.assertEqual(self.calls(log), 2)

    def test_retries_are_off_unless_configured(self):
        log, _, bell = self.flaky()
        bell.pop("retries")
        cfg = self.cfg([bell])
        env = self.deliver(cfg)
        entry = cli.watch_once("cc", cfg, sync=False, now=0.0)[0]
        self.assertNotIn("retry", entry)
        self.assertIn(env["id"], doorbell.load_rung("cc"))
        self.assertEqual(cli.watch_once("cc", cfg, sync=False, now=10_000.0), [])
        self.assertEqual(self.calls(log), 1)  # an existing command bell still runs exactly once

    def test_retries_are_capped(self):
        self.assertEqual(doorbell.retry_policy({"type": "command", "retries": 1000})[0], doorbell.MAX_RETRIES)
        self.assertEqual(doorbell.retry_policy({"type": "command", "retries": "5"})[0], 0)  # invalid -> off
        self.assertEqual(doorbell.retry_policy({"type": "dispatch", "retries": 5})[0], 0)

    def test_retries_zero_keeps_the_old_record_on_first_try(self):
        _, _, bell = self.flaky(retries=0)
        cfg = self.cfg([bell])
        env = self.deliver(cfg)
        entry = cli.watch_once("cc", cfg, sync=False, now=0.0)[0]
        self.assertNotIn("retry", entry)
        self.assertIn(env["id"], doorbell.load_rung("cc"))

    def test_dispatch_bell_is_never_retried(self):
        def broken(env, bell):
            raise RuntimeError("boom")

        cfg = self.cfg([{"type": "dispatch"}])
        env = self.deliver(cfg)
        with mock.patch.dict(doorbell.CHANNELS, {"dispatch": broken}):
            entry = cli.watch_once("cc", cfg, sync=False, now=0.0)[0]
        self.assertNotIn("retry", entry)
        self.assertIn(env["id"], doorbell.load_rung("cc"))  # dispatch keeps its own run state; re-ringing could start a second worker

    def test_retry_state_is_dropped_once_the_mail_leaves_new(self):
        _, _, bell = self.flaky()
        cfg = self.cfg([bell])
        env = self.deliver(cfg)
        cli.watch_once("cc", cfg, sync=False, now=0.0)
        Store(cfg["store"]).ack("cc", env["id"])
        self.assertEqual(cli.watch_once("cc", cfg, sync=False, now=10_000.0), [])
        self.assertEqual(doorbell.load_retry("cc"), {})

    def test_failed_batch_is_retried_for_every_mail_in_it(self):
        toasts = []

        def toast(env, bell):
            toasts.append(env)
            if len(toasts) == 1:
                raise RuntimeError("notification service down")
            return "ok"

        cfg = self.cfg([{"type": "toast", "retries": 3}])
        a, b = self.deliver(cfg, "a"), self.deliver(cfg, "b")
        with mock.patch.dict(doorbell.CHANNELS, {"toast": toast}):
            first = cli.watch_once("cc", cfg, sync=False, now=0.0)
            self.assertEqual([e.get("retry") for e in first], [["0:toast"], ["0:toast"]])
            cli.watch_once("cc", cfg, sync=False, now=10_000.0)
        self.assertEqual(len(toasts), 2)
        self.assertEqual(toasts[1]["count"], 2)  # the retry is one summary again, not two toasts
        self.assertEqual(sorted(doorbell.load_rung("cc")), sorted([a["id"], b["id"]]))

    def test_auto_handling_runs_once_across_retries(self):
        _, fixed, bell = self.flaky()
        cfg = self.cfg([bell])
        self.deliver(cfg)
        with mock.patch.object(cli, "automate", return_value="did it") as auto:
            cli.watch_once("cc", cfg, sync=False, now=0.0)
            fixed.touch()
            cli.watch_once("cc", cfg, sync=False, now=10_000.0)
        self.assertEqual(auto.call_count, 1)

class WatchStateFailSafeTests(Base):
    """Unreadable rung/retry state must not turn into "nothing has rung yet" and re-ring everything."""

    def setUp(self):
        super().setUp()
        self.started = []
        patch = mock.patch.dict(doorbell.CHANNELS, {"dispatch": lambda env, bell: self.started.append(env["id"]) or "ok"})
        patch.start()
        self.addCleanup(patch.stop)
        self.cfg = {"store": str(self.dir / "store"), "agent": "cc", "doorbells": [{"type": "dispatch"}]}
        self.env = envelope.build(sender="commander", to="cc", subject="s", body="")
        Store(self.cfg["store"]).deliver(self.env)
        (self.dir / "home").mkdir(parents=True, exist_ok=True)

    def log(self):
        return (self.dir / "home" / "relay.log").read_text(encoding="utf-8")

    def test_corrupt_rung_file_rings_nothing_and_says_so(self):
        (self.dir / "home" / "rung-cc.json").write_text("{not json", encoding="utf-8")
        with self.assertRaises(doorbell.StateError):
            cli.watch_once("cc", self.cfg, sync=False)
        self.assertEqual(self.started, [])
        self.assertIn("rung-cc.json", self.log())

    def test_rung_file_of_the_wrong_shape_rings_nothing(self):
        (self.dir / "home" / "rung-cc.json").write_text('{"a": 1}', encoding="utf-8")
        with self.assertRaises(doorbell.StateError):
            cli.watch_once("cc", self.cfg, sync=False)
        self.assertEqual(self.started, [])

    def test_corrupt_retry_file_rings_nothing(self):
        (self.dir / "home" / "ring-retry-cc.json").write_text("[1, 2", encoding="utf-8")
        with self.assertRaises(doorbell.StateError):
            cli.watch_once("cc", self.cfg, sync=False)
        self.assertEqual(self.started, [])
        self.assertIn("ring-retry-cc.json", self.log())

    def test_malformed_retry_entry_is_settled_without_ringing_dispatch(self):
        (self.dir / "home" / "ring-retry-cc.json").write_text(
            json.dumps({self.env["id"]: {"0:dispatch": {"next_at": "soon"}}}), encoding="utf-8")
        report = cli.watch_once("cc", self.cfg, sync=False, now=10_000.0)
        self.assertEqual(self.started, [])
        self.assertIn(self.env["id"], doorbell.load_rung("cc"))
        self.assertEqual(report, [])

    def test_watch_once_command_reports_unreadable_state(self):
        (self.dir / "home" / "rung-cc.json").write_text("{not json", encoding="utf-8")
        with mock.patch.object(cli.config, "load", return_value=self.cfg),                 mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            code = cli.main(["watch", "--once", "--no-sync", "--agent", "cc"])
        self.assertEqual(code, 1)
        self.assertIn("rung-cc.json", err.getvalue())
        self.assertEqual(self.started, [])

    def test_missing_state_files_are_a_normal_first_run(self):
        self.assertEqual(len(cli.watch_once("cc", self.cfg, sync=False)), 1)
        self.assertEqual(self.started, [self.env["id"]])


class PollerTests(unittest.TestCase):
    def test_task_xml(self):
        xml = poller.build_task_xml("hermes", 5, r"C:\Python\pythonw.exe", r"C:\relay")
        self.assertIn("<StartWhenAvailable>true</StartWhenAvailable>", xml)
        self.assertIn("<Interval>PT5M</Interval>", xml)
        self.assertIn("watch --agent hermes --once", xml)

    def test_bad_interval(self):
        with self.assertRaises(ValueError):
            poller.build_task_xml("hermes", 0, "x", "y")


class CliTests(Base):
    def test_domain_errors_return_two_without_traceback(self):
        cases = (
            (["send", "--to", "codex", "--subject", "s"], cli.signing, "settings", cli.signing.SigningError),
            (["keygen"], cli.signing, "keygen", cli.signing.SigningError),
            (["trust", "codex", "invalid"], cli.signing, "trust", cli.signing.SigningError),
            (["gc"], cli.relay_gc, "plan", cli.relay_gc.GcError),
            (["accept", "reply-id", "--repo", "."], cli.accepter, "accept", cli.accepter.AcceptError),
        )
        cfg = {"agent": "commander", "store": str(self.dir / "store")}
        for argv, module, method, error in cases:
            with self.subTest(command=argv[0]), \
                    mock.patch.object(cli.config, "load", return_value=cfg), \
                    mock.patch.object(module, method, side_effect=error("test failure")), \
                    mock.patch("sys.stdout"), mock.patch("sys.stderr", new_callable=io.StringIO) as err:
                self.assertEqual(cli.main([*argv, "--no-sync"]), 2)
                self.assertTrue(err.getvalue().startswith("error:"), err.getvalue())
                self.assertNotIn("Traceback", err.getvalue())

    def test_version(self):
        result = subprocess.run(
            [sys.executable, "-m", "agent_relay", "--version"],
            cwd=ROOT, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), f"agent-relay {__version__}")

    def test_send_inbox_read_ack_json(self):
        store = str(self.dir / "store")
        with mock.patch.dict(os.environ, {"AGENT_RELAY_STORE": store}):
            with mock.patch("sys.stdout") as out:
                self.assertEqual(cli.main(["send", "--from", "commander", "--to", "codex", "--subject", "s", "--body", "b", "--no-sync", "--json"]), 0)
            sent = json.loads("".join(c.args[0] for c in out.write.call_args_list))
            self.assertEqual(cli.main(["read", sent["id"], "--agent", "codex", "--no-sync"]), 0)
            self.assertEqual(cli.main(["ack", sent["id"], "--agent", "codex", "--no-sync"]), 0)
            self.assertEqual(Store(store).list("codex"), [])
            self.assertEqual(cli.main(["ack", "nope", "--agent", "codex", "--no-sync"]), 2)


if __name__ == "__main__":
    unittest.main()
