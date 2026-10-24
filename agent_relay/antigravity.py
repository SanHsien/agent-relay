"""Antigravity App sidecar: notify one explicitly configured existing conversation,
or open a new top-level Desktop conversation when mail carries `antigravity-session:new`.

Uses the App's agentapi, not agy CLI's separate conversation store. Only message
IDs and fixed relay commands enter the notification; mail text is never argv.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import gitsync, procs, signing
from . import status as relay_status
from .context import redact
from .store import Store


def _write(path: Path, value: dict) -> None:
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    os.replace(tmp, path)


def deliver_once(home: Path, *, sync: bool = True) -> dict:
    """One cycle, then a receiver heartbeat (SPEC section 16). A heartbeat failure never
    turns a delivered notification into an error; it is recorded as heartbeat_error."""
    result = _deliver(home, sync=sync)
    cfg = json.loads((home / 'config.json').read_text(encoding='utf-8'))
    try:
        if relay_status.heartbeat(cfg, 'antigravity') and sync:
            gitsync.sync(Path(cfg['store']), "relay: antigravity heartbeat")
    except (gitsync.SyncError, OSError) as exc:
        result['heartbeat_error'] = type(exc).__name__
    return result


def _repos(settings: dict) -> set[Path]:
    """Resolved set of repos the bound conversation accepts; legacy `repo` is one element."""
    if 'repos' in settings and 'repo' in settings:
        raise ValueError('configure either repos or repo, not both')
    raw = settings['repos'] if 'repos' in settings else [settings['repo']] if 'repo' in settings else None
    if (not isinstance(raw, list) or not raw
            or not all(isinstance(item, str) and item for item in raw)):
        raise ValueError('requires a non-empty repos list (or legacy repo string)')
    repos = {Path(item).resolve() for item in raw}
    if not all(path.is_dir() for path in repos):
        raise ValueError('requires every configured repo to be an existing directory')
    return repos


def _deliver(home: Path, *, sync: bool) -> dict:
    """At most one notification per cycle; attempted sends are never auto-retried."""
    cfg = json.loads((home / 'config.json').read_text(encoding='utf-8'))
    settings = cfg['antigravity']
    session = str(uuid.UUID(settings['conversation_id']))
    repos = _repos(settings)
    agent = cfg['agent']
    if agent != 'antigravity':
        raise ValueError('requires antigravity mailbox')
    senders = settings.get('senders') or []
    argv = settings['agentapi_argv']
    if (not isinstance(argv, list) or not argv or
            not Path(argv[0]).is_absolute() or Path(argv[0]).suffix.lower() in ('.bat', '.cmd')):
        raise ValueError('agentapi_argv requires an absolute native executable; no batch wrappers')
    if sync:
        gitsync.pull(Path(cfg['store']))
    state_dir = home / 'app-delivery'
    state_dir.mkdir(parents=True, exist_ok=True)
    _, allowed = signing.settings(cfg, home)
    for state, mail in Store(cfg['store']).list(agent):
        if state != 'new' or mail['kind'] not in ('task', 'reply') or mail['from'] not in senders:
            continue
        # A signed explicit session ref, or a signed project, must match local policy.
        refs = mail.get('refs', [])
        session_refs = [ref for ref in refs if ref.startswith('antigravity-session:')]
        new_conv = session_refs == ['antigravity-session:new']
        if session_refs:
            if not new_conv and session_refs != [f'antigravity-session:{session}']:
                continue
        elif not mail.get('project'):
            continue
        if new_conv and (mail['kind'] != 'task' or not mail.get('project')):
            continue
        project = Path(mail['project']).resolve() if mail.get('project') else None
        if project is not None and project not in repos:
            continue
        if mail.get('dispatch'):
            continue  # dispatch is a different, fresh-worker protocol
        try:
            created = datetime.fromisoformat(mail['created_at'].replace('Z', '+00:00'))
            age = (datetime.now(timezone.utc) - created).total_seconds()
        except (ValueError, TypeError):
            continue
        if not 0 <= age <= float(settings.get('max_age_hours', 24)) * 3600:
            continue
        reason = signing.verify(mail, allowed)
        if reason:
            # Keep a receipt so a refused mail is reported once, not on every cycle.
            try:
                with (state_dir / f"{mail['id']}.json").open('x', encoding='utf-8') as out:
                    json.dump({'status': 'refused', 'reason': reason}, out)
            except FileExistsError:
                continue
            print(json.dumps({'id': mail['id'], 'status': 'refused', 'reason': reason}), flush=True)
            continue
        receipt = state_dir / f"{mail['id']}.json"
        target_session = 'new' if new_conv else session
        try:
            with receipt.open('x', encoding='utf-8') as out:
                json.dump({'status': 'attempting', 'conversation_id': target_session}, out)
        except FileExistsError:
            continue
        # Fixed text only. Full signed mail stays in the mailbox until the agent reads it.
        scope = f"{project} 的任務" if project is not None else "信件所定義範圍的任務"
        notification = (
            f"agent-relay 有一封已驗證寄件者的工作訊息，ID {mail['id']}。"
            f"本機已授權將{scope}交給這個會話；先完成目前安全步驟，再收信。"
            f"執行 python -m agent_relay read {mail['id']} --agent antigravity "
            f'--home "{home}" --json。依信件範圍與驗收條件處理，保留目前模型、權限與 hooks。'
            f"回信使用同一 --home，--from antigravity --to {mail['from']} --kind result "
            f"--reply-to {mail['id']}，附上實際驗證證據；最後 ack 原信。"
            "若與目前工作衝突或無法執行，回報阻擋，不另開工人。"
        )
        result = {'id': mail['id'], 'conversation_id': target_session}
        if new_conv:
            result['mode'] = 'new-conversation'
        cmd = (
            [*argv, 'new-conversation', f"--title=[Relay] {mail['id']}", notification]
            if new_conv else
            [*argv, 'send-message', session, notification]
        )
        try:
            proc = procs.run(cmd,
                             capture_output=True, text=True, encoding='utf-8', errors='replace',
                             timeout=30, check=False)
            result.update(status='notified' if proc.returncode == 0 else 'failed', exit=proc.returncode)
            if new_conv and proc.returncode == 0 and isinstance(getattr(proc, 'stdout', None), str) and proc.stdout:
                try:
                    payload = json.loads(proc.stdout)
                    spawned = payload.get('response', {}).get('newConversation', {}).get('conversationId')
                    if isinstance(spawned, str) and spawned:
                        result['conversation_id'] = spawned
                except (ValueError, TypeError, AttributeError):
                    pass
            if proc.returncode:
                result['detail'] = redact(str(proc.stderr or proc.stdout or 'agentapi failed'))[:1000]
        except (OSError, subprocess.TimeoutExpired) as exc:
            # Delivery may already have happened. Retain receipt; do not duplicate work.
            result.update(status='uncertain', error=type(exc).__name__)
        _write(receipt, result)
        return result
    return {'status': 'idle'}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--interval', type=int, default=300)
    args = parser.parse_args()
    if args.interval < 30:
        parser.error('--interval must be at least 30 seconds')
    # The heartbeat lists dispatch runs found under config.home(); this sidecar has its own
    # home (no runs/), so point it there instead of reporting the main receiver's runs.
    os.environ['AGENT_RELAY_HOME'] = str(args.home.resolve())
    while True:
        try:
            result = deliver_once(args.home.resolve())
        except (OSError, ValueError, KeyError, gitsync.SyncError, signing.SigningError) as exc:
            result = {'status': 'error', 'error': type(exc).__name__}
        if result['status'] != 'idle' or args.once:
            print(json.dumps(result, ensure_ascii=False), flush=True)
        if args.once:
            return 0 if result['status'] in ('idle', 'notified') else 1
        time.sleep(args.interval)


if __name__ == '__main__':
    sys.exit(main())
