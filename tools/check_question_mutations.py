"""Remove question guards one at a time in a temporary copy; require assertion failures."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
ENV = "agent_relay/envelope.py"
CLI = "agent_relay/cli.py"
E = "tests.test_questions.EnvelopeQuestionTests."
C = "tests.test_questions.CliQuestionWorkflowTests."

# (name, file, exact source, replacement, focused regression)
MUTANTS = [
    ("ask question kind", CLI, 'checked = _send_allowlist(args, cfg, "question")',
     'checked = _send_allowlist(args, cfg, "task")', C + "test_ask_rejects_new_session_ref_with_allowed_project"),
    ("message id half-check", CLI, 'if orig["id"] != env_id or orig["to"] != agent:',
     'if orig["to"] != agent:', C + "test_answer_rejects_mismatched_message_id"),
    ("single-line note", CLI, 'if "\\n" in note or "\\r" in note:',
     'if False:', C + "test_answer_rejects_multiline_note"),
    ("format characters", ENV, 'if any(unicodedata.category(ch) == "Cf" for ch in code):',
     'if False:', E + "test_option_codes_reject_format_characters"),
    ("known fields", ENV, 'if set(q) - {"options", "recommend", "deadline"}:',
     "if False:", E + "test_unknown_question_fields"),
    ("two options", ENV, "len(opts) < 2", "len(opts) < 1", E + "test_options_require_two_entries"),
    ("option format", ENV,
     'if not sep or not code or any(ch.isspace() for ch in code) or not description.strip():',
     "if False:", E + "test_option_format"),
    ("unique codes", ENV, "if code in codes:", "if False:", E + "test_option_codes_must_be_unique"),
    ("recommend membership", ENV, 'if "recommend" in q and q["recommend"] not in codes:',
     "if False:", E + "test_recommend_must_match_code"),
    ("recommend needs options", ENV, 'if "recommend" in q and q["recommend"] not in codes:',
     'if "options" in q and "recommend" in q and q["recommend"] not in codes:',
     E + "test_recommend_requires_options"),
    ("answer required", CLI, "if not choice and not note:", "if False:", C + "test_answer_requires_choice_or_note"),
    ("duplicate ids", CLI, "if len(set(args.id)) != len(args.id):", "if False:", C + "test_batch_invalid_member_sends_nothing"),
    ("agent required", CLI,
     'raise envelope.EnvelopeError("answer needs --agent or a configured agent")',
     'raise SystemExit("missing agent")', C + "test_answer_argument_errors_exit_two"),
    ("message identity", CLI, 'if orig["id"] != env_id or orig["to"] != agent:',
     "if False:", C + "test_batch_invalid_member_sends_nothing"),
    ("question kind", CLI, 'if orig["kind"] != "question":', "if False:", C + "test_answer_rejects_non_question"),
    ("open question", CLI, 'if state == "done":', "if False:", C + "test_answer_rejects_closed_question"),
    ("choice membership", CLI, "if choice is not None and choice not in codes:",
     "if False:", C + "test_answer_rejects_invalid_choice"),
    ("open question choice", CLI, "if choice is not None and choice not in codes:",
     "if codes and choice is not None and choice not in codes:", C + "test_answer_rejects_choice_without_options"),
    ("validate all replies", CLI, "        envelope.build(**reply, sender_session=session)\n", "",
     C + "test_batch_reply_validation_before_delivery"),
    ("filter exclusion", CLI, "filters = s.add_mutually_exclusive_group()", "filters = s",
     C + "test_inbox_filters_mutually_exclusive"),
    ("ask allowlist", CLI, 'checked = _send_allowlist(args, cfg, "question")',
     'checked = ({"status": "ok"}, None)', C + "test_ask_allowlist_and_override"),
    ("override reason length", CLI, "if reason is not None and len(reason) < antigravity_send.MIN_REASON:",
     "if False:", C + "test_ask_allowlist_and_override"),
    ("override audit", CLI, "        override_verdict=verdict,", "        override_verdict=None,",
     C + "test_ask_allowlist_and_override"),
    ("deadline display", CLI, 'q_lines.append(f"deadline: {q[\'deadline\']}")', "pass",
     C + "test_ask_filter_read_and_answer_roundtrip"),
    ("multiple ids", CLI, 's.add_argument("id", nargs="+", help="one or more question message ids")',
     's.add_argument("id", nargs=1, help="one question message id")', C + "test_answer_batch"),
]


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="relay-question-mutations-") as folder:
        work = Path(folder)
        shutil.copytree(ROOT / "agent_relay", work / "agent_relay", ignore=shutil.ignore_patterns("__pycache__"))
        (work / "tests").mkdir()
        (work / "tests" / "__init__.py").touch()
        shutil.copy2(ROOT / "tests" / "test_questions.py", work / "tests" / "test_questions.py")
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(work),
                   AGENT_RELAY_HOME=str(work / "home"), AGENT_RELAY_STORE="", AGENT_RELAY_AGENT="")
        failed = []
        for name, filename, old, new, test in MUTANTS:
            path = work / filename
            original = path.read_bytes()
            source = original.decode("utf-8")
            if old not in source:
                raise RuntimeError(f"mutation source not found: {name}")
            try:
                path.write_text(source.replace(old, new), encoding="utf-8", newline="\n")
                result = subprocess.run([sys.executable, "-m", "unittest", test, "-q"], cwd=work,
                                        env=env, capture_output=True, text=True, timeout=30)
                killed = result.returncode == 1 and "FAIL:" in result.stderr and "ERROR:" not in result.stderr
                print(f"{'KILLED' if killed else 'FAILED'}: {name} -> {test}", flush=True)
                if not killed:
                    print(result.stdout + result.stderr)
                    failed.append(name)
            finally:
                path.write_bytes(original)
        print(f"Mutation checks: {len(MUTANTS) - len(failed)}/{len(MUTANTS)} killed; temporary sources restored")
        return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
