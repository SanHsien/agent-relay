"""Tests for async structured decision questions (ask / inbox --questions / answer)."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from agent_relay import cli, envelope
from agent_relay.store import Store


def _run_cli(home: Path, argv: list[str]) -> tuple[int, str]:
    buf = io.StringIO()
    with patch.dict("os.environ", {"AGENT_RELAY_HOME": str(home)}), redirect_stdout(buf):
        rc = cli.main(argv)
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
        self.assertIn("recommend", read_out.lower())

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
            ["ask", "--to", "commander", "--subject", "Q?", "--option", "A=1", "--no-sync", "--json"],
        )
        self.assertEqual(rc, 0)
        q_id = json.loads(out)["id"]

        with self.assertRaises(SystemExit):
            _run_cli(self.commander_home, ["answer", q_id, "--no-sync"])


if __name__ == "__main__":
    unittest.main()
