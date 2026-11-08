"""`relay archive`: archive an Antigravity conversation this relay opened (SPEC section 20).

Uses the Language Server's own conversation-management RPC, the one the App's sidebar uses:

    POST http://127.0.0.1:<port>/exa.language_server_pb.LanguageServerService/UpdateConversationAnnotations
    {"cascadeIds": [<id>], "annotations": {"archived": true, ...}, "mergeAnnotations": true}

No model is called and no message is sent to any conversation. `agentapi` (what the sidecar
shells out to) has no archive command, so this talks to the same LS directly.

LS address and CSRF token, in this order:
  1. ANTIGRAVITY_LS_ADDRESS + ANTIGRAVITY_CSRF_TOKEN, the environment the App gives its sidecars
  2. ~/.gemini/antigravity/daemon/hub_<port>.json (`httpPort`, `csrfToken`, `pid`) of a live hub
The token is held in memory only: never printed, logged, put in an exception or a result.

Read-back: ~/.gemini/antigravity/annotations/<id>.pbtxt must show `archived:true`.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import procs
from .antigravity import _canonical_uuid
from .context import redact

RPC = '/exa.language_server_pb.LanguageServerService/UpdateConversationAnnotations'
DEFAULT_SIDECAR_LOG = Path('sidecar_data') / 'agent-relay' / 'logs' / 'sidecar.log'


MIN_SUFFIX = 6  # a mail id may be given as any suffix of at least this many characters


class ArchiveError(Exception):
    pass


def gemini_dir() -> Path:
    return Path(os.environ.get('ANTIGRAVITY_GEMINI_DIR') or Path.home() / '.gemini' / 'antigravity')


def _clean(text: str, secrets: tuple[str, ...]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, '[REDACTED]')
    return redact(text)


def _opened_by_relay(log_text: str) -> list[dict]:
    """sidecar.log lines for conversations the sidecar opened: other lines are skipped."""
    rows = []
    for line in log_text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if (isinstance(row, dict) and row.get('mode') == 'new-conversation' and row.get('status') == 'notified'
                and _canonical_uuid(row.get('conversation_id')) and isinstance(row.get('id'), str)):
            rows.append(row)
    return rows


def sidecar_log(gemini: Path | None = None) -> Path:
    return (gemini or gemini_dir()) / DEFAULT_SIDECAR_LOG


def resolve_mail(mail: str, log: Path) -> str:
    """The conversation a mail opened. `mail` is a full id or its short suffix (>= 6 chars)."""
    try:
        rows = _opened_by_relay(log.read_text(encoding='utf-8', errors='replace'))
    except OSError as exc:
        raise ArchiveError(f"cannot read the sidecar log {log} ({type(exc).__name__})") from None
    hits = {r['id']: r['conversation_id'] for r in rows
            if r['id'] == mail or (len(mail) >= MIN_SUFFIX and r['id'].endswith(mail))}
    if not hits:
        raise ArchiveError(f"mail {mail!r} opened no conversation according to {log} "
                           f"(needs a mode=new-conversation, status=notified line)")
    if len(hits) > 1:
        raise ArchiveError(f"mail {mail!r} is ambiguous: {', '.join(sorted(hits))}")
    return next(iter(hits.values()))


def relay_opened(conversation: str, log: Path) -> bool:
    try:
        return any(r['conversation_id'] == conversation
                   for r in _opened_by_relay(log.read_text(encoding='utf-8', errors='replace')))
    except OSError:
        return False


def discover_ls(gemini: Path | None = None) -> list[tuple[str, str]]:
    """Candidate (base url, csrf token) pairs, best first. The token never leaves this tuple."""
    found: list[tuple[str, str]] = []
    address, token = os.environ.get('ANTIGRAVITY_LS_ADDRESS'), os.environ.get('ANTIGRAVITY_CSRF_TOKEN')
    if address and token:
        found.append((address if address.startswith(('http://', 'https://')) else f'http://{address}', token))
    hubs = sorted((gemini or gemini_dir()).glob('daemon/hub_*.json'), key=lambda p: p.stat().st_mtime, reverse=True)
    for hub in hubs:
        try:
            data = json.loads(hub.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        port, csrf = data.get('httpPort'), data.get('csrfToken')
        if (isinstance(port, int) and isinstance(csrf, str) and csrf
                and procs.pid_alive(data.get('pid'))):
            found.append((f'http://127.0.0.1:{port}', csrf))
    if not found:
        raise ArchiveError("no running Antigravity language server found "
                           "(no ANTIGRAVITY_LS_ADDRESS/ANTIGRAVITY_CSRF_TOKEN, no live daemon/hub_*.json); is the App open?")
    return found


def read_annotation(conversation: str, gemini: Path | None = None) -> dict:
    """{"archived": bool, "archived_at": int | None, "title": str | None} from the .pbtxt, or {} if absent."""
    path = (gemini or gemini_dir()) / 'annotations' / f'{conversation}.pbtxt'
    try:
        text = path.read_text(encoding='utf-8')
    except OSError:
        return {}
    stamp = re.search(r'archival_status_timestamp:\{seconds:(\d+)', text)
    title = re.search(r'title:"((?:[^"\\]|\\.)*)"', text)
    return {'archived': bool(re.search(r'\barchived:true\b', text)),
            'archived_at': int(stamp.group(1)) if stamp else None,
            'title': title.group(1) if title else None}


def _post(base: str, token: str, conversation: str) -> int:
    stamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    body = {'cascadeIds': [conversation], 'mergeAnnotations': True,
            'annotations': {'archived': True, 'archivalStatusTimestamp': stamp}}
    try:
        req = urllib.request.Request(
            base + RPC, data=json.dumps(body).encode('utf-8'), method='POST',
            headers={'Content-Type': 'application/json', 'Connect-Protocol-Version': '1',
                     'x-codeium-csrf-token': token})
        with urllib.request.urlopen(req, timeout=20) as resp:
            resp.read(4096)
            return resp.status
    except urllib.error.HTTPError as exc:
        detail = _clean(exc.read(500).decode('utf-8', 'replace'), (token,))
        raise ArchiveError(f"LS answered HTTP {exc.code}: {detail}") from None
    except (urllib.error.URLError, OSError) as exc:
        raise ArchiveError(f"LS at {base} unreachable ({type(exc.reason if hasattr(exc, 'reason') else exc).__name__})") from None
    except (ValueError, http.client.HTTPException) as exc:
        # http.client rejects a header value with CR/LF by raising ValueError whose message quotes
        # the value, i.e. the token. Only the exception type may leave this function.
        raise ArchiveError(f"LS request failed ({type(exc).__name__})") from None


def archive(conversation: str, *, gemini: Path | None = None, wait: float = 10.0,
            post=_post) -> dict:
    """Archive one conversation and confirm it from the annotation file.

    Returns {"conversation_id", "http", "before", "after", "verified"}; raises ArchiveError when the
    RPC fails or the read-back does not show `archived:true`."""
    if _canonical_uuid(conversation) is None:
        raise ArchiveError(f"{conversation!r} is not a lowercase hyphenated conversation UUID")
    before = read_annotation(conversation, gemini)
    errors: list[str] = []
    status = None
    for base, token in discover_ls(gemini):
        try:
            status = post(base, token, conversation)
            break
        except ArchiveError as exc:
            errors.append(str(exc))
    if status is None:
        raise ArchiveError("; ".join(errors))
    start = time.monotonic()
    deadline = start + wait
    while True:
        after = read_annotation(conversation, gemini)
        # Already archived: the file shows archived:true at once, so also wait (briefly) for the
        # LS to rewrite the timestamp; that is the only proof this call, not an earlier one, landed.
        changed = after.get('archived_at') != before.get('archived_at')
        if after.get('archived') and (changed or not before.get('archived') or time.monotonic() >= min(deadline, start + 3)):
            break
        if time.monotonic() >= deadline:
            raise ArchiveError(f"LS answered HTTP {status} but {conversation}.pbtxt does not show archived:true")
        time.sleep(0.25)
    return {'conversation_id': conversation, 'http': status, 'before': before, 'after': after,
            'verified': True, 'timestamp_refreshed': changed}
