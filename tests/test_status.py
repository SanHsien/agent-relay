"""T-20261021-10: relay status and receiver heartbeats."""

import io
import json
import os
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

from agent_relay import cli, dispatch, envelope, status  # noqa: E402
from agent_relay.store import Store  # noqa: E402


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        patch = mock.patch.dict(os.environ, {"AGENT_RELAY_HOME": str(self.dir / "home")})
        patch.start()
        self.addCleanup(patch.stop)
        self.cfg = {"store": str(self.dir / "store"), "agent": "claude-code", "doorbells": []}
        self.beat = Path(self.cfg["store"]) / "hosts" / f"claude-code@{status._host()}.json"

    def test_heartbeat_only_when_due_or_changed(self):
        now = time.time()
        self.assertTrue(status.heartbeat(self.cfg, "claude-code", now))
        data = json.loads(self.beat.read_text(encoding="utf-8"))
        self.assertEqual((data["agent"], data["running"], data["queued"]), ("claude-code", [], []))
        self.assertFalse(status.heartbeat(self.cfg, "claude-code", now + 60))  # nothing new
        self.assertTrue(status.heartbeat(self.cfg, "claude-code", now + 31 * 60))  # interval passed
        dispatch.write_state("t1", phase="running", pid=os.getpid(), started=now, deadline=now + 600)
        self.assertTrue(status.heartbeat(self.cfg, "claude-code", now + 32 * 60))  # a run started
        self.assertEqual(json.loads(self.beat.read_text(encoding="utf-8"))["running"], ["t1"])

    def test_overview_flags_a_stopped_receiver_and_overdue_runs(self):
        now = time.time()
        status.heartbeat(self.cfg, "claude-code", now - 3 * 3600)
        Store(self.cfg["store"]).deliver(envelope.build(sender="commander", to="claude-code", subject="s", body=""))
        dispatch.write_state("late", phase="running", pid=os.getpid(), started=now - 7200, deadline=now - 60)
        data = status.overview(self.cfg, stale_minutes=90, now=now)
        self.assertEqual(data["mailboxes"], {"claude-code": {"new": 1, "cur": 0}})
        self.assertTrue(data["hosts"][0]["stale"])
        self.assertEqual(data["overdue"], ["late"])
        self.assertIn("心跳逾時", status.render(data))
        status.heartbeat(self.cfg, "claude-code", now)  # the receiver came back
        self.assertFalse(status.overview(self.cfg, stale_minutes=90, now=now)["hosts"][0]["stale"])

    def test_watch_writes_the_heartbeat(self):
        cli.watch_once("claude-code", self.cfg, sync=False)
        self.assertTrue(self.beat.is_file())


class UndeliveredTests(unittest.TestCase):
    """2026-11-22: two mails sat in antigravity/new for 3+ hours while status said all ok."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.recv = self.dir / "recv-home"
        (self.recv / "app-delivery").mkdir(parents=True)
        (self.recv / "config.json").write_text("{}", encoding="utf-8")
        patch = mock.patch.dict(os.environ, {"AGENT_RELAY_HOME": str(self.dir / "home"),
                                             "AGENT_RELAY_ANTIGRAVITY_HOME": str(self.recv)})
        patch.start()
        self.addCleanup(patch.stop)
        self.cfg = {"store": str(self.dir / "store"), "agent": "commander", "doorbells": []}
        self.now = time.time()

    def mail(self, minutes_ago, to="antigravity"):
        env = envelope.build(sender="commander", to=to, subject="s", body="")
        env["created_at"] = status._iso(self.now - minutes_ago * 60)
        Store(self.cfg["store"]).deliver(env)
        return env["id"]

    def receipt(self, mail_id, value, skipped=False):
        folder = self.recv / "app-delivery" / ("skipped" if skipped else "")
        folder.mkdir(exist_ok=True)
        (folder / f"{mail_id}.json").write_text(json.dumps(value), encoding="utf-8")

    def overview(self, **kw):
        return status.overview(self.cfg, now=self.now, **kw)

    def test_old_antigravity_mail_without_receipt_is_undelivered(self):
        stuck = self.mail(200)
        self.mail(5)  # younger than one or two sidecar polls: not yet suspicious
        data = self.overview(undelivered_minutes=30)
        self.assertEqual([item["id"] for item in data["undelivered"]], [stuck])
        self.assertEqual(data["undelivered"][0]["mailbox"], "antigravity")
        self.assertIn("未投遞", status.render(data))
        self.assertIn(stuck, status.render(data))

    def test_notified_receipt_counts_as_delivered_but_skipped_does_not(self):
        ok, skipped = self.mail(200), self.mail(200)
        self.receipt(ok, {"status": "notified"})
        self.receipt(skipped, {"status": "skipped", "reason": "project harness-guard is not in antigravity.repos"},
                     skipped=True)
        data = self.overview(undelivered_minutes=30)
        self.assertEqual([item["id"] for item in data["undelivered"]], [skipped])
        self.assertIn("harness-guard", data["undelivered"][0]["reason"])

    def test_unreadable_receiver_home_is_unchecked_not_empty(self):
        self.mail(200)
        (self.recv / "config.json").unlink()
        data = self.overview(undelivered_minutes=30)
        self.assertIsNone(data["undelivered"])
        self.assertIn("未檢查", status.render(data))

    def test_status_exits_non_zero_on_undelivered_mail(self):
        self.mail(200)
        with mock.patch.dict(os.environ, {"AGENT_RELAY_STORE": self.cfg["store"], "AGENT_RELAY_AGENT": "commander"}), \
                mock.patch("sys.stdout", new=io.StringIO()):
            code = cli.main(["status", "--no-sync", "--undelivered-minutes", "30"])
        self.assertEqual(code, 8)


if __name__ == "__main__":
    unittest.main()
