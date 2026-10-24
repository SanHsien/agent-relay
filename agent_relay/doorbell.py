"""Ring when new mail lands.

A doorbell is only a nudge; the envelope in the store is the truth. Every channel
is best-effort: one failing channel is logged and the others still ring.

Channels
  toast    Windows toast through the built-in WinRT API (no module to install)
  command  run an argv list with {id} {from} {to} {subject} {kind} {summary}
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


def summary(env: dict) -> str:
    return f"[agent-relay] {env['from']} -> {env['to']} ({env['kind']}): {env['subject']}"


def fields(env: dict) -> dict:
    return {
        "id": env["id"],
        "from": env["from"],
        "to": env["to"],
        "subject": env["subject"],
        "kind": env["kind"],
        "summary": summary(env),
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


def ring(env: dict, bells: list[dict]) -> dict[str, str]:
    results: dict[str, str] = {}
    for i, bell in enumerate(bells):
        kind = bell.get("type", "")
        name = f"{i}:{kind}"
        wanted = bell.get("kinds")
        if wanted and env["kind"] not in wanted:
            results[name] = "skipped: kind filter"
            continue
        channel = CHANNELS.get(kind)
        try:
            if channel is None:
                raise ValueError(f"unknown doorbell type {kind!r}")
            results[name] = channel(env, bell)
        except Exception as exc:  # one broken bell must not silence the rest
            results[name] = f"error: {exc}"
        _log(f"ring {env['id']} {name} -> {results[name]}")
    return results


def _rung_path(agent: str) -> Path:
    return config.home() / f"rung-{agent}.json"


def load_rung(agent: str) -> list[str]:
    try:
        return json.loads(_rung_path(agent).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def save_rung(agent: str, ids: list[str]) -> None:
    """Atomic: a run cut off mid-write must not forget everything and re-ring it all."""
    path = _rung_path(agent)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".rung-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(ids[-KEEP_RUNG:]))
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
