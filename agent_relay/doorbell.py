"""Ring when new mail lands.

A doorbell is only a nudge; the envelope in the store is the truth. Every channel
is best-effort: one failing channel is logged and the others still ring.

Channels
  toast    Windows toast through the built-in WinRT API (no module to install)
  command  run an argv list with {id} {from} {to} {subject} {kind} {summary}
           filled in; no shell, so mail text cannot inject commands
  handoff  for kind=task with a "project" path: drop a dropoff/pickup card in
           <project>/.claude/handoffs/ so the next session's /pickup sees it
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from . import config

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
    proc = subprocess.run(
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
    values = fields(env)
    filled = [str(arg).format_map(values) for arg in argv]
    proc = subprocess.run(
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


def handoff_card(env: dict) -> str:
    created = datetime.now().strftime("%Y-%m-%d %H:%M")
    return (
        "---\n"
        "status: pending\n"
        f"from: {env['from']} (agent-relay, host {env.get('host', '?')})\n"
        f"from-session: agent-relay:{env['id']}\n"
        f"to: {env['project']}\n"
        f"created: {created}\n"
        f"priority: {env.get('priority', 'normal')}\n"
        "notify: rung\n"
        "---\n\n"
        f"# {env['subject']}\n\n"
        "## 要做什麼\n"
        f"{env['body']}\n\n"
        "## 脈絡（陌生 session 也看得懂的程度）\n"
        f"這張卡由 agent-relay 信件 `{env['id']}` 轉成（寄件者 `{env['from']}`，收件信箱 `{env['to']}`）。"
        f"完成後請回信：`python -m agent_relay send --to {env['from']} --kind result "
        f"--reply-to {env['id']} --subject \"done: {env['subject']}\" --body \"<結果與證據>\"`，"
        f"再 `python -m agent_relay ack {env['id']}`。\n\n"
        "## 相關檔案／連結\n"
        + "".join(f"- {ref}\n" for ref in env.get("refs", []))
        + "\n## 完成的定義\n"
        "照「要做什麼」裡的驗收條件；沒寫就以回信附上可驗證證據為準。\n"
    )


def ring_handoff(env: dict, bell: dict) -> str:
    if env["kind"] != "task" or not env.get("project"):
        return "skipped: not a task with a project"
    project = Path(env["project"]).expanduser()
    if not project.is_dir():
        raise FileNotFoundError(f"project folder not on this machine: {project}")
    folder = project / ".claude" / "handoffs"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    card = folder / f"{stamp}-relay-{env['id'][-6:]}.md"
    if not card.exists():
        card.write_text(handoff_card(env), encoding="utf-8", newline="\n")
    return f"ok: {card}"


CHANNELS = {"toast": ring_toast, "command": ring_command, "handoff": ring_handoff}


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
    path = _rung_path(agent)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ids[-KEEP_RUNG:]), encoding="utf-8")
