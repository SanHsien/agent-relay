"""Regression tests for the 2026-10-20 review findings."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent_relay import cli, doorbell, envelope, gitsync  # noqa: E402
from agent_relay.store import Store, StoreError  # noqa: E402

NL, CR = chr(10), chr(13)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        patch = mock.patch.dict(os.environ, {"AGENT_RELAY_HOME": str(self.dir / "home")})
        patch.start()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(patch.stop)


class NameTests(unittest.TestCase):
    """Finding 1 and 2: trailing newline, trailing dot, Windows device names."""

    def test_rejects_unsafe_names(self):
        for bad in ("codex" + NL, "codex.", "nul", "CON", "com1", "lpt9.txt", ".hidden", ""):
            with self.subTest(bad=bad), self.assertRaises(envelope.EnvelopeError):
                envelope.build(sender="commander", to=bad, subject="x", body="")

    def test_rejects_unsafe_ids(self):
        env = envelope.build(sender="a", to="b", subject="x", body="")
        for bad in ("abc" + NL, "abc.", "aux", "../x"):
            env["id"] = bad
            with self.subTest(bad=bad), self.assertRaises(envelope.EnvelopeError):
                envelope.validate(env)

    def test_accepts_normal_names(self):
        for good in ("codex", "claude-code", "agent_1", "a.b"):
            envelope.build(sender="commander", to=good, subject="x", body="")

    def test_store_refuses_unsafe_lookup(self):
        with self.assertRaises(StoreError):
            Store("x").box("codex.", "new")


class LineTests(unittest.TestCase):
    """Finding 3: subject/host/project must be one line; refs and created_at are typed."""

    def test_multiline_fields_rejected(self):
        for key in ("subject", "host", "project"):
            for breaker in (NL, CR):
                env = envelope.build(sender="a", to="b", subject="x", body="")
                env[key] = "line" + breaker + "status: done"
                with self.subTest(key=key), self.assertRaises(envelope.EnvelopeError):
                    envelope.validate(env)

    def test_refs_and_created_at_typed(self):
        env = envelope.build(sender="a", to="b", subject="x", body="")
        env["refs"] = "not-a-list"
        with self.assertRaises(envelope.EnvelopeError):
            envelope.validate(env)
        env["refs"] = []
        env["created_at"] = "yesterday"
        with self.assertRaises(envelope.EnvelopeError):
            envelope.validate(env)


class StoreIntegrityTests(Base):
    """Finding 2: a file that lies about its id or mailbox is skipped."""

    def test_mismatched_files_skipped(self):
        store = Store(self.dir / "store")
        real = envelope.build(sender="a", to="codex", subject="real", body="")
        store.deliver(real)
        box = store.box("codex", "new")
        borrowed = dict(real, subject="borrowed")
        (box / "other-name.json").write_text(json.dumps(borrowed), encoding="utf-8")
        wrong_box = envelope.build(sender="a", to="hermes", subject="elsewhere", body="")
        (box / f"{wrong_box['id']}.json").write_text(json.dumps(wrong_box), encoding="utf-8")
        self.assertEqual([e["subject"] for _, e in store.list("codex")], ["real"])


class HandoffTests(Base):
    """Findings 3, 5, 6: no mail text in the reply command, opt-in roots, one card per id."""

    def setUp(self):
        super().setUp()
        self.project = self.dir / "proj"
        self.project.mkdir()

    def task(self, subject="fix"):
        return envelope.build(sender="commander", to="cc", subject=subject, body="b", kind="task",
                              project=str(self.project))

    def test_subject_not_pasted_into_command(self):
        env = self.task(subject='x" ; Remove-Item -Recurse ~ ; "')
        card = doorbell.handoff_card(env, self.project)
        command_line = next(line for line in card.splitlines() if "agent_relay send" in line)
        self.assertNotIn("Remove-Item", command_line)

    def test_outside_project_roots_refused(self):
        bell = {"type": "handoff", "project_roots": [str(self.dir / "allowed")]}
        result = doorbell.ring(self.task(), [bell])["0:handoff"]
        self.assertTrue(result.startswith("error"), result)
        self.assertFalse((self.project / ".claude").exists())
        no_roots = doorbell.ring(self.task(), [{"type": "handoff"}])["0:handoff"]
        self.assertTrue(no_roots.startswith("error"), no_roots)

    def test_rering_does_not_duplicate_card(self):
        bell = {"type": "handoff", "project_roots": [str(self.dir)]}
        env = self.task()
        doorbell.ring(env, [bell])
        doorbell.ring(env, [bell])
        self.assertEqual(len(list((self.project / ".claude" / "handoffs").glob("*.md"))), 1)


class CommandTests(unittest.TestCase):
    """Finding 4: batch files would let cmd.exe re-parse mail text."""

    def test_batch_files_refused(self):
        env = envelope.build(sender="a", to="b", subject="a & calc", body="")
        for prog in ("notify.cmd", "C:/x/NOTIFY.BAT"):
            result = doorbell.ring(env, [{"type": "command", "argv": [prog, "{subject}"]}])["0:command"]
            self.assertIn(".bat/.cmd", result)


class RungTests(Base):
    """Finding 6: rung is saved per message and atomically."""

    def test_saved_after_each_message(self):
        cfg = {"store": str(self.dir / "store"), "agent": "cc", "doorbells": [{"type": "toast"}]}
        store = Store(cfg["store"])
        first = envelope.build(sender="a", to="cc", subject="1", body="")
        second = envelope.build(sender="a", to="cc", subject="2", body="")
        store.deliver(first)
        store.deliver(second)
        calls = []

        def ring(env, bells):
            calls.append(env["id"])
            if len(calls) == 2:
                raise KeyboardInterrupt  # the run is killed while ringing the second message
            return {}

        with mock.patch.object(doorbell, "ring", ring), self.assertRaises(KeyboardInterrupt):
            cli.watch_once("cc", cfg, sync=False)
        self.assertEqual(doorbell.load_rung("cc"), calls[:1])
        self.assertEqual(sorted(calls), sorted([first["id"], second["id"]]))
        self.assertEqual(list((self.dir / "home").glob(".rung-*")), [])


class GitTests(Base):
    """Findings 7, 8, 10: timeouts become SyncError, fresh clone sync, temp files never committed."""

    def init_repo(self):
        repo = self.dir / "repo"
        subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
        for key, value in (("user.email", "t@example.com"), ("user.name", "t")):
            subprocess.run(["git", "-C", str(repo), "config", key, value], check=True)
        return repo

    def test_timeout_is_sync_error(self):
        with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("git", 1)):
            with self.assertRaises(gitsync.SyncError):
                gitsync._git(self.dir, "status")

    def test_commit_without_mailboxes_folder(self):
        self.assertFalse(gitsync.commit(self.init_repo(), "nothing yet"))

    def test_temp_files_not_committed(self):
        repo = self.init_repo()
        Store(repo).deliver(envelope.build(sender="a", to="b", subject="x", body=""))
        (repo / "mailboxes" / "b" / "new" / ".tmp-half.json").write_text("{", encoding="utf-8")
        self.assertTrue(gitsync.commit(repo, "m"))
        tracked = subprocess.run(["git", "-C", str(repo), "ls-files"], capture_output=True, text=True).stdout
        self.assertNotIn(".tmp-half", tracked)


class CliTests(Base):
    """Findings 9 and 10: queued send on push failure, reply keeps the original thread."""

    def run_cli(self, *argv):
        with mock.patch("sys.stdout") as out:
            code = cli.main([*argv, "--json"])
        return code, json.loads("".join(c.args[0] for c in out.write.call_args_list) or "null")

    def test_reply_chain_keeps_thread(self):
        store = str(self.dir / "store")
        with mock.patch.dict(os.environ, {"AGENT_RELAY_STORE": store}):
            _, first = self.run_cli("send", "--from", "commander", "--to", "w", "--subject", "1", "--no-sync")
            _, reply = self.run_cli("send", "--from", "w", "--to", "commander", "--subject", "2",
                                    "--reply-to", first["id"], "--no-sync")
            self.run_cli("send", "--from", "commander", "--to", "w", "--subject", "3",
                         "--reply-to", reply["id"], "--no-sync")
            # The commander's copy of the reply lives in its own mailbox, so the chain resolves.
            threads = [e["thread"] for _, e in Store(store).list("w")]
            self.assertEqual(threads, [first["id"], first["id"]])

    def test_push_failure_reports_queued(self):
        store = str(self.dir / "store")
        with mock.patch.dict(os.environ, {"AGENT_RELAY_STORE": store}), \
                mock.patch.object(gitsync, "pull"), \
                mock.patch.object(gitsync, "sync", side_effect=gitsync.SyncError("push rejected")):
            code, out = self.run_cli("send", "--from", "a", "--to", "b", "--subject", "s")
        self.assertEqual(code, 3)
        self.assertTrue(out["queued"])
        self.assertEqual(len(Store(store).list("b")), 1)


if __name__ == "__main__":
    unittest.main()
