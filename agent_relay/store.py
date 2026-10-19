"""File mailbox store, Maildir-style.

    <store>/mailboxes/<agent>/new/<id>.json   delivered, not yet read
    <store>/mailboxes/<agent>/cur/<id>.json   read, still open
    <store>/mailboxes/<agent>/done/<id>.json  acknowledged

Writes go to a temp file in the same directory and are renamed into place, so a
reader (or a git commit) never sees half an envelope.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from . import envelope

STATES = ("new", "cur", "done")


class StoreError(RuntimeError):
    pass


class Store:
    def __init__(self, root: str | os.PathLike):
        self.root = Path(root)

    def box(self, agent: str, state: str) -> Path:
        if not envelope.AGENT_RE.match(agent):
            raise StoreError(f"bad agent name: {agent!r}")
        if state not in STATES:
            raise StoreError(f"bad state: {state!r}")
        return self.root / "mailboxes" / agent / state

    def deliver(self, env: dict) -> Path:
        envelope.validate(env)
        target_dir = self.box(env["to"], "new")
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{env['id']}.json"
        if self.find(env["to"], env["id"]) is not None:
            raise StoreError(f"duplicate id {env['id']} in mailbox {env['to']}")
        fd, tmp = tempfile.mkstemp(dir=target_dir, prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(envelope.dumps(env))
            os.replace(tmp, target)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        return target

    def list(self, agent: str, states: tuple[str, ...] = ("new", "cur")) -> list[tuple[str, dict]]:
        """Return (state, envelope) pairs, oldest first. Unreadable files are skipped."""
        out: list[tuple[str, dict]] = []
        for state in states:
            folder = self.box(agent, state)
            if not folder.is_dir():
                continue
            for path in folder.glob("*.json"):
                if path.name.startswith("."):  # half-written temp file
                    continue
                try:
                    out.append((state, envelope.loads(path.read_text(encoding="utf-8"))))
                except (ValueError, OSError):
                    continue
        out.sort(key=lambda item: item[1]["id"])
        return out

    def find(self, agent: str, env_id: str) -> tuple[str, Path] | None:
        if not envelope.ID_RE.match(env_id):
            raise StoreError(f"bad id: {env_id!r}")
        for state in STATES:
            path = self.box(agent, state) / f"{env_id}.json"
            if path.is_file():
                return state, path
        return None

    def read(self, agent: str, env_id: str) -> dict:
        """Return the envelope and move it new -> cur."""
        hit = self.find(agent, env_id)
        if hit is None:
            raise StoreError(f"no message {env_id} in mailbox {agent}")
        state, path = hit
        env = envelope.loads(path.read_text(encoding="utf-8"))
        if state == "new":
            self._move(path, agent, "cur")
        return env

    def ack(self, agent: str, env_id: str) -> Path:
        hit = self.find(agent, env_id)
        if hit is None:
            raise StoreError(f"no message {env_id} in mailbox {agent}")
        state, path = hit
        if state == "done":
            return path
        return self._move(path, agent, "done")

    def _move(self, path: Path, agent: str, state: str) -> Path:
        dest_dir = self.box(agent, state)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / path.name
        os.replace(path, dest)
        return dest
