#!/usr/bin/env python3
"""Proof of concept: map an agent-relay envelope to an UNSIGNED NIP-01 event and back.

Standard library only. The event carries no `sig`: BIP-340 Schnorr on secp256k1
is not in the Python standard library, and this PoC never connects to a relay.
See docs/nostr-nip01-evaluation.en.md Section 5 (Prototype).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_relay import envelope  # noqa: E402

# Placeholder kind numbers inside the NIP-01 regular range 1000-9999.
# Collision with registered NIPs is NOT verified (evaluation doc Section 5).
KIND_TO_INT = {"task": 9001, "result": 9002, "reply": 9003, "note": 9004}
INT_TO_KIND = {v: k for k, v in KIND_TO_INT.items()}

PUBKEY_RE = re.compile(r"[0-9a-f]{64}")
ID_HEX_RE = re.compile(r"[0-9a-f]{64}")
PLACEHOLDER_PUBKEY = "00" * 32

# NIP-01 string escaping: only these seven escapes; every other character
# (including other control characters and non-ASCII) is emitted raw.
_ESCAPES = {'"': '\\"', "\\": "\\\\", "\n": "\\n", "\r": "\\r",
            "\t": "\\t", "\b": "\\b", "\f": "\\f"}


class NostrPocError(ValueError):
    pass


def _ser_string(text: str) -> str:
    return '"' + "".join(_ESCAPES.get(ch, ch) for ch in text) + '"'


def _ser(value: object) -> str:
    if isinstance(value, str):
        return _ser_string(value)
    if isinstance(value, bool) or value is None or not isinstance(value, (int, list)):
        raise NostrPocError(f"unsupported value in event: {value!r}")
    if isinstance(value, int):
        return str(value)
    return "[" + ",".join(_ser(v) for v in value) + "]"


def serialize(event: dict) -> str:
    """NIP-01 commitment string: [0,pubkey,created_at,kind,tags,content], compact."""
    return _ser([0, event["pubkey"], event["created_at"], event["kind"],
                 event["tags"], event["content"]])


def compute_id(event: dict) -> str:
    return hashlib.sha256(serialize(event).encode("utf-8")).hexdigest()


def iso_to_unix(text: str) -> int:
    stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise NostrPocError(f"created_at has no timezone: {text!r}")
    return int(stamp.astimezone(timezone.utc).timestamp())


def to_event(env: dict, pubkey: str = PLACEHOLDER_PUBKEY, to_pubkey: str | None = None) -> dict:
    """Envelope -> unsigned NIP-01 event. The full envelope JSON goes in `content`."""
    envelope.validate(env)
    if not PUBKEY_RE.fullmatch(pubkey):
        raise NostrPocError("pubkey must be 64 lowercase hex chars")
    if to_pubkey is not None and not PUBKEY_RE.fullmatch(to_pubkey):
        raise NostrPocError("to_pubkey must be 64 lowercase hex chars")
    tags = [["relay-id", env["id"]], ["relay-from", env["from"]], ["relay-to", env["to"]]]
    if to_pubkey:
        tags.append(["p", to_pubkey])
    tags += [["relay-kind", env["kind"]], ["subject", env["subject"]],
             ["relay-thread", env.get("thread", env["id"])]]
    if "reply_to" in env:
        tags.append(["relay-reply-to", env["reply_to"]])
    event = {
        "pubkey": pubkey,
        "created_at": iso_to_unix(env["created_at"]),
        "kind": KIND_TO_INT[env["kind"]],
        "tags": tags,
        "content": envelope.dumps(env),
    }
    return {"id": compute_id(event), **event}  # no "sig": unsigned by design


def verify_id(event: dict) -> bool:
    return bool(ID_HEX_RE.fullmatch(str(event.get("id", "")))) and event["id"] == compute_id(event)


def _tag(event: dict, name: str) -> str | None:
    for tag in event["tags"]:
        if tag and tag[0] == name:
            return tag[1]
    return None


def from_event(event: dict) -> dict:
    """Unsigned NIP-01 event -> envelope. Rejects id mismatch and tag/content drift."""
    if not verify_id(event):
        raise NostrPocError("event id does not match NIP-01 recomputation (content altered?)")
    env = envelope.loads(event["content"])
    checks = {
        "relay-id": env["id"], "relay-from": env["from"], "relay-to": env["to"],
        "relay-kind": env["kind"], "subject": env["subject"],
        "relay-thread": env.get("thread", env["id"]),
    }
    for name, expected in checks.items():
        if _tag(event, name) != expected:
            raise NostrPocError(f"tag {name!r} disagrees with embedded envelope")
    if event["kind"] != KIND_TO_INT[env["kind"]]:
        raise NostrPocError("event kind disagrees with embedded envelope")
    if event["created_at"] != iso_to_unix(env["created_at"]):
        raise NostrPocError("event created_at disagrees with embedded envelope")
    return env


def _read_json(path: str) -> dict:
    text = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    return json.loads(text)


def _demo_envelope() -> dict:
    return {
        "schema": envelope.SCHEMA, "id": "20261026T012345000Z-a1b2c3",
        "from": "commander", "to": "codex", "created_at": "2026-10-26T01:23:45Z",
        "host": "demo-host", "kind": "task", "priority": "normal",
        "subject": "demo: NIP-01 mapping", "body": "Hello, Nostr 你好\n", "thread": "20261026T012345000Z-a1b2c3",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="PoC: convert an agent-relay envelope to an UNSIGNED NIP-01 event and back. "
                    "No sig (no secp256k1 in the standard library), no relay connection.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("to-event", help="envelope JSON file ('-' for stdin) -> unsigned event JSON")
    p.add_argument("file")
    p.add_argument("--pubkey", default=PLACEHOLDER_PUBKEY, help="64 hex bridge pubkey (default: all-zero placeholder)")
    p.add_argument("--to-pubkey", default=None, help="64 hex receiver pubkey for the 'p' tag")
    p = sub.add_parser("from-event", help="event JSON file ('-' for stdin) -> envelope JSON")
    p.add_argument("file")
    p = sub.add_parser("verify", help="recompute the NIP-01 id; exit 1 on mismatch")
    p.add_argument("file")
    sub.add_parser("demo", help="built-in sample envelope: convert, verify, round-trip")
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdin, sys.stdout, sys.stderr):  # before argparse so --help is safe on cp950 consoles
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    try:
        if args.cmd == "to-event":
            print(json.dumps(to_event(_read_json(args.file), args.pubkey, args.to_pubkey),
                             ensure_ascii=False, indent=2))
        elif args.cmd == "from-event":
            print(envelope.dumps(from_event(_read_json(args.file))), end="")
        elif args.cmd == "verify":
            event = _read_json(args.file)
            ok = verify_id(event)
            print(f"id {'OK' if ok else 'MISMATCH'}: claimed={event.get('id')} computed={compute_id(event)}"
                  f" (unsigned: sig not checked)")
            return 0 if ok else 1
        else:
            env = _demo_envelope()
            event = to_event(env)
            print("serialization:", serialize(event)[:120] + "...")
            print("event id     :", event["id"])
            print("id verifies  :", verify_id(event))
            print("round trip   :", from_event(event) == env)
            print("sig          : absent (unsigned; no secp256k1 in stdlib)")
    except (NostrPocError, envelope.EnvelopeError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
