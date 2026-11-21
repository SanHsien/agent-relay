"""Ring when new mail lands.

A doorbell is only a nudge; the envelope in the store is the truth. Every channel
is best-effort: one failing channel is logged and the others still ring.

Channels
  toast    Windows toast through the built-in WinRT API (no module to install)
  command  run an argv list with {id} {from} {to} {subject} {kind} {summary} {count}
           filled in; no shell. .bat/.cmd programs are refused because cmd.exe
           would re-parse mail text. A value may start with "-", so put it
           after "--" if the program takes options.
  handoff  for kind=task with a "project" under the bell's "project_roots":
           drop a dropoff/pickup card in <project>/.claude/handoffs/ so the
           next session's /pickup sees it
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from . import config
from . import procs

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
KEEP_RUNG = 1000

_TOAST_PS = r"""
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
$tpl = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$t = $tpl.GetElementsByTagName('text')
$t.Item(0).AppendChild($tpl.CreateTextNode($env:RELAY_TITLE)) > $null
$t.Item(1).AppendChild($tpl.CreateTextNode($env:RELAY_BODY)) > $null
$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($tpl))
"""


HUMAN_BELLS = ("toast", "command")  # the only bells a batch summary makes sense for
BATCH_SUMMARY_MAX = 300
BATCH_SUBJECT_MAX = 60
BATCH_LIST_MAX = 5


def summary(env: dict) -> str:
    if env.get("batch_summary"):
        return env["batch_summary"]
    return f"[agent-relay] {env['from']} -> {env['to']} ({env['kind']}): {env['subject']}"


def fields(env: dict) -> dict:
    return {
        "id": env["id"],
        "from": env["from"],
        "to": env["to"],
        "subject": env["subject"],
        "kind": env["kind"],
        "summary": summary(env),
        "count": str(env.get("count", 1)),
    }


def batches(bell: dict) -> bool:
    """Whether this bell rings once per watch tick instead of once per mail.

    On by default only for toast: it is read by a person and nothing parses it. A
    command bell's argv may use {id} or {subject} for one specific mail, so merging
    there would change what an existing script receives; it batches only on request.
    handoff and dispatch act on each mail and never batch.
    """
    kind = bell.get("type", "")
    return kind in HUMAN_BELLS and bool(bell.get("batch", kind == "toast"))


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def batch_envelope(envs: list[dict], to: str) -> dict:
    """A stand-in envelope for one ring covering several mails; summary() gives the short text."""
    listed = "; ".join(f"{e['from']} ({e['kind']}): {_cut(e['subject'], BATCH_SUBJECT_MAX)}"
                       for e in envs[:BATCH_LIST_MAX])
    more = f"; +{len(envs) - BATCH_LIST_MAX} more" if len(envs) > BATCH_LIST_MAX else ""
    tail = f" -> {to}: {listed}"
    head = f"[agent-relay] {len(envs)} new"
    text = head + _cut(tail, BATCH_SUMMARY_MAX - len(head) - len(more)) + more
    return {
        "id": ",".join(e["id"] for e in envs),
        "from": ",".join(dict.fromkeys(e["from"] for e in envs)),
        "to": to,
        "subject": _cut(" | ".join(e["subject"] for e in envs), BATCH_SUMMARY_MAX),
        "kind": ",".join(dict.fromkeys(e["kind"] for e in envs)),
        "count": len(envs),
        "batch_summary": text,
    }


def _log(line: str) -> None:
    try:
        config.home().mkdir(parents=True, exist_ok=True)
        with open(config.home() / "relay.log", "a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now().isoformat(timespec='seconds')} {line}\n")
    except OSError:
        pass


def ring_toast(env: dict, bell: dict) -> str:
    if sys.platform != "win32":
        return "skipped: toast is Windows-only"
    proc = procs.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _TOAST_PS],
        env={**os.environ, "RELAY_TITLE": f"agent-relay: mail for {env['to']}", "RELAY_BODY": summary(env)},
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=CREATE_NO_WINDOW,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"exit {proc.returncode}")
    return "ok"


def ring_command(env: dict, bell: dict) -> str:
    argv = bell.get("argv")
    if not argv or not isinstance(argv, list):
        raise ValueError("command doorbell needs an argv list")
    if Path(str(argv[0])).suffix.lower() in (".bat", ".cmd"):
        # Windows hands batch files to cmd.exe, which re-parses the arguments:
        # mail text like "a & calc" would run. Use bash, python or an .exe instead.
        raise ValueError("command doorbell refuses .bat/.cmd programs")
    values = fields(env)
    filled = [str(arg).format_map(values) for arg in argv]
    proc = procs.run(
        filled,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=int(bell.get("timeout", 60)),
        creationflags=CREATE_NO_WINDOW,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout).strip() or f"exit {proc.returncode}")
    return "ok"


def handoff_card(env: dict, project: Path | None = None) -> str:
    created = datetime.now().strftime("%Y-%m-%d %H:%M")
    return (
        "---\n"
        "status: pending\n"
        f"from: {env['from']} (agent-relay, host {env.get('host', '?')})\n"
        f"from-session: agent-relay:{env['id']}\n"
        f"to: {project or env['project']}\n"
        f"created: {created}\n"
        f"priority: {env.get('priority', 'normal')}\n"
        "notify: rung\n"
        "---\n\n"
        f"# {env['subject']}\n\n"
        "## 要做什麼\n"
        f"{env['body']}\n\n"
        "## 脈絡（陌生 session 也看得懂的程度）\n"
        f"這張卡由 agent-relay 信件 `{env['id']}` 轉成（寄件者 `{env['from']}`，收件信箱 `{env['to']}`）。"
        # The subject is mail text: never paste it into a command someone will run.
        f"完成後請回信：`python -m agent_relay send --to {env['from']} --kind result "
        f"--reply-to {env['id']} --subject \"done: <原標題>\" --body-file <結果與證據檔>`，"
        f"再 `python -m agent_relay ack {env['id']}`。\n\n"
        "## 相關檔案／連結\n"
        + "".join(f"- {ref}\n" for ref in env.get("refs", []))
        + "\n## 完成的定義\n"
        "照「要做什麼」裡的驗收條件；沒寫就以回信附上可驗證證據為準。\n"
    )


def asr_registry_path() -> Path:
    """agent-session-restore's registry (SanHsien/agent-session-restore, storage.py)."""
    override = os.environ.get("CLAUDE_SESSION_REGISTRY_FILE", "").strip()
    return Path(override) if override else Path.home() / ".claude" / "claude-sessions.json"


