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

from . import procs

FIVE_HOUR_LIMIT = 90.0
WEEKLY_LIMIT = 95.0

# English words match on word boundaries; Chinese terms are specific enough to match as text.
HIGH_EN = ("debug", "debugging", "root cause", "race condition", "data race", "deadlock", "security", "vulnerability",
           "architecture", "refactor", "refactoring", "migrate", "migration", "concurrency", "concurrent")
HIGH_ZH = ("架構", "重構", "除錯", "根因", "資安", "競態", "死結", "遷移", "效能瓶頸")
LOW_EN = ("rename", "typo", "typos", "reformat", "formatting", "lint", "comment", "comments", "readme", "bump", "changelog")
LOW_ZH = ("改名", "錯字", "格式化", "加註解", "排版", "批次改名")
_EN_HIGH = re.compile(r"\b(" + "|".join(re.escape(w) for w in HIGH_EN) + r")\b")
_EN_LOW = re.compile(r"\b(" + "|".join(re.escape(w) for w in LOW_EN) + r")\b")
# Paths, file names and code spans describe the work's location, not its difficulty.
_NOISE = re.compile(r"`[^`]*`|[A-Za-z0-9_.~-]*[/\\][A-Za-z0-9_./\\~-]*|[A-Za-z0-9_.-]+\.(?:md|py|ts|js|json|toml|ya?ml|txt|cs|go|rs)\b")

# (runtime, model, effort) per complexity, best first. Models are the runtime's own ids.
ROUTES = {
    "high": [("claude", "opus", None), ("codex", None, "high"), ("claude", "sonnet", None)],
    "medium": [("claude", "sonnet", None), ("codex", None, "medium")],
    "low": [("claude", "haiku", None), ("codex", None, "low")],
}
PLAN_OK = {"claude", "codex"}
BUILD_OK = {"claude", "codex"}


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
    text = _NOISE.sub(" ", f"{subject}\n{body}").lower()
    reasons = []
    score = 0
    high = list(dict.fromkeys(_EN_HIGH.findall(text) + [w for w in HIGH_ZH if w in text]))
    low = list(dict.fromkeys(_EN_LOW.findall(text) + [w for w in LOW_ZH if w in text]))
    if high:
        score += 2
        reasons.append("難度關鍵字：" + "、".join(high[:4]))
    elif low:  # a mechanical word never pulls hard work down
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


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _window(used, reset, now) -> tuple[float | None, float | None]:
    """A window whose reset time has passed is empty again."""
    if reset is not None and reset < now:
        return 0.0, None
    return used, reset


def claude_quota(status_file: Path | None = None, now: float | None = None) -> Quota:
    """agentdeck's statusline writes the Claude Code rate limits here; a passed reset means 0%."""
    now = now or time.time()
    path = status_file or Path.home() / ".claude" / "agentdeck-status.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Quota("claude")
    except (OSError, ValueError):
        return Quota("claude", source="讀取失敗")
    if not isinstance(data, dict) or not isinstance(data.get("rate_limits"), dict):
        return Quota("claude", source="讀取失敗")  # valid JSON, wrong shape
    limits = _dict(data.get("rate_limits"))
    five, week = _dict(limits.get("five_hour")), _dict(limits.get("seven_day"))
    f_used, f_reset = _window(_num(five.get("used_percentage")), _num(five.get("resets_at")), now)
    w_used, w_reset = _window(_num(week.get("used_percentage")), _num(week.get("resets_at")), now)
    out = Quota("claude", f_used, w_used, source=str(path))
    out.resets_at = _blocking_reset(out, {"five_hour": f_reset, "seven_day": w_reset})
    return out


def codex_quota(command: list[str] | None, now: float | None = None, timeout: float = 30) -> Quota:
    """codex-reset-checker --json: usage.primary_window (5h) and secondary_window (weekly)."""
    if not command:
        return Quota("codex")
    now = now or time.time()
    try:
        proc = procs.run([*command, "--json"], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, check=False)
        data = _dict(json.loads(proc.stdout)) if proc.returncode == 0 else None
    except (subprocess.SubprocessError, OSError, ValueError):
        data = None
    if data is None or not isinstance(data.get("usage"), dict):
        return Quota("codex", source="讀取失敗")
    usage = _dict(data.get("usage"))
    primary, secondary = _dict(usage.get("primary_window")), _dict(usage.get("secondary_window"))
    f_used, f_reset = _window(_num(primary.get("used_percent")), _num(primary.get("reset_at")), now)
    w_used, w_reset = _window(_num(secondary.get("used_percent")), _num(secondary.get("reset_at")), now)
    out = Quota("codex", f_used, w_used, source="codex-reset-checker")
    out.resets_at = _blocking_reset(out, {"five_hour": f_reset, "seven_day": w_reset})
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
    if isinstance(cmd, str):  # Windows-style split keeps quotes; strip them from each token
        command = [token.strip('"') for token in shlex.split(cmd, posix=False)]
    else:
        command = [str(c) for c in cmd] if isinstance(cmd, list) and cmd else None
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
    picked, fallbacks, waits, noted = None, [], [], set()
    for runtime, model, effort in candidates:
        quota = quotas.get(runtime) or Quota(runtime)
        if quota.source == "讀取失敗" and f"{runtime}:error" not in noted:
            noted.add(f"{runtime}:error")
            reasons.append(f"{runtime} 額度讀取失敗，當作可用")
        if quota.blocked():
            if runtime not in noted:
                noted.add(runtime)
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
    dod_names = ("驗收", "DoD", "完成的定義")
    dod = _section(body, dod_names)
    work = _section(body, ("要做什麼", "目標", "任務")) or _strip_sections(body, dod_names)
    when = time.strftime("%Y-%m-%d %H:%M")
    route = (f"{decision.runtime}{' / ' + decision.model if decision.model else ''}"
             f"{' / effort ' + decision.effort if decision.effort else ''}") if decision.runtime else "（等待額度）"
    def pct(value):
        return "未知" if value is None else f"{value:g}%"

    quota_lines = "\n".join(
        f"- {name}：5 小時 {pct(q.get('five_hour'))}、週 {pct(q.get('weekly'))}（{q.get('source')}）"
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
        f"## 要做什麼\n{work.strip() or '（見工單）'}\n\n"
        f"## 驗收條件（DoD）\n{dod or '照「要做什麼」；沒寫就以可驗證的證據為準。'}\n\n"
        "## 備案\n" + ("\n".join(f"- {f['runtime']}{' / ' + f['model'] if f['model'] else ''}" for f in decision.fallbacks)
                     or "- （無）") + "\n\n"
        f"## 派工當下的額度\n{quota_lines or '- （未讀取）'}\n"
    )


def _strip_sections(body: str, names: tuple[str, ...]) -> str:
    """The body without the named sections (so the card does not repeat the DoD)."""
    out, skipping = [], False
    for line in body.splitlines():
        if re.match(r"^\s*#", line):
            skipping = any(n.lower() in line.lower() for n in names)
        if not skipping:
            out.append(line)
    return "\n".join(out).strip()


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
