"""Envelope v1: the one JSON shape every agent reads and writes.

See SPEC.md for the field contract. This module only builds, validates and
serialises envelopes; it knows nothing about where they are stored.
"""

from __future__ import annotations

import json
import re
import secrets
import socket
from datetime import datetime, timezone

SCHEMA = "agent-relay/envelope@1"

KINDS = ("task", "reply", "note", "result")
PRIORITIES = ("high", "normal", "low")

# Mailbox names become directory names, so keep them filesystem-safe.
AGENT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
ID_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]{0,127}$")

REQUIRED = ("schema", "id", "from", "to", "created_at", "kind", "subject", "body")


class EnvelopeError(ValueError):
    pass


def new_id(now: datetime | None = None) -> str:
    """Sortable id: UTC timestamp to the millisecond plus 6 random hex chars."""
    now = now or datetime.now(timezone.utc)
    return now.strftime("%Y%m%dT%H%M%S") + f"{now.microsecond // 1000:03d}Z-" + secrets.token_hex(3)


def build(
    *,
    sender: str,
    to: str,
    subject: str,
    body: str,
    kind: str = "note",
    priority: str = "normal",
    reply_to: str | None = None,
    thread: str | None = None,
    project: str | None = None,
    refs: list[str] | None = None,
) -> dict:
    now = datetime.now(timezone.utc)
    env_id = new_id(now)
    env = {
        "schema": SCHEMA,
        "id": env_id,
        "from": sender,
        "to": to,
        "created_at": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "host": socket.gethostname(),
        "kind": kind,
        "priority": priority,
        "subject": subject,
        "body": body,
        "thread": thread or reply_to or env_id,
    }
    if reply_to:
        env["reply_to"] = reply_to
    if project:
        env["project"] = project
    if refs:
        env["refs"] = list(refs)
    validate(env)
    return env


def validate(env: dict) -> None:
    if not isinstance(env, dict):
        raise EnvelopeError("envelope must be a JSON object")
    missing = [k for k in REQUIRED if k not in env]
    if missing:
        raise EnvelopeError(f"missing fields: {', '.join(missing)}")
    if env["schema"] != SCHEMA:
        raise EnvelopeError(f"unsupported schema: {env['schema']!r}")
    for key in ("from", "to"):
        if not AGENT_RE.match(str(env[key])):
            raise EnvelopeError(f"bad agent name in {key!r}: {env[key]!r}")
    if not ID_RE.match(str(env["id"])):
        raise EnvelopeError(f"id must be a plain file name: {env['id']!r}")
    if env["kind"] not in KINDS:
        raise EnvelopeError(f"kind must be one of {KINDS}")
    if env.get("priority", "normal") not in PRIORITIES:
        raise EnvelopeError(f"priority must be one of {PRIORITIES}")
    if not str(env["subject"]).strip():
        raise EnvelopeError("subject is empty")


def dumps(env: dict) -> str:
    return json.dumps(env, ensure_ascii=False, indent=2) + "\n"


def loads(text: str) -> dict:
    env = json.loads(text)
    validate(env)
    return env
