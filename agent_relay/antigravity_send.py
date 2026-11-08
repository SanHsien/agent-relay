"""Send-time check for mail to the Antigravity sidecar (SPEC section 20).

The sidecar silently skips any mail whose `project` is not *exactly* one of its
`antigravity.repos` entries (a subdirectory of an entry does not count). Nothing tells the
sender, so a mistyped project sits in new/ until it expires. `send --to antigravity` reads the
same list the sidecar reads and refuses the mistake up front.

Where the list lives: the receiver's config.json, found in this order
  1. AGENT_RELAY_ANTIGRAVITY_HOME
  2. `antigravity_home` in the sender's own config.json
  3. ~/.agent-relay-antigravity   (the documented receiver home, same machine)
A sender on another machine cannot see that file; then the check cannot run and the caller
must warn loudly instead of staying silent.
"""

from __future__ import annotations

import difflib
import json
import os
from pathlib import Path

from . import antigravity

NEW_SESSION_REF = 'antigravity-session:new'
MIN_REASON = 8


class AllowlistUnreadable(Exception):
    """The receiver's allow-list could not be read from this machine."""


def receiver_home(cfg: dict) -> Path:
    raw = os.environ.get('AGENT_RELAY_ANTIGRAVITY_HOME') or cfg.get('antigravity_home')
    return Path(raw).expanduser() if raw else Path.home() / '.agent-relay-antigravity'


def load_allowlist(home: Path) -> list[Path]:
    """The resolved `antigravity.repos` (or legacy `repo`) of the receiver, exactly as the
    sidecar builds it (antigravity._repos), so the two can never disagree."""
    path = home / 'config.json'
    try:
        cfg = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        raise AllowlistUnreadable(f"{path} does not exist") from None
    except (OSError, ValueError) as exc:
        raise AllowlistUnreadable(f"{path} is unreadable ({type(exc).__name__})") from None
    settings = cfg.get('antigravity') if isinstance(cfg, dict) else None
    if not isinstance(settings, dict):
        raise AllowlistUnreadable(f"{path} has no 'antigravity' section")
    try:
        return sorted(antigravity._repos(settings), key=str)
    except (ValueError, KeyError) as exc:
        raise AllowlistUnreadable(f"{path}: {exc}") from None


def closest_allowed(project: Path, allowed: list[Path]) -> tuple[Path, str] | None:
    """The allowed entry nearest to a rejected project, and how they relate."""
    best: tuple[int, Path] | None = None
    for entry in allowed:  # an ancestor: the "subdirectory of an entry" mistake
        if entry in project.parents and (best is None or len(entry.parts) > best[0]):
            best = (len(entry.parts), entry)
    if best:
        return best[1], 'the project is inside this entry, but the sidecar needs an exact match'
    for entry in allowed:  # a descendant: the project is a parent of a listed repo
        if project in entry.parents:
            return entry, 'this entry is inside the project, but the sidecar needs an exact match'
    scored = [(difflib.SequenceMatcher(None, str(project).lower(), str(e).lower()).ratio(), e) for e in allowed]
    if not scored:
        return None
    ratio, entry = max(scored, key=lambda item: item[0])
    return entry, f'most similar entry (similarity {ratio:.2f})'


def check(cfg: dict, kind: str, project: str | None, refs: list[str] | None,
          override_reason: str | None = None) -> dict:
    """Judge one outgoing mail to the Antigravity mailbox.

    Returns {"status": "ok" | "reject" | "unchecked", "errors": [...], "warnings": [...]}.
    `reject` carries the messages to print; `unchecked` means the list could not be read."""
    result: dict = {'status': 'ok', 'errors': [], 'warnings': []}
    if NEW_SESSION_REF in (refs or []) and (kind != 'task' or not project):
        result['errors'].append(
            f"--ref {NEW_SESSION_REF} needs --kind task and --project; the sidecar silently skips it otherwise")
    if not project:
        result['status'] = 'reject' if result['errors'] else 'ok'
        return result
    home = receiver_home(cfg)
    try:
        allowed = load_allowlist(home)
    except AllowlistUnreadable as exc:
        result['status'] = 'reject' if result['errors'] else 'unchecked'
        result['warnings'].append(
            f"WARNING: cannot read the Antigravity allow-list ({exc}). The sidecar silently skips a mail "
            f"whose --project is not an exact antigravity.repos entry, and this send cannot verify it. "
            f"Point AGENT_RELAY_ANTIGRAVITY_HOME (or antigravity_home in config.json) at the receiver's home.")
        return result
    resolved = Path(project).resolve()
    if resolved not in allowed:
        near = closest_allowed(resolved, allowed)
        lines = [f"--project {project} is not an antigravity.repos entry of {home / 'config.json'}; "
                 f"the sidecar would skip this mail silently (exact match only)."]
        if not Path(project).is_absolute():
            lines.append("the project is not an absolute path; the sidecar resolves it in its own working directory.")
        if near:
            lines.append(f"closest allowed entry: {near[0]}  ({near[1]})")
        lines.append("allowed: " + ", ".join(str(p) for p in allowed))
        lines.append("fix: send with an allowed --project and name the subfolder in the body, or add the "
                     "path to antigravity.repos, or override with --allow-unlisted-project \"<reason>\".")
        if override_reason:
            result['warnings'].append(
                f"WARNING: --allow-unlisted-project override ({override_reason}); the sidecar will still skip "
                f"this mail unless {project} is added to antigravity.repos.\n" + "\n".join(lines[:-1]))
            result['override'] = True
        else:
            result['errors'].append("\n".join(lines))
    result['status'] = 'reject' if result['errors'] else 'ok'
    return result