def resolve_project(value: str) -> Path:
    """A folder path, or `asr:<session name or id>` looked up in the asr registry.

    The asr form lets the sender name a session ("the ai-inbox session") without
    knowing where that project lives on the receiving machine.
    """
    if not value.startswith("asr:"):
        return Path(value).expanduser()
    wanted = value[4:].strip()
    try:
        sessions = json.loads(asr_registry_path().read_text(encoding="utf-8")).get("sessions", {})
    except (OSError, ValueError) as exc:
        raise FileNotFoundError(f"cannot read agent-session-restore registry: {exc}") from exc
    matches = [
        entry for key, entry in sessions.items()
        if isinstance(entry, dict) and wanted in (key, entry.get("session_id"), entry.get("name"))
    ]
    active = [entry for entry in matches if entry.get("status") == "active"] or matches
    if not active or not active[0].get("cwd"):
        raise FileNotFoundError(f"no session {wanted!r} in agent-session-restore registry")
    active.sort(key=lambda entry: str(entry.get("updated_at", "")), reverse=True)
    return Path(active[0]["cwd"])


def _inside(path: Path, roots: list[str]) -> bool:
    resolved = path.resolve()
    for root in roots:
        base = Path(root).expanduser().resolve()
        if resolved == base or base in resolved.parents:
            return True
    return False


def ring_handoff(env: dict, bell: dict) -> str:
    if env["kind"] != "task" or not env.get("project"):
        return "skipped: not a task with a project"
    project = resolve_project(env["project"])
    if not project.is_dir():
        raise FileNotFoundError(f"project folder not on this machine: {project}")
    # Mail decides where a card lands, so the receiver must have opted that folder in.
    roots = bell.get("project_roots") or []
    if not _inside(project, roots):
        raise PermissionError(f"{project} is not under this doorbell's project_roots {roots}")
    folder = project / ".claude" / "handoffs"
    folder.mkdir(parents=True, exist_ok=True)
    card = folder / f"relay-{env['id']}.md"  # keyed on id: a re-ring never makes a second card
    if not card.exists():
        card.write_text(handoff_card(env, project), encoding="utf-8", newline="\n")
    return f"ok: {card}"


def ring_dispatch(env: dict, bell: dict) -> str:
    from .dispatch import ring_dispatch as start  # late import: dispatch imports this module

    return start(env, bell)


CHANNELS = {"toast": ring_toast, "command": ring_command, "handoff": ring_handoff, "dispatch": ring_dispatch}


PRIORITY_RANK = {"low": 0, "normal": 1, "high": 2}


def skip_reason(env: dict, bell: dict) -> str | None:
    """Why this bell stays quiet for this message, or None when it should ring.

    A min_priority the receiver mistyped raises ValueError: a typo must show up as an
    error, not silently mute (or un-mute) the bell.
    """
    wanted = bell.get("kinds")
    if wanted and env["kind"] not in wanted:
        return "skipped: kind filter"
    floor = bell.get("min_priority")
    if floor is not None:
        if floor not in PRIORITY_RANK:
            raise ValueError(f"min_priority must be one of {tuple(PRIORITY_RANK)}, got {floor!r}")
        if PRIORITY_RANK.get(env.get("priority", "normal"), 1) < PRIORITY_RANK[floor]:
            return "skipped: priority filter"
    return None


def bell_name(i: int, bell: dict) -> str:
    return f"{i}:{bell.get('type', '')}"


