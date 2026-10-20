"""Complexity- and quota-aware routing: which runtime and model should take this task?

    task (subject, body, paths, mode)
        │ complexity: high / medium / low
        ▼
    quota snapshot ── claude: ~/.claude/agentdeck-status.json (agentdeck statusline)
        │             codex:  codex-reset-checker --json (SanHsien/codex-reset-checker)
        │             agy:    no local source → unknown
        ▼
    decision: runtime + model + effort, fallbacks, or "wait until <reset>"

The routing table follows agent-advisor's lanes (SanHsien/agent-advisor): hard,
judgment-heavy work to the strongest model, well-specified work to the middle tier,
mechanical batches to the cheapest fast model. A runtime is skipped when its
five-hour window is >= 90% used or its weekly window >= 95%.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

FIVE_HOUR_LIMIT = 90.0
WEEKLY_LIMIT = 95.0

HIGH_WORDS = ("debug", "root cause", "race", "deadlock", "security", "architecture", "refactor", "migrate",
              "concurren", "設計", "架構", "重構", "除錯", "根因", "安全", "競態", "遷移", "效能")
LOW_WORDS = ("rename", "typo", "format", "lint", "comment", "docs", "readme", "bump", "changelog",
             "改名", "錯字", "格式", "註解", "文件", "說明", "排版", "批次")

# (runtime, model, effort) per complexity, best first. Models are the runtime's own ids.
ROUTES = {
    "high": [("claude", "opus", None), ("codex", None, "high"), ("claude", "sonnet", None)],
    "medium": [("claude", "sonnet", None), ("codex", None, "medium"), ("agy", None, None)],
    "low": [("agy", None, None), ("claude", "haiku", None), ("codex", None, "low")],
}
PLAN_OK = {"claude", "codex", "agy"}
BUILD_OK = {"claude", "codex"}  # agent-relay runs agy read-only (plan + sandbox)


@dataclass
class Quota:
    runtime: str
    five_hour: float | None = None  # percent used
    weekly: float | None = None
    resets_at: float | None = None  # epoch seconds of the window that blocks it
    source: str = "unknown"

    def blocked(self) -> bool:
        return ((self.five_hour is not None and self.five_hour >= FIVE_HOUR_LIMIT)
                or (self.weekly is not None and self.weekly >= WEEKLY_LIMIT))


@dataclass
class Decision:
    complexity: str
    runtime: str | None
    model: str | None
    effort: str | None
    reasons: list[str] = field(default_factory=list)
    fallbacks: list[dict] = field(default_factory=list)
    wait_until: float | None = None
    quotas: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------- complexity


def complexity(subject: str, body: str, paths: list[str] | None = None, mode: str = "build") -> tuple[str, list[str]]:
    text = f"{subject}\n{body}".lower()
    reasons = []
    score = 0
    high = [w for w in HIGH_WORDS if w in text]
    low = [w for w in LOW_WORDS if w in text]
    if high:
        score += 2
        reasons.append("難度關鍵字：" + "、".join(high[:4]))
    if low:
        score -= 1
        reasons.append("機械性關鍵字：" + "、".join(low[:4]))
    files = len(paths or [])
    wide = any(p.endswith("/") or "**" in p for p in (paths or []))
    if files >= 6 or wide:
        score += 1
        reasons.append(f"範圍大（{files} 個路徑{'，含整個資料夾' if wide else ''}）")
    if len(body) > 3000:
        score += 1
        reasons.append("工單很長（>3000 字）")
    if mode == "plan":
        reasons.append("唯讀任務")
    level = "high" if score >= 2 else "low" if score < 0 else "medium"
    return level, reasons


# ---------------------------------------------------------------- quotas


def _num(value) -> float | None:
    try:
        return None if isinstance(value, bool) else float(value)
    except (TypeError, ValueError):
        return None


def claude_quota(status_file: Path | None = None, now: float | None = None) -> Quota:
    """agentdeck's statusline writes the Claude Code rate limits here; a passed reset means 0%."""
    now = now or time.time()
    path = status_file or Path.home() / ".claude" / "agentdeck-status.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Quota("claude")
    limits = data.get("rate_limits") or {}
    out = Quota("claude", source=str(path))
    resets = []
    for key, attr in (("five_hour", "five_hour"), ("seven_day", "weekly")):
        window = limits.get(key) or {}
        used, reset = _num(window.get("used_percentage")), _num(window.get("resets_at"))
        if reset is not None and reset < now:
            used = 0.0
        setattr(out, attr, used)
        if reset and reset >= now:
            resets.append((key, reset))
    out.resets_at = _blocking_reset(out, dict(resets))
    return out


def codex_quota(command: list[str] | None, now: float | None = None, timeout: float = 30) -> Quota:
    """codex-reset-checker --json: usage.primary_window (5h) and secondary_window (weekly)."""
    if not command:
        return Quota("codex")
    try:
        proc = subprocess.run([*command, "--json"], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, check=False)
        data = json.loads(proc.stdout) if proc.returncode == 0 else {}
    except (subprocess.SubprocessError, OSError, ValueError):
        return Quota("codex")
    usage = data.get("usage") or {}
    primary, secondary = usage.get("primary_window") or {}, usage.get("secondary_window") or {}
    out = Quota("codex", _num(primary.get("used_percent")), _num(secondary.get("used_percent")),
                source="codex-reset-checker")
    out.resets_at = _blocking_reset(out, {"five_hour": _num(primary.get("reset_at")),
                                          "seven_day": _num(secondary.get("reset_at"))})
    return out


