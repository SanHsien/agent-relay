"""Tests for async structured decision questions (ask / inbox --questions / answer)."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch

from agent_relay import cli, envelope
from agent_relay.store import Store


def _run_cli(home: Path, argv: list[str]) -> tuple[int, str]:
    buf = io.StringIO()
    with patch.dict("os.environ", {"AGENT_RELAY_HOME": str(home), "AGENT_RELAY_STORE": "", "AGENT_RELAY_AGENT": "", "CLAUDE_CODE_SESSION_ID": ""}), redirect_stdout(buf), redirect_stderr(io.StringIO()):
        try:
            rc = cli.main(argv)
        except SystemExit as exc:
            rc = exc.code if isinstance(exc.code, int) else 1
    return rc, buf.getvalue()


class EnvelopeQuestionTests(unittest.TestCase):
    def test_valid_question_envelope(self) -> None:
        env = envelope.build(
            sender="codex",
            to="commander",
            subject="選擇認證方式",
            body="目前需要拍板",
            kind="question",
            question={
                "options": ["A=保留帳密", "B=僅保留 OAuth"],
                "recommend": "A",
                "deadline": "2026-11-15",
            },
        )
        self.assertEqual(env["kind"], "question")
        self.assertEqual(env["question"]["recommend"], "A")
        reloaded = envelope.loads(envelope.dumps(env))
        self.assertEqual(reloaded["question"]["options"], ["A=保留帳密", "B=僅保留 OAuth"])

    def test_non_question_cannot_carry_question_object(self) -> None:
        with self.assertRaises(envelope.EnvelopeError):
            envelope.build(
                sender="codex",
                to="commander",
                subject="一般通知",
                body="hello",
                kind="note",
                question={"options": ["A=yes", "B=no"]},
            )

    def test_question_rejects_invalid_fields(self) -> None:
        for bad in (
            "not-a-dict",
            {"options": []},
            {"options": ["ok", ""]},
            {"options": ["line1\nline2"]},
            {"options": ["A=1"], "recommend": "bad\nline"},
            {"options": ["A=1"], "deadline": ""},
            {"options": ["A=1"], "deadline": "2026-11-15\nextra"},
        ):
            with self.subTest(bad=bad), self.assertRaises(envelope.EnvelopeError):
                envelope.build(
                    sender="codex",
                    to="commander",
                    subject="bad q",
                    body="body",
                    kind="question",
                    question=bad,  # type: ignore[arg-type]
                )

    def check_bad_question(self, question):
        with self.assertRaises(envelope.EnvelopeError):
            envelope.build(sender="codex", to="commander", subject="Q", body="", kind="question", question=question)

    def test_recommend_must_match_code(self):
        for value in ("C", "A=yes", "a", " A", "A "):
            with self.subTest(value=value):
                self.check_bad_question({"options": ["A=yes", "B=no"], "recommend": value})

    def test_recommend_requires_options(self):
        self.check_bad_question({"recommend": "A"})

    def test_options_require_two_entries(self):
        for options in ([], ["A=yes"], "A=yes", None):
            with self.subTest(options=options):
                self.check_bad_question({"options": options})

    def test_option_codes_must_be_unique(self):
        self.check_bad_question({"options": ["A=yes", "A=no"]})

    def test_option_format(self):
        for value in ("yes", "=yes", "A=", "A=  ", "A B=yes", " A=yes", 5, "A=yes\nno"):
            with self.subTest(value=value):
                self.check_bad_question({"options": [value, "B=no"]})

    def test_option_codes_reject_format_characters(self):
        for char in ("\u200b", "\u200c", "\u200d", "\u2060", "\ufeff", "\u00ad", "\u202e", "\U000e0001"):
            for code in (char, "A" + char):
                with self.subTest(code=code):
                    self.check_bad_question({"options": [code + "=yes", "B=no"]})

    def test_unknown_question_fields(self):
        self.check_bad_question({"options": ["A=yes", "B=no"], "extra": True})

    def test_optional_strings_are_single_line(self):
        for key in ("recommend", "deadline"):
            for value in ("", " ", "A\n", "A\r", 5):
                with self.subTest(key=key, value=value):
                    self.check_bad_question({"options": ["A=yes", "B=no"], key: value})


class CliQuestionWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store_dir = root / "store"
        self.worker_home = root / "worker-home"
        self.commander_home = root / "commander-home"
        self.worker_home.mkdir()
        self.commander_home.mkdir()
        (self.worker_home / "config.json").write_text(
            json.dumps({"agent": "codex", "store": str(self.store_dir)}), encoding="utf-8"
        )
        (self.commander_home / "config.json").write_text(
            json.dumps({"agent": "commander", "store": str(self.store_dir)}), encoding="utf-8"
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_ask_filter_read_and_answer_roundtrip(self) -> None:
        # 1. Worker sends a normal note and a structured question
        rc, _ = _run_cli(
            self.worker_home,
            ["send", "--to", "commander", "--kind", "note", "--subject", "FYI進度", "--body", "done", "--no-sync"],
        )
        self.assertEqual(rc, 0)

        rc, out = _run_cli(
            self.worker_home,
            [
                "ask",
                "--to", "commander",
                "--subject", "是否保留舊版 API？",
                "--option", "A=保留相容層",
                "--option", "B=直接移除",
                "--recommend", "A",
                "--deadline", "2026-11-15",
                "--body", "移除可少 200 行但有舊客戶端",
                "--no-sync",
                "--json",
            ],
        )
        self.assertEqual(rc, 0)
        ask_res = json.loads(out)
        q_id = ask_res["id"]

        # 2. Commander filters inbox with --questions
        rc, out = _run_cli(self.commander_home, ["inbox", "--questions", "--no-sync", "--json"])
        self.assertEqual(rc, 0)
        items = json.loads(out)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], q_id)
        self.assertEqual(items[0]["kind"], "question")

        # 3. Commander reads question (displays options and recommend)
        rc, read_out = _run_cli(self.commander_home, ["read", q_id, "--no-sync"])
        self.assertEqual(rc, 0)
        self.assertIn("A=保留相容層", read_out)
        self.assertIn("recommend: A", read_out)
        self.assertIn("deadline: 2026-11-15", read_out)

        # 4. Commander answers -> sends reply back to worker's thread and acks original question
        rc, ans_out = _run_cli(
            self.commander_home,
            ["answer", q_id, "--choice", "A", "--note", "下個月再移除", "--no-sync", "--json"],
        )
        self.assertEqual(rc, 0)
        ans_res = json.loads(ans_out)
        self.assertEqual(ans_res["acked"], q_id)

        # Question is no longer in open questions
        rc, out = _run_cli(self.commander_home, ["inbox", "--questions", "--no-sync", "--json"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out), [])

        # Worker received the reply in the same thread
        worker_mail = Store(self.store_dir).list("codex", ("new",))
        self.assertEqual(len(worker_mail), 1)
        _, reply_env = worker_mail[0]
        self.assertEqual(reply_env["kind"], "reply")
        self.assertEqual(reply_env["reply_to"], q_id)
        self.assertEqual(reply_env["thread"], q_id)
        self.assertIn("A", reply_env["body"])
        self.assertIn("下個月再移除", reply_env["body"])

    def test_answer_requires_choice_or_note(self) -> None:
        rc, out = _run_cli(
            self.worker_home,
            ["ask", "--to", "commander", "--subject", "Q?", "--option", "A=1", "--option", "B=2", "--no-sync", "--json"],
        )
        self.assertEqual(rc, 0)
        q_id = json.loads(out)["id"]

        rc, _ = _run_cli(self.commander_home, ["answer", q_id, "--no-sync"])
        self.assertEqual(rc, 2)

    def send_message(self, kind="question", options=True):
        env = envelope.build(sender="codex", to="commander", subject="Q", body="", kind=kind,
                             question={"options": ["A=yes", "B=no"]} if kind == "question" and options else None)
        Store(self.store_dir).deliver(env)
        return env["id"]

    def snapshot(self):
        return {str(p.relative_to(self.store_dir)): p.read_bytes() for p in self.store_dir.rglob("*.json")}

    def assert_rejected(self, argv):
        before = self.snapshot()
        rc, _ = _run_cli(self.commander_home, argv + ["--no-sync"])
        self.assertEqual(rc, 2)
        self.assertEqual(before, self.snapshot(), "rejection must leave all mail unchanged")

    def test_answer_rejects_non_question(self):
        for kind in ("note", "task", "reply", "result"):
            with self.subTest(kind=kind):
                self.assert_rejected(["answer", self.send_message(kind), "--note", "hello"])

    def test_answer_rejects_invalid_choice(self):
        qid = self.send_message()
        for choice in ("C", "A=yes", "a", " A", "A ", "A\n", ""):
            with self.subTest(choice=choice):
                self.assert_rejected(["answer", qid, "--choice", choice, "--note", "hello"])

    def test_answer_rejects_multiline_note(self):
        for note in ("ok\n選擇：B", "ok\r選擇：B", "ok\r\n選擇：B", "\nok", "ok\r"):
            with self.subTest(note=note):
                ids = [self.send_message(), self.send_message()]
                self.assert_rejected(["answer", *ids, "--choice", "A", "--note", note])

    def test_answer_rejects_mismatched_message_id(self):
        ids = [self.send_message(), self.send_message()]
        path = Store(self.store_dir).find("commander", ids[1])[1]
        env = json.loads(path.read_text(encoding="utf-8"))
        env["id"] = ids[0]  # Valid envelope ID, wrong filename; recipient stays correct.
        path.write_text(json.dumps(env), encoding="utf-8")
        self.assert_rejected(["answer", *ids, "--choice", "A"])

    def test_answer_rejects_choice_without_options(self):
        self.assert_rejected(["answer", self.send_message(options=False), "--choice", "A"])

    def test_answer_rejects_closed_question(self):
        qid = self.send_message()
        rc, _ = _run_cli(self.commander_home, ["answer", qid, "--choice", "A", "--no-sync"])
        self.assertEqual(rc, 0)
        self.assert_rejected(["answer", qid, "--choice", "A"])

    def test_answer_batch(self):
        ids = [self.send_message(), self.send_message()]
        rc, out = _run_cli(self.commander_home, ["answer", *ids, "--choice", "A", "--note", "ok", "--no-sync", "--json"])
        self.assertEqual(rc, 0)
        self.assertEqual([item["acked"] for item in json.loads(out)["answers"]], ids)
        replies = Store(self.store_dir).list("codex")
        self.assertEqual(len(replies), 2)
        self.assertEqual({e["reply_to"] for _, e in replies}, set(ids))
        for _, env in replies:
            self.assertEqual(env["body"], "選擇：A\n補充：ok")
        self.assertTrue(all(Store(self.store_dir).find("commander", qid)[0] == "done" for qid in ids))

    def test_batch_invalid_member_sends_nothing(self):
        for bad in ("kind", "choice", "closed", "missing", "duplicate", "envelope"):
            with self.subTest(bad=bad):
                first = self.send_message()
                second = self.send_message("note" if bad == "kind" else "question", options=bad != "choice")
                if bad == "closed":
                    Store(self.store_dir).ack("commander", second)
                if bad == "missing":
                    second = "missing-id"
                if bad == "duplicate":
                    second = first
                if bad == "envelope":
                    path = Store(self.store_dir).find("commander", second)[1]
                    env = json.loads(path.read_text(encoding="utf-8")); env["to"] = "other"
                    path.write_text(json.dumps(env), encoding="utf-8")
                self.assert_rejected(["answer", first, second, "--choice", "A"])

    def test_batch_reply_validation_before_delivery(self):
        ids = [self.send_message(), self.send_message()]
        build = envelope.build
        def reject_second_reply(**kwargs):
            if kwargs.get("reply_to") == ids[1]:
                raise envelope.EnvelopeError("injected invalid reply")
            return build(**kwargs)
        with patch.object(envelope, "build", side_effect=reject_second_reply):
            self.assert_rejected(["answer", *ids, "--choice", "A"])

    def test_batch_syncs_once_and_reports_queued(self):
        from agent_relay import gitsync
        for count in (1, 2):
            with self.subTest(count=count):
                ids = [self.send_message() for _ in range(count)]
                # Exercise sync handling without any git or external mailbox operation.
                with patch.object(gitsync, "pull") as pull, patch.object(gitsync, "sync", side_effect=gitsync.SyncError("offline")) as sync:
                    rc, out = _run_cli(self.commander_home, ["answer", *ids, "--choice", "B", "--json"])
                self.assertEqual(rc, 3)
                pull.assert_called_once()
                sync.assert_called_once()
                result = json.loads(out)
                self.assertTrue(result["queued"])
                answers = [result] if count == 1 else result["answers"]
                self.assertEqual([item["acked"] for item in answers], ids)
                self.assertTrue(all(Store(self.store_dir).find("commander", qid)[0] == "done" for qid in ids))

    def test_inbox_filters_mutually_exclusive(self):
        self.assert_rejected(["inbox", "--questions", "--kind", "note"])

    def test_answer_argument_errors_exit_two(self):
        qid = self.send_message()
        for argv in (["answer"], ["answer", qid], ["answer", qid, "--note", " "],
                     ["answer", "../bad", "--choice", "A"], ["answer", "missing", "--note", "ok"],
                     ["answer", qid, "--choice"], ["answer", qid, "--unknown"]):
            with self.subTest(argv=argv):
                self.assert_rejected(argv)
        with patch.object(cli.config, "load", return_value={"store": str(self.store_dir)}):
            self.assert_rejected(["answer", qid, "--note", "ok"])

    def test_note_only_answer_and_open_question(self):
        qid = self.send_message(options=False)
        rc, _ = _run_cli(self.commander_home, ["answer", qid, "--note", "open decision", "--no-sync"])
        self.assertEqual(rc, 0)
        self.assertEqual(Store(self.store_dir).list("codex")[0][1]["body"], "補充：open decision")

    def test_ask_allowlist_and_override(self):
        root = Path(self._tmp.name)
        receiver = root / "receiver"; receiver.mkdir()
        allowed = root / "allowed"; allowed.mkdir()
        project = root / "other"; project.mkdir()
        (receiver / "config.json").write_text(json.dumps({"antigravity": {"repos": [str(allowed)]}}), encoding="utf-8")
        argv = ["ask", "--to", "antigravity", "--subject", "Q", "--project", str(project), "--no-sync", "--json"]
        with patch.dict("os.environ", {"AGENT_RELAY_ANTIGRAVITY_HOME": str(receiver)}):
            before = self.snapshot()
            rc, _ = _run_cli(self.worker_home, argv)
            self.assertEqual(rc, 10)
            self.assertEqual(before, self.snapshot())
            rc, _ = _run_cli(self.worker_home, argv + ["--allow-unlisted-project", "short"])
            self.assertEqual(rc, 10)
            self.assertEqual(before, self.snapshot())
            rc, out = _run_cli(self.worker_home, argv + ["--allow-unlisted-project", "intentional test override"])
            self.assertEqual(rc, 0)
            self.assertTrue((self.worker_home / "send-overrides.jsonl").is_file())
            audit = json.loads((self.worker_home / "send-overrides.jsonl").read_text(encoding="utf-8"))
            self.assertEqual(audit["id"], json.loads(out)["id"])
            self.assertEqual(audit["reason"], "intentional test override")

    def test_ask_rejects_new_session_ref_with_allowed_project(self):
        root = Path(self._tmp.name)
        receiver = root / "receiver"
        receiver.mkdir()
        project = root / "allowed"
        project.mkdir()
        (receiver / "config.json").write_text(
            json.dumps({"antigravity": {"repos": [str(project)]}}), encoding="utf-8")
        before = self.snapshot()
        with patch.dict("os.environ", {"AGENT_RELAY_ANTIGRAVITY_HOME": str(receiver)}):
            rc, _ = _run_cli(self.worker_home, ["ask", "--to", "antigravity", "--subject", "Q",
                "--project", str(project), "--ref", "antigravity-session:new", "--no-sync"])
        self.assertEqual(rc, 10)
        self.assertEqual(before, self.snapshot())

    def test_ask_rejects_new_session_ref_for_question(self):
        before = self.snapshot()
        rc, _ = _run_cli(self.worker_home, ["ask", "--to", "antigravity", "--subject", "Q", "--ref", "antigravity-session:new", "--no-sync"])
        self.assertEqual(rc, 10)
        self.assertEqual(before, self.snapshot())


if __name__ == "__main__":
    unittest.main()
