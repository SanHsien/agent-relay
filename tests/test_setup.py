"""SPEC 19: relay setup-local rebuilds the commander + worker setup and is safe to run again."""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Never touch the real ~/.agent-relay: every test run gets its own home.
os.environ["AGENT_RELAY_HOME"] = tempfile.mkdtemp(prefix="agent-relay-test-home-")

from agent_relay import envelope, poller, setup, signing  # noqa: E402


@unittest.skipUnless(shutil.which("ssh-keygen") or Path(signing.WINDOWS_OPENSSH).is_file(), "ssh-keygen not installed")
class SetupLocalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = Path(self.tmp.name)
        self.commander, self.worker = d / "commander", d / "worker"
        (d / "store").mkdir()
        self.commander.mkdir()
        (self.commander / "config.json").write_text(json.dumps({"store": str(d / "store"), "agent": "commander"}),
                                                    encoding="utf-8")

    def run_setup(self, **kw):
        return setup.setup_local(self.commander, self.worker, "claude-code", ["C:/GitHub"], None, 5,
                                 install_tasks=False, **kw)

    def test_builds_everything_and_both_sides_trust_each_other(self):
        steps = self.run_setup()
        self.assertTrue(any("key created" in s for s in steps))
        worker = json.loads((self.worker / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(worker["agent"], "claude-code")
        self.assertEqual(worker["dispatch"]["senders"], ["commander"])
        self.assertTrue(worker["dispatch"]["require_verification"])
        self.assertNotIn("default", worker["dispatch"])  # no human-free mode unless asked for
        commander = json.loads((self.commander / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(commander["accept"]["verify"]["command"], "auto")
        self.assertNotIn("auto", commander["accept"])
        # A task signed by the commander verifies on the worker, a reply the other way round.
        task = signing.sign(envelope.build(sender="commander", to="claude-code", subject="t", body=""),
                            signing.default_key(self.commander, "commander"))
        self.assertIsNone(signing.verify(task, self.worker / "allowed_signers"))
        reply = signing.sign(envelope.build(sender="claude-code", to="commander", subject="r", body=""),
                             signing.default_key(self.worker, "claude-code"))
        self.assertIsNone(signing.verify(reply, self.commander / "allowed_signers"))

    def test_second_run_changes_nothing_and_keeps_owner_edits(self):
        self.run_setup()
        cfg_path = self.worker / "config.json"
        worker = json.loads(cfg_path.read_text(encoding="utf-8"))
        worker["dispatch"]["max_concurrent"] = 1  # the owner's own choice
        cfg_path.write_text(json.dumps(worker), encoding="utf-8")
        keys = {p: p.read_bytes() for p in (self.commander / "keys").iterdir()}
        trust = (self.worker / "allowed_signers").read_text(encoding="utf-8")
        steps = self.run_setup()
        self.assertTrue(all("key created" not in s for s in steps))
        self.assertEqual({p: p.read_bytes() for p in (self.commander / "keys").iterdir()}, keys)
        self.assertEqual((self.worker / "allowed_signers").read_text(encoding="utf-8"), trust)
        self.assertEqual(json.loads(cfg_path.read_text(encoding="utf-8"))["dispatch"]["max_concurrent"], 1)

    def test_auto_turns_on_the_human_free_settings(self):
        self.run_setup(auto=True)
        worker = json.loads((self.worker / "config.json").read_text(encoding="utf-8"))
        commander = json.loads((self.commander / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(worker["dispatch"]["default"]["runtime"], "claude")
        self.assertTrue(commander["accept"]["auto"])

    def test_needs_an_initialised_commander(self):
        (self.commander / "config.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            self.run_setup()

    def test_scheduled_task_arguments_carry_each_home(self):
        xml = poller.build_task_xml("claude-code", 5, "pythonw.exe", "C:/x", self.worker)
        self.assertIn(f'--home "{self.worker}"', xml.replace("&quot;", '"'))


if __name__ == "__main__":
    unittest.main()
