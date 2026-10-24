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
        if state.get("phase") not in ("running", "queued"):
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
    if old is not None:
        last = _parse(old.get("at", ""))
        unchanged = (old.get("running"), old.get("queued")) == (data["running"], data["queued"])
        if last is not None and now - last < interval * 60 and unchanged:
            return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".tmp-{path.name}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return True


def overview(cfg: dict, stale_minutes: float = DEFAULT_STALE_MIN, now: float | None = None) -> dict:
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
        last = _parse(data.get("at", ""))
        age = None if last is None else round((now - last) / 60, 1)
        hosts.append({"name": beat.stem, "at": data.get("at"), "age_minutes": age,
                      "stale": age is None or age > stale_minutes,
                      "running": data.get("running") or [], "queued": data.get("queued") or []})
    leases = local_leases(now)
    return {"mailboxes": boxes, "hosts": hosts, "local_runs": leases,
            "overdue": [lease["id"] for lease in leases if lease["overdue"]], "stale_minutes": stale_minutes}


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
    lines.append("本機派工:")
    lines += [f"  {lease['id']}: {lease['phase']}{'（已逾期）' if lease['overdue'] else ''}"
              f"{'' if lease['alive'] else '（行程已不在）'}" for lease in data["local_runs"]] or ["  （沒有）"]
    return "\n".join(lines)
