"""`sender_session`: which session dispatched a mail (optional, signed like every other field)."""

import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_relay import cli, envelope, signing
from agent_relay.store import Store

UUID = "9c2e74fe-1c31-4ea5-8892-473a077b46ae"


def build(**kw):
    return envelope.build(sender="commander", to="codex", subject="s", body="b", **kw)


class EnvelopeFieldTests(unittest.TestCase):
    def test_absent_unless_given_so_old_mail_shape_is_unchanged(self):
        self.assertNotIn("sender_session", build())
        self.assertNotIn("sender_session", build(sender_session=""))
        self.assertNotIn("sender_session", build(sender_session=None))

    def test_round_trips_through_storage_text(self):
        env = build(sender_session=UUID)
        self.assertEqual(envelope.loads(envelope.dumps(env))["sender_session"], UUID)

    def test_mail_with_and_without_the_field_are_both_accepted(self):
        envelope.validate(build())
        envelope.validate(build(sender_session=UUID))

    def test_bounds_and_character_set(self):
        for good in (UUID, "a", "x" * 128, "asr:session.name_1@host"):
            envelope.validate(build(sender_session=good))
        for bad in ("x" * 129, "has space", "line\nbreak", "tab\there", "中文", "\x00", "a\r"):
            with self.assertRaises(envelope.EnvelopeError, msg=repr(bad)):
                build(sender_session=bad)

    def test_wrong_type_in_a_received_envelope_is_rejected(self):
        env = build()
        for bad in (7, ["a"], {"id": "a"}, ""):
            env["sender_session"] = bad
            with self.assertRaises(envelope.EnvelopeError, msg=repr(bad)):
                envelope.validate(env)

    def test_secret_shaped_values_are_refused(self):
        for bad in ("sk-abcdefghijklmnopqrstuvwxyz0123", "ghp_" + "a" * 30, "token=abc123"):
            with self.assertRaises(envelope.EnvelopeError, msg=bad):
                build(sender_session=bad)

    def test_unknown_to_older_readers_means_ignored_not_an_error(self):
        # An older reader's validate() has no sender_session rule and tolerates unknown fields (SPEC section 1).
        env = build(sender_session=UUID)
        env["future_field"] = "kept"
        envelope.validate(env)


class SendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.store = root / "store"
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_SESSION_ID"}
        env.update(AGENT_RELAY_HOME=str(root / "home"), AGENT_RELAY_STORE=str(self.store),
                   AGENT_RELAY_AGENT="commander")
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def send(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["send", "--to", "codex", "--subject", "s", "--body", "b", "--no-sync", "--json", *extra])
        return code, out.getvalue(), err.getvalue()

    def stored(self):
        ((_, mail),) = Store(str(self.store)).list("codex")
        return mail

    def test_flag_is_recorded(self):
        self.assertEqual(self.send("--from-session", UUID)[0], 0)
        self.assertEqual(self.stored()["sender_session"], UUID)

    def test_environment_is_the_fallback(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": UUID}):
            self.assertEqual(self.send()[0], 0)
        self.assertEqual(self.stored()["sender_session"], UUID)

    def test_flag_wins_over_environment(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "from-env"}):
            self.assertEqual(self.send("--from-session", "from-flag")[0], 0)
        self.assertEqual(self.stored()["sender_session"], "from-flag")

    def test_no_flag_no_environment_means_no_field(self):
        self.assertEqual(self.send()[0], 0)
        self.assertNotIn("sender_session", self.stored())

    def test_bad_environment_value_is_ignored_with_a_warning_and_never_blocks_the_send(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "has space"}):
            code, _, err = self.send()
        self.assertEqual(code, 0)
        self.assertIn("ignoring CLAUDE_CODE_SESSION_ID", err)
        self.assertNotIn("sender_session", self.stored())

    def test_bad_explicit_flag_fails_and_writes_nothing(self):
        code, _, err = self.send("--from-session", "line\nbreak")
        self.assertEqual(code, 2)
        self.assertIn("sender_session", err)
        self.assertEqual(Store(str(self.store)).list("codex"), [])


@unittest.skipUnless(shutil.which("ssh-keygen"), "ssh-keygen not installed")
class SignatureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)
        cls.key = cls.dir / "commander_ed25519"
        pub = signing.keygen(cls.key, "commander")
        cls.allowed = cls.dir / "allowed_signers"
        signing.trust(cls.allowed, "commander", pub)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_field_is_inside_the_signed_bytes(self):
        env = build(sender_session=UUID)
        self.assertIn(UUID.encode(), signing.canonical(env))

    def test_signed_mail_with_the_field_verifies_after_a_storage_round_trip(self):
        signed = signing.sign(build(sender_session=UUID), self.key)
        self.assertIsNone(signing.verify(envelope.loads(envelope.dumps(signed)), self.allowed))

    def test_changing_or_removing_or_adding_the_field_breaks_the_signature(self):
        signed = signing.sign(build(sender_session=UUID), self.key)
        changed = {**signed, "sender_session": "someone-else"}
        removed = {k: v for k, v in signed.items() if k != "sender_session"}
        self.assertTrue(signing.verify(changed, self.allowed).startswith("bad signature"))
        self.assertTrue(signing.verify(removed, self.allowed).startswith("bad signature"))
        plain = signing.sign(build(), self.key)
        self.assertTrue(signing.verify({**plain, "sender_session": UUID}, self.allowed).startswith("bad signature"))

    def test_signed_mail_without_the_field_still_verifies(self):
        self.assertIsNone(signing.verify(signing.sign(build(), self.key), self.allowed))


if __name__ == "__main__":
    unittest.main()
