"""T-20261021-10: relay status and receiver heartbeats."""

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


if __name__ == "__main__":
    unittest.main()
