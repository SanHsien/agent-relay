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

from agent_relay import cli, doorbell, envelope, gitsync, poller  # noqa: E402
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

    def test_command_placeholders_are_not_shell(self):
        env = envelope.build(sender="a", to="b", subject="x; rm -rf /", body="")
        out = self.dir / "out.txt"
        bell = {"type": "command", "argv": [sys.executable, "-c", "import sys; open(sys.argv[1],'w').write(sys.argv[2])", str(out), "{subject}"]}
        self.assertEqual(doorbell.ring(env, [bell])["0:command"], "ok")
        self.assertEqual(out.read_text(), "x; rm -rf /")


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
