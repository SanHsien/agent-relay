"""SPEC 18: plain tasks run with dispatch.default; replies are accepted, notes acked, on their own."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Never touch the real ~/.agent-relay: every test run gets its own home.
os.environ["AGENT_RELAY_HOME"] = tempfile.mkdtemp(prefix="agent-relay-test-home-")

from agent_relay import cli, dispatch, envelope  # noqa: E402
from test_accept import GOOD_WORKER, AcceptBase, git  # noqa: E402


class AutoTests(AcceptBase):
    def run_plain(self, project=None):
        env = envelope.build(sender="commander", to="claude-code", subject="plain", body="add add()", kind="task",
                             project=project)
        self.store.deliver(env)
        with mock.patch.object(dispatch, "build_argv", return_value=(["claude", "-c", GOOD_WORKER], "test", None)), \
                mock.patch.object(dispatch, "resolve_executable", return_value=sys.executable):
            report = dispatch.dispatch(env["id"], "claude-code", self.receiver_cfg, sync=False, use_default=True)
        return env, report

    def test_plain_task_uses_dispatch_default(self):
        _, report = self.run_plain(project=str(self.receiver))
        self.assertEqual(report["status"], "refused")  # no dispatch.default yet
        self.receiver_cfg["dispatch"]["default"] = {"runtime": "claude", "timeout_minutes": 1}
        env, report = self.run_plain(project=str(self.receiver))
        self.assertEqual(report["status"], "ok", report)
        self.assertTrue(report["commit"].startswith("committed:"))
        env, report = self.run_plain()  # no repo anywhere: refused, with a hint
        self.assertEqual(report["status"], "refused")
        replies = [e for _, e in self.store.list("commander") if e.get("reply_to") == env["id"]]
        self.assertIn("--project", replies[0]["body"])

    def test_plain_task_still_needs_a_trusted_sender(self):
        self.receiver_cfg["dispatch"]["default"] = {"runtime": "claude", "timeout_minutes": 1}
        self.receiver_cfg["dispatch"]["senders"] = ["someone-else"]
        _, report = self.run_plain(project=str(self.receiver))
        self.assertEqual(report["status"], "refused")

    def test_doorbell_launches_plain_tasks_only_with_a_default(self):
        env = envelope.build(sender="commander", to="claude-code", subject="plain", body="", kind="task")
        with mock.patch.object(dispatch.config, "load", return_value={"dispatch": {}}):
            self.assertEqual(dispatch.ring_dispatch(env, {}), "skipped: no dispatch section")
        with mock.patch.object(dispatch.config, "load", return_value={"dispatch": {"default": {"runtime": "claude"}}}), \
                mock.patch.object(dispatch, "launch", return_value="ok") as launch:
            dispatch.ring_dispatch(env, {})
        self.assertIn("--default", launch.call_args.args[0])

    def test_commander_accepts_on_its_own_and_acks_notes(self):
        _, reply = self.dispatch_task()
        self.commander_cfg["accept"].update(auto=True, repo_roots=[str(self.commander.parent)])
        with mock.patch.object(cli.dispatcher, "launch", return_value="ok: detached process") as launch:
            self.assertEqual(cli.automate(reply, "commander", self.commander_cfg), "auto-accept ok: detached process")
        args = launch.call_args.args[0]
        self.assertEqual(args[:4], ["accept", reply["id"], "--repo", str(self.commander)])
        self.commander_cfg["accept"]["auto"] = False
        self.assertIsNone(cli.automate(reply, "commander", self.commander_cfg))

        # The accept note the receiver gets is pure information: acked without a human.
        report = self.accept(reply)
        note = next(e for _, e in self.store.list("claude-code") if e.get("reply_to") == reply["id"])
        self.assertEqual(cli.automate(note, "claude-code", self.receiver_cfg), "acked accept note")
        self.assertEqual(self.store.list("claude-code"), [])
        self.assertEqual(report["status"], "accepted")

    def test_accept_push(self):
        _, reply = self.dispatch_task()
        report = self.accept(reply, push=True)
        self.assertEqual((report["status"], report.get("pushed")), ("accepted", "origin/main"))
        self.assertEqual(git("rev-parse", "main", cwd=self.remote), report["merge"])


if __name__ == "__main__":
    unittest.main()
