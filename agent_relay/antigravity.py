"""Antigravity App sidecar: notify one explicitly configured existing conversation.

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
from .context import redact
from .store import Store


def _write(path: Path, value: dict) -> None:
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    os.replace(tmp, path)


def deliver_once(home: Path, *, sync: bool = True) -> dict:
    """At most one notification per cycle; attempted sends are never auto-retried."""
    cfg = json.loads((home / 'config.json').read_text(encoding='utf-8'))
    settings = cfg['antigravity']
    session = str(uuid.UUID(settings['conversation_id']))
    repo = Path(settings['repo']).resolve()
    agent = cfg['agent']
    if agent != 'antigravity' or not repo.is_dir():
        raise ValueError('requires antigravity mailbox and an existing configured repo')
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
        if session_refs:
            if session_refs != [f'antigravity-session:{session}']:
                continue
        elif not mail.get('project') or Path(mail['project']).resolve() != repo:
            continue
        if mail.get('project') and Path(mail['project']).resolve() != repo:
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
            print(json.dumps({'id': mail['id'], 'status': 'refused', 'reason': reason}), flush=True)
            continue
        receipt = state_dir / f"{mail['id']}.json"
        try:
            with receipt.open('x', encoding='utf-8') as out:
                json.dump({'status': 'attempting', 'conversation_id': session}, out)
        except FileExistsError:
            continue
        # Fixed text only. Full signed mail stays in the mailbox until the agent reads it.
        notification = (
            f"agent-relay 有一封已驗證寄件者的工作訊息，ID {mail['id']}。"
            f"本機已授權將 {repo} 的任務交給這個會話；先完成目前安全步驟，再收信。"
            f"執行 python -m agent_relay read {mail['id']} --agent antigravity "
            f'--home "{home}" --json。依信件範圍與驗收條件處理，保留目前模型、權限與 hooks。'
            f"回信使用同一 --home，--from antigravity --to {mail['from']} --kind result "
            f"--reply-to {mail['id']}，附上實際驗證證據；最後 ack 原信。"
            "若與目前工作衝突或無法執行，回報阻擋，不另開工人。"
        )
        result = {'id': mail['id'], 'conversation_id': session}
        try:
            proc = procs.run([*argv, 'send-message', session, notification],
                             capture_output=True, text=True, encoding='utf-8', errors='replace',
                             timeout=30, check=False)
            result.update(status='notified' if proc.returncode == 0 else 'failed', exit=proc.returncode)
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