def ring_one(env: dict, name: str, bell: dict) -> str:
    """Ring one bell for one envelope (real or batch_envelope); never raises."""
    kind = bell.get("type", "")
    channel = CHANNELS.get(kind)
    try:
        if channel is None:
            raise ValueError(f"unknown doorbell type {kind!r}")
        result = channel(env, bell)
    except Exception as exc:  # one broken bell must not silence the rest
        result = f"error: {exc}"
    _log(f"ring {env['id']} {name} -> {result}")
    return result


def ring(env: dict, bells: list[dict], only: set[str] | None = None) -> dict[str, str]:
    """Ring every bell (or only the named ones) for one message."""
    results: dict[str, str] = {}
    for i, bell in enumerate(bells):
        name = bell_name(i, bell)
        if only is not None and name not in only:
            continue
        try:
            reason = skip_reason(env, bell)
        except ValueError as exc:
            results[name] = f"error: {exc}"
            _log(f"ring {env['id']} {name} -> {results[name]}")
            continue
        if reason:
            results[name] = reason
            continue
        results[name] = ring_one(env, name, bell)
    return results


def ring_batched(envs: list[dict], bells: list[dict], to: str,
                 due: dict[str, set[str]] | None = None) -> dict[tuple[str, str], str]:
    """For each batching bell, ring once for all mails that pass its filters.

    `due` limits each mail to the bells that should ring for it this tick (retries).

    Returns {(mail id, bell name): result} for the pairs it decided. A bell left with
    a single mail is not decided here, so that mail rings exactly as it used to.
    """
    decided: dict[tuple[str, str], str] = {}
    for i, bell in enumerate(bells):
        if not batches(bell):
            continue
        name = bell_name(i, bell)
        group = []
        considered = [env for env in envs if due is None or name in due.get(env["id"], ())]
        for env in considered:
            try:
                reason = skip_reason(env, bell)
            except ValueError as exc:
                reason = f"error: {exc}"
            if reason:
                decided[(env["id"], name)] = reason
            else:
                group.append(env)
        if len(group) < 2:
            for env in considered:
                decided.pop((env["id"], name), None)
            continue
        result = ring_one(batch_envelope(group, to), name, bell)
        for env in group:
            decided[(env["id"], name)] = result
    return decided


def _rung_path(agent: str) -> Path:
    return config.home() / f"rung-{agent}.json"


def load_rung(agent: str) -> list[str]:
    try:
        return json.loads(_rung_path(agent).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def _write_atomic(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".rung-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data))
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def save_rung(agent: str, ids: list[str]) -> None:
    """Atomic: a run cut off mid-write must not forget everything and re-ring it all."""
    _write_atomic(_rung_path(agent), ids[-KEEP_RUNG:])


# ---------------------------------------------------------------- retrying failed bells

RETRYABLE = ("toast", "command", "handoff")  # dispatch keeps its own run state; a re-ring could start a 2nd worker
DEFAULT_RETRIES = 3
DEFAULT_RETRY_BACKOFF_S = 60


def retry_policy(bell: dict) -> tuple[int, int]:
    """(extra attempts after the first failure, first backoff in seconds); doubles each time."""
    if bell.get("type") not in RETRYABLE:
        return 0, DEFAULT_RETRY_BACKOFF_S
    retries = bell.get("retries", DEFAULT_RETRIES)
    backoff = bell.get("retry_backoff_seconds", DEFAULT_RETRY_BACKOFF_S)
    if not isinstance(retries, int) or isinstance(retries, bool) or retries < 0:
        retries = DEFAULT_RETRIES
    if not isinstance(backoff, (int, float)) or isinstance(backoff, bool) or backoff <= 0:
        backoff = DEFAULT_RETRY_BACKOFF_S
    return retries, backoff


def _retry_path(agent: str) -> Path:
    return config.home() / f"ring-retry-{agent}.json"


def load_retry(agent: str) -> dict:
    """{mail id: {bell name: {"attempts", "next_at", "error"}}} for bells still to be retried."""
    try:
        data = json.loads(_retry_path(agent).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_retry(agent: str, pending: dict) -> None:
    _write_atomic(_retry_path(agent), pending)


def settle(env_id: str, results: dict[str, str], bells: list[dict], previous: dict, now: float) -> tuple[dict, list[str]]:
    """Which failed bells of one mail ring again later, and which have given up for good.

    Returns (still pending {bell name: state}, bell names that gave up). Bells that
    succeeded, were skipped, or are not retryable drop out.
    """
    by_name = {bell_name(i, bell): bell for i, bell in enumerate(bells)}
    pending: dict = {}
    gave_up: list[str] = []
    for name, result in results.items():
        if not result.startswith("error") or name not in by_name:
            continue
        retries, backoff = retry_policy(by_name[name])
        attempts = int((previous.get(name) or {}).get("attempts", 0)) + 1
        if attempts <= retries:
            pending[name] = {"attempts": attempts, "next_at": now + backoff * 2 ** (attempts - 1), "error": result}
        elif retries:
            gave_up.append(name)
            _log(f"ring {env_id} {name} gave up after {attempts} attempts: {result}")
    return pending, gave_up
