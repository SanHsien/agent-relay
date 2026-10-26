"""Tests for tools/nostr_adapter_poc.py (unsigned NIP-01 mapping PoC)."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load():
    path = ROOT / "tools" / "nostr_adapter_poc.py"
    spec = importlib.util.spec_from_file_location("nostr_adapter_poc", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["nostr_adapter_poc"] = module
    spec.loader.exec_module(module)
    return module


poc = _load()

# Hand-written NIP-01 commitment strings, hashed here with hashlib directly
# (not through the PoC), so the PoC is checked against an independent oracle.
VECTOR_PLAIN = '[0,"' + "a" * 64 + '",1792977825,9001,[],""]'
VECTOR_PLAIN_ID = "7d54e917753a9824b8742c44a93136468ec118f8086e6d039acc86cbb29d561a"
# subject = hi "x"<LF>, content = hello<U+0001><U+4F60>; NIP-01 escapes only \" \\ \n \r \t \b \f
VECTOR_ESC = ('[0,"' + "a" * 64 + '",1792977825,9001,[["subject","hi \\"x\\"\\n"]],"hello\x01\u4f60"]')
VECTOR_ESC_ID = "c515faad484adccb24762d738cd20c9144b69c0311b1d91e8a9c29cd719ef1df"


def sample(**extra) -> dict:
    env = {
        "schema": "agent-relay/envelope@1", "id": "20261026T012345000Z-a1b2c3",
        "from": "commander", "to": "codex", "created_at": "2026-10-26T01:23:45Z",
        "host": "h", "kind": "task", "priority": "normal", "subject": "s",
        "body": "line1\n\u4f60\u597d \"q\"\t\\\n", "thread": "20261026T012345000Z-a1b2c3",
    }
    env.update(extra)
    return env


class VectorTests(unittest.TestCase):
    def test_vectors_are_what_we_think(self):
        self.assertEqual(hashlib.sha256(VECTOR_PLAIN.encode()).hexdigest(), VECTOR_PLAIN_ID)
        self.assertEqual(hashlib.sha256(VECTOR_ESC.encode()).hexdigest(), VECTOR_ESC_ID)

    def test_plain_serialization_and_id(self):
        event = {"pubkey": "a" * 64, "created_at": 1792977825, "kind": 9001, "tags": [], "content": ""}
        self.assertEqual(poc.serialize(event), VECTOR_PLAIN)
        self.assertEqual(poc.compute_id(event), VECTOR_PLAIN_ID)

    def test_escaping_follows_nip01_not_python_json(self):
        event = {"pubkey": "a" * 64, "created_at": 1792977825, "kind": 9001,
                 "tags": [["subject", 'hi "x"\n']], "content": "hello\x01\u4f60"}
        self.assertEqual(poc.serialize(event), VECTOR_ESC)
        self.assertEqual(poc.compute_id(event), VECTOR_ESC_ID)

    def test_created_at_example_from_evaluation_doc(self):
        self.assertEqual(poc.iso_to_unix("2026-10-26T01:23:45Z"), 1792977825)
        self.assertEqual(poc.iso_to_unix("2026-10-26T09:23:45+08:00"), 1792977825)


class RoundTripTests(unittest.TestCase):
    def test_round_trip_lossless_all_kinds(self):
        for kind in poc.KIND_TO_INT:
            env = sample(kind=kind)
            event = poc.to_event(env)
            self.assertNotIn("sig", event)
            self.assertTrue(poc.verify_id(event))
            self.assertEqual(poc.from_event(event), env)

    def test_round_trip_survives_json_transport(self):
        env = sample(reply_to="20261025T000000000Z-ffffff", refs=["a", "b"])
        wire = json.loads(json.dumps(poc.to_event(env, "b" * 64, "c" * 64)))
        self.assertEqual(poc.from_event(wire), env)
        self.assertIn(["p", "c" * 64], wire["tags"])

    def test_tags_and_kind_mapping(self):
        event = poc.to_event(sample())
        self.assertEqual(event["kind"], 9001)
        self.assertIn(["relay-id", "20261026T012345000Z-a1b2c3"], event["tags"])
        self.assertEqual(event["created_at"], 1792977825)

    def test_bad_pubkey_rejected(self):
        with self.assertRaises(poc.NostrPocError):
            poc.to_event(sample(), "xyz")


class TamperTests(unittest.TestCase):
    def test_tampered_content_detected(self):
        event = poc.to_event(sample())
        event["content"] = event["content"].replace("line1", "LINE1")
        self.assertFalse(poc.verify_id(event))
        with self.assertRaises(poc.NostrPocError):
            poc.from_event(event)

    def test_tampered_tag_detected(self):
        event = poc.to_event(sample())
        event["tags"][1] = ["relay-from", "evil"]
        self.assertFalse(poc.verify_id(event))

    def test_consistent_id_but_tag_content_drift_detected(self):
        event = poc.to_event(sample())
        event["tags"][1] = ["relay-from", "evil"]
        event["id"] = poc.compute_id(event)  # attacker recomputes id
        with self.assertRaises(poc.NostrPocError):
            poc.from_event(event)

    def test_malformed_id_rejected(self):
        event = poc.to_event(sample())
        event["id"] = "00"
        self.assertFalse(poc.verify_id(event))


class CliTests(unittest.TestCase):
    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = poc.main(argv)
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_help_exits_zero(self):
        code, out, _ = self.run_cli(["--help"])
        self.assertEqual(code, 0)
        self.assertIn("UNSIGNED", out)

    def test_demo(self):
        code, out, _ = self.run_cli(["demo"])
        self.assertEqual(code, 0)
        self.assertIn("round trip   : True", out)

    def test_verify_detects_tamper_via_cli(self):
        import tempfile
        event = poc.to_event(sample())
        with tempfile.TemporaryDirectory() as tmp:
            good = Path(tmp) / "good.json"
            bad = Path(tmp) / "bad.json"
            good.write_text(json.dumps(event), encoding="utf-8")
            tampered = copy.deepcopy(event)
            tampered["content"] += " "
            bad.write_text(json.dumps(tampered), encoding="utf-8")
            self.assertEqual(self.run_cli(["verify", str(good)])[0], 0)
            self.assertEqual(self.run_cli(["verify", str(bad)])[0], 1)


if __name__ == "__main__":
    unittest.main()
