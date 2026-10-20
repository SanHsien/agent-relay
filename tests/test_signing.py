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

from agent_relay import envelope, signing  # noqa: E402

NEEDS_SSH = unittest.skipUnless(shutil.which("ssh-keygen"), "ssh-keygen not installed")


@NEEDS_SSH
class SigningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)
        cls.key = cls.dir / "commander_ed25519"
        cls.other = cls.dir / "mallory_ed25519"
        cls.pub = signing.keygen(cls.key, "commander")
        cls.other_pub = signing.keygen(cls.other, "mallory")
        cls.allowed = cls.dir / "allowed_signers"
        signing.trust(cls.allowed, "commander", cls.pub)
        signing.trust(cls.allowed, "mallory", cls.other_pub)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def env(self, sender="commander", **kw):
        return envelope.build(sender=sender, to="claude-code", subject="s", body="中文" + chr(10) + "b", **kw)

    def test_valid_signature_survives_storage(self):
        signed = signing.sign(self.env(kind="task", dispatch={"runtime": "claude", "repo": "C:/x",
                                                               "paths": ["src/"]}), self.key)
        self.assertIsNone(signing.verify(signed, self.allowed))
        self.assertIsNone(signing.verify(envelope.loads(envelope.dumps(signed)), self.allowed))

    def test_rejections(self):
        env = self.env()
        self.assertEqual(signing.verify(env, self.allowed), "unsigned")
        signed = signing.sign(env, self.key)
        for field, value in (("body", "evil"), ("to", "codex"), ("id", "20990101T000000000Z-aaaaaa")):
            with self.subTest(field=field):
                self.assertIn("bad signature", signing.verify({**signed, field: value}, self.allowed))
        # mallory's key is trusted, but only for the mailbox mallory: it cannot speak as commander.
        forged = signing.sign(env, self.other)
        self.assertIn("bad signature", signing.verify(forged, self.allowed))
        self.assertIsNone(signing.verify(signing.sign(self.env(sender="mallory"), self.other), self.allowed))
        self.assertIn("no allowed_signers", signing.verify(signed, self.dir / "missing"))

    def test_trust_is_idempotent_and_checks_the_key(self):
        before = self.allowed.read_text(encoding="utf-8")
        signing.trust(self.allowed, "commander", self.pub)
        self.assertEqual(self.allowed.read_text(encoding="utf-8"), before)
        with self.assertRaises(signing.SigningError):
            signing.trust(self.allowed, "x", "not a key")

    def test_keygen_never_overwrites(self):
        with self.assertRaises(signing.SigningError):
            signing.keygen(self.key, "commander")

    def test_settings(self):
        home = self.dir / "home"
        self.assertEqual(signing.settings({"agent": "a"}, home), (None, home / "allowed_signers"))
        key, allowed = signing.settings({"signing": {"key": str(self.key), "allowed_signers": str(self.allowed)}}, home)
        self.assertEqual((key, allowed), (self.key, self.allowed))


if __name__ == "__main__":
    unittest.main()
