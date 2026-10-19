"""Register a repeating Windows scheduled task that runs `watch --once`.

Same reasoning as agentdeck's auto-resume task (SanHsien/agentdeck,
autoresume_scheduler.py): an in-process timer freezes while the machine sleeps,
a scheduled task with StartWhenAvailable catches up on wake. The task is defined
in XML because StartWhenAvailable has no schtasks command-line flag.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

TASK_PREFIX = "agent-relay-watch-"

_TASK_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{description}</Description>
  </RegistrationInfo>
  <Triggers>
    <TimeTrigger>
      <StartBoundary>{start}</StartBoundary>
      <Repetition>
        <Interval>PT{minutes}M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <Enabled>true</Enabled>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <ExecutionTimeLimit>PT10M</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{command}</Command>
      <Arguments>{arguments}</Arguments>
      <WorkingDirectory>{workdir}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def task_name(agent: str) -> str:
    return TASK_PREFIX + agent


def interpreter() -> str:
    """Prefer pythonw.exe so the repeating task does not flash a console window."""
    exe = Path(sys.executable)
    windowless = exe.with_name("pythonw.exe")
    return str(windowless if windowless.is_file() else exe)


def build_task_xml(agent: str, minutes: int, command: str, workdir: str) -> str:
    if not 1 <= minutes <= 1440:
        raise ValueError("minutes must be between 1 and 1440")
    return _TASK_XML.format(
        description=escape(f"agent-relay: check mailbox '{agent}' and ring doorbells"),
        start=datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        minutes=minutes,
        command=escape(command),
        arguments=escape(f"-m agent_relay watch --agent {agent} --once"),
        workdir=escape(workdir),
    )


def _schtasks(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["schtasks", *args], capture_output=True, text=True, errors="replace", timeout=30, check=False
    )


def install(agent: str, minutes: int) -> str:
    workdir = str(Path(__file__).resolve().parent.parent)
    xml = build_task_xml(agent, minutes, interpreter(), workdir)
    fd, path = tempfile.mkstemp(suffix=".xml")
    os.close(fd)
    try:
        Path(path).write_text(xml, encoding="utf-16")
        proc = _schtasks("/Create", "/TN", task_name(agent), "/XML", path, "/F")
    finally:
        os.unlink(path)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip())
    return task_name(agent)


def uninstall(agent: str) -> bool:
    return _schtasks("/Delete", "/TN", task_name(agent), "/F").returncode == 0
