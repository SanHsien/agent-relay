"""`relay status` and receiver heartbeats (SPEC section 16).

Each `watch` round leaves a small heartbeat in the store, hosts/<agent>@<host>.json,
so a commander can tell "the receiver is busy" from "the receiver's scheduled task
stopped". The file is rewritten only when it is older than the heartbeat interval or
the set of running tasks changed, so the store does not grow a commit every round.
"""

from __future__ import annotations

import json
import os
import re
import socket
import time
from datetime import datetime, timezone
from pathlib import Path

from . import __version__, config
from .procs import pid_alive

SCHEMA = "agent-relay/heartbeat@1"
DEFAULT_HEARTBEAT_MIN = 30
DEFAULT_STALE_MIN = 90
DEFAULT_UNDELIVERED_MIN = 30  # six sidecar polls at the default 300 s interval


def _host() -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "-", socket.gethostname())[:63] or "host"


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse(value: str) -> float | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def local_leases(now: float | None = None) -> list[dict]:
    """Dispatch runs on this machine that have not answered yet (running or queued)."""
    now = now or time.time()
    out = []
    runs = config.home() / "runs"
    if not runs.is_dir():
        return out
    for state_file in runs.glob("*/state.json"):
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(state, dict) or state.get("phase") not in ("running", "queued"):
            continue
        deadline = state.get("deadline")
        out.append({"id": state_file.parent.name, "phase": state["phase"], "pid": state.get("pid"),
                    "alive": pid_alive(state.get("pid")),
                    "overdue": isinstance(deadline, (int, float)) and now > deadline})
    return sorted(out, key=lambda lease: lease["id"])


def heartbeat(cfg: dict, agent: str, now: float | None = None) -> bool:
    """Write this machine's heartbeat if due. Returns True when the store changed."""
    now = now or time.time()
    path = Path(cfg["store"]) / "hosts" / f"{agent}@{_host()}.json"
    leases = local_leases(now)
    data = {"schema": SCHEMA, "agent": agent, "host": _host(), "at": _iso(now), "version": __version__,
            "running": [lease["id"] for lease in leases if lease["phase"] == "running" and lease["alive"]],
            "queued": [lease["id"] for lease in leases if lease["phase"] == "queued" and lease["alive"]]}
    interval = (cfg.get("status") or {}).get("heartbeat_minutes", DEFAULT_HEARTBEAT_MIN)
    interval = interval if isinstance(interval, (int, float)) and not isinstance(interval, bool) and interval > 0 \
        else DEFAULT_HEARTBEAT_MIN
    try:
        old = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        old = None
    if isinstance(old, dict):
        last = _parse(old.get("at", ""))
        unchanged = (old.get("running"), old.get("queued")) == (data["running"], data["queued"])
        if last is not None and now - last < interval * 60 and unchanged:
            return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".tmp-{path.name}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return True