def _blocking_reset(quota: Quota, resets: dict) -> float | None:
    if quota.weekly is not None and quota.weekly >= WEEKLY_LIMIT:
        return resets.get("seven_day")
    if quota.five_hour is not None and quota.five_hour >= FIVE_HOUR_LIMIT:
        return resets.get("five_hour")
    return None


def snapshot(cfg: dict) -> dict[str, Quota]:
    settings = cfg.get("advisor") if isinstance(cfg.get("advisor"), dict) else {}
    cmd = settings.get("codex_quota_command")
    command = shlex.split(cmd, posix=False) if isinstance(cmd, str) else cmd if isinstance(cmd, list) else None
    status = settings.get("claude_status_file")
    return {
        "claude": claude_quota(Path(status) if status else None),
        "codex": codex_quota(command),
        "agy": Quota("agy"),
    }


# ---------------------------------------------------------------- decision


def decide(subject: str, body: str, paths: list[str] | None = None, mode: str = "build",
           quotas: dict[str, Quota] | None = None, allowed: list[str] | None = None) -> Decision:
    level, reasons = complexity(subject, body, paths, mode)
    quotas = quotas or {}
    usable = PLAN_OK if mode == "plan" else BUILD_OK
    candidates = []
    for runtime, model, effort in ROUTES[level]:
        if runtime not in usable or (allowed is not None and runtime not in allowed):
            continue
        candidates.append((runtime, model, effort))
    # keep everything usable as a last resort, in the order of the next tiers
    for tier in ("medium", "low", "high"):
        for route in ROUTES[tier]:
            if route not in candidates and route[0] in usable and (allowed is None or route[0] in allowed):
                candidates.append(route)
    picked, fallbacks, waits = None, [], []
    for runtime, model, effort in candidates:
        quota = quotas.get(runtime) or Quota(runtime)
        if quota.blocked():
            reasons.append(f"{runtime} 額度不足（5 小時 {quota.five_hour}%／週 {quota.weekly}%），跳過")
            if quota.resets_at:
                waits.append(quota.resets_at)
            continue
        if picked is None:
            picked = (runtime, model, effort)
        else:
            fallbacks.append({"runtime": runtime, "model": model, "effort": effort})
    decision = Decision(level, None, None, None, reasons, fallbacks[:3],
                        quotas={k: asdict(v) for k, v in quotas.items()})
    if picked:
        decision.runtime, decision.model, decision.effort = picked
        decision.reasons.append(f"複雜度 {level} → {picked[0]}{'/' + picked[1] if picked[1] else ''}"
                                f"{'（effort ' + picked[2] + '）' if picked[2] else ''}")
    else:
        decision.wait_until = min(waits) if waits else None
        decision.reasons.append("所有可用 runtime 額度都不足，等最早的重置時間再派")
    return decision


# ---------------------------------------------------------------- hand-over card


def handoff_card(decision: Decision, subject: str, body: str, paths: list[str] | None = None) -> str:
    """Obsidian/Markdown hand-over card for the chosen worker (frontmatter + fixed sections)."""
    dod = _section(body, ("驗收", "DoD", "完成的定義"))
    when = time.strftime("%Y-%m-%d %H:%M")
    route = (f"{decision.runtime}{' / ' + decision.model if decision.model else ''}"
             f"{' / effort ' + decision.effort if decision.effort else ''}") if decision.runtime else "（等待額度）"
    quota_lines = "\n".join(
        f"- {name}：5 小時 {q.get('five_hour')}%、週 {q.get('weekly')}%（{q.get('source')}）"
        for name, q in decision.quotas.items())
    return (
        "---\n"
        "status: pending\n"
        f"created: {when}\n"
        f"complexity: {decision.complexity}\n"
        f"runtime: {decision.runtime or 'wait'}\n"
        f"model: {decision.model or 'default'}\n"
        f"effort: {decision.effort or 'default'}\n"
        "tags: [handoff, agent-relay]\n"
        "---\n\n"
        f"# {subject}\n\n"
        f"## 派給誰\n{route}\n\n" + "\n".join(f"- {r}" for r in decision.reasons) + "\n\n"
        "## 範圍\n" + ("\n".join(f"- `{p}`" for p in paths) if paths else "- （未指定）") + "\n\n"
        f"## 要做什麼\n{body.strip()}\n\n"
        f"## 驗收條件（DoD）\n{dod or '照「要做什麼」；沒寫就以可驗證的證據為準。'}\n\n"
        "## 備案\n" + ("\n".join(f"- {f['runtime']}{' / ' + f['model'] if f['model'] else ''}" for f in decision.fallbacks)
                     or "- （無）") + "\n\n"
        f"## 派工當下的額度\n{quota_lines or '- （未讀取）'}\n"
    )


def _section(body: str, names: tuple[str, ...]) -> str:
    lines = body.splitlines()
    for i, line in enumerate(lines):
        if line.lstrip().startswith("#") and any(n.lower() in line.lower() for n in names):
            out = []
            for nxt in lines[i + 1:]:
                if re.match(r"^\s*#", nxt):
                    break
                out.append(nxt)
            return "\n".join(out).strip()
    return ""
