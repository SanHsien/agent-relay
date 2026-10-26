"""Second-round scan: broken input from other machines or workers must not crash or hang relay."""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

os.environ["AGENT_RELAY_HOME"] = tempfile.mkdtemp(prefix="agent-relay-test-home-")

import nostr_adapter_poc as poc  # noqa: E402
from agent_relay import dispatch, locks, status  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        patch = mock.patch.dict(os.environ, {"AGENT_RELAY_HOME": str(self.dir / "home")})
        patch.start()
        self.addCleanup(patch.stop)
        self.cfg = {"store": str(self.dir / "store"), "agent": "claude-code", "doorbells": []}


class StatusRobustnessTests(Base):
    def test_json_that_is_not_an_object_is_skipped(self):
        """A synced hosts/*.json or a run record holding `[]` must not crash `relay status`."""
        hosts = Path(self.cfg["store"]) / "hosts"
        hosts.mkdir(parents=True)
        (hosts / "bad@host.json").write_text("[]", encoding="utf-8")
        run = Path(os.environ["AGENT_RELAY_HOME"]) / "runs" / "t1"
        run.mkdir(parents=True)
        (run / "state.json").write_text("[]", encoding="utf-8")
        data = status.overview(self.cfg)
        self.assertEqual((data["hosts"], data["local_runs"]), ([], []))
        # the heartbeat of this host is also unreadable: rewrite it instead of crashing
        (hosts / f"claude-code@{status._host()}.json").write_text("[]", encoding="utf-8")
        self.assertTrue(status.heartbeat(self.cfg, "claude-code"))


class ClaimNormalizeTests(Base):
    def test_claim_without_holder_or_expires_at_is_listed_with_defaults(self):
        folder = locks.claims_dir(Path(self.cfg["store"]), "k")
        folder.mkdir(parents=True)
        claim = {"schema": locks.CLAIM_SCHEMA, "id": "c1", "paths": ["a.py"], "expires": time.time() + 600}
        (folder / "c1.json").write_text(json.dumps(claim), encoding="utf-8")
        (listed,) = locks.active_claims(Path(self.cfg["store"]), "k")
        self.assertIsInstance(listed["holder"], str)
        self.assertIsInstance(listed["expires_at"], str)


class RunStdinTests(Base):
    def test_worker_that_never_reads_stdin_still_times_out(self):
        """A prompt bigger than the pipe buffer must not block the timeout."""
        result = {}

        def go():
            result["out"] = dispatch.run([sys.executable, "-c", "import time; time.sleep(25)"], self.dir,
                                         self.dir / "o.log", 2, stdin_text="x" * 5_000_000)

        thread = threading.Thread(target=go, daemon=True)
        started = time.monotonic()
        thread.start()
        thread.join(15)
        self.assertFalse(thread.is_alive(), "run() hung on the stdin write")
        self.assertEqual(result["out"][0], "timeout")
        self.assertLess(time.monotonic() - started, 15)


class NeverStartedTests(Base):
    def test_created_at_with_an_offset_is_read_as_that_instant(self):
        local = datetime.now(timezone(timedelta(hours=-8))) - timedelta(minutes=1)
        env = {"id": "20261026T000000000Z-aaaaaa", "created_at": local.strftime("%Y-%m-%dT%H:%M:%S-08:00")}
        cfg = {"doorbells": [{"type": "dispatch"}]}
        self.assertFalse(dispatch._never_started(env, cfg, {env["id"]}, time.time()))


class NostrMalformedTests(unittest.TestCase):
    def test_malformed_events_are_refused_not_crashed(self):
        env = {"schema": "agent-relay/envelope@1", "id": "20261026T012345000Z-a1b2c3", "from": "a", "to": "b",
               "created_at": "2026-10-26T01:23:45Z", "kind": "note", "subject": "s", "body": "b"}
        event = poc.to_event(env)
        for label, broken in (("short tag", {**event, "tags": [["relay-id"]]}),
                              ("no tags", {k: v for k, v in event.items() if k != "tags"}),
                              ("not an object", [])):
            with self.subTest(label):
                self.assertFalse(poc.verify_id(broken))
                with self.assertRaises(poc.NostrPocError):
                    poc.from_event(broken)
        # a well-formed id over a tag list with a one-element tag: from_event still refuses cleanly
        short = {**event, "tags": [["relay-id"]]}
        short["id"] = poc.compute_id(short)
        with self.assertRaises(poc.NostrPocError):
            poc.from_event(short)


if __name__ == "__main__":
    unittest.main()