def _receipt(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def undelivered(cfg: dict, minutes: float, now: float) -> tuple[list[dict] | None, str | None]:
    """Mail in antigravity/new older than `minutes` that the sidecar never notified.

    Only the Antigravity sidecar keeps per-mail delivery receipts (app-delivery/ in the receiver
    home), so only that mailbox can be judged. Returns (None, reason) when the receiver home
    cannot be read on this machine: unchecked, never reported as "nothing stuck"."""
    from .antigravity_send import receiver_home  # local: antigravity_send imports this package's sidecar
    box = Path(cfg["store"]) / "mailboxes" / "antigravity" / "new"
    mails = sorted(f for f in box.glob("*.json") if not f.name.startswith(".")) if box.is_dir() else []
    if not mails:
        return [], None
    home = receiver_home(cfg)
    if not (home / "config.json").is_file():
        return None, f"{home / 'config.json'} is not readable here"
    receipts = home / "app-delivery"
    out = []
    for path in mails:
        mail = _receipt(path) or {}
        mail_id = mail.get("id") or path.stem
        created = _parse(mail.get("created_at", ""))
        age = None if created is None else (now - created) / 60
        if age is not None and age < minutes:
            continue
        got = _receipt(receipts / f"{mail_id}.json")
        if got and got.get("status") in ("notified", "attempting", "uncertain"):
            continue  # reached the App, or may have: not this check's business
        if got:
            reason = f"receipt {got.get('status')}: {got.get('reason') or got.get('detail') or ''}".rstrip(": ")
        else:
            note = _receipt(receipts / "skipped" / f"{mail_id}.json")
            reason = (f"skipped: {note.get('reason')}" if note else
                      "no receipt (sidecar not running, or an older sidecar that skips silently)")
        out.append({"mailbox": "antigravity", "id": mail_id, "subject": mail.get("subject"),
                    "age_minutes": None if age is None else round(age, 1), "reason": reason})
    return out, None


def overview(cfg: dict, stale_minutes: float = DEFAULT_STALE_MIN, now: float | None = None,
             undelivered_minutes: float = DEFAULT_UNDELIVERED_MIN) -> dict:
    now = now or time.time()
    store = Path(cfg["store"])
    boxes = {}
    for box in sorted((store / "mailboxes").glob("*")) if (store / "mailboxes").is_dir() else []:
        counts = {state: sum(1 for f in (box / state).glob("*.json") if not f.name.startswith("."))
                  for state in ("new", "cur")}
        if any(counts.values()):
            boxes[box.name] = counts
    hosts = []
    for beat in sorted((store / "hosts").glob("*.json")) if (store / "hosts").is_dir() else []:
        try:
            data = json.loads(beat.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        last = _parse(data.get("at", ""))
        age = None if last is None else round((now - last) / 60, 1)
        hosts.append({"name": beat.stem, "at": data.get("at"), "age_minutes": age,
                      "stale": age is None or age > stale_minutes,
                      "running": data.get("running") or [], "queued": data.get("queued") or []})
    leases = local_leases(now)
    stuck, unchecked = undelivered(cfg, undelivered_minutes, now)
    return {"mailboxes": boxes, "hosts": hosts, "local_runs": leases,
            "overdue": [lease["id"] for lease in leases if lease["overdue"]], "stale_minutes": stale_minutes,
            "undelivered": stuck, "undelivered_unchecked": unchecked, "undelivered_minutes": undelivered_minutes}


def render(data: dict) -> str:
    lines = ["信箱（未結案）:"]
    lines += [f"  {name}: new {c['new']}, cur {c['cur']}" for name, c in data["mailboxes"].items()] or ["  （全部已結案）"]
    lines.append(f"收件端心跳（超過 {data['stale_minutes']:g} 分鐘沒更新視為停擺）:")
    for host in data["hosts"]:
        flag = "⚠ 心跳逾時" if host["stale"] else "ok"
        lines.append(f"  {host['name']}: {flag}，{host['age_minutes']} 分鐘前；running {len(host['running'])}，"
                     f"queued {len(host['queued'])}")
    if not data["hosts"]:
        lines.append("  （還沒有任何心跳）")
    lines.append(f"Antigravity 未投遞（在 new/ 超過 {data['undelivered_minutes']:g} 分鐘、沒有通知收據）:")
    if data["undelivered"] is None:
        lines.append(f"  ⚠ 未檢查：{data['undelivered_unchecked']}")
    else:
        lines += [f"  ⚠ {item['id']}（{item['age_minutes']} 分鐘）{item['subject'] or ''}：{item['reason']}"
                  for item in data["undelivered"]] or ["  （沒有）"]
    lines.append("本機派工:")
    lines += [f"  {lease['id']}: {lease['phase']}{'（已逾期）' if lease['overdue'] else ''}"
              f"{'' if lease['alive'] else '（行程已不在）'}" for lease in data["local_runs"]] or ["  （沒有）"]
    return "\n".join(lines)
