"""Process helpers shared by dispatch and the store lock."""

from __future__ import annotations

import os
import subprocess
import sys


def pid_alive(pid) -> bool:
    """Is that dispatch process still running? Never signals it (os.kill on Windows would end it)."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


# The scheduled watch runs under pythonw.exe, which has no console. Every console program it
# starts (git, ssh-keygen, schtasks) then gets a console of its own, and with Windows Terminal
# as the default terminal that is a new tab; one whose program exits non-zero stays open,
# empty. Seen live: a dozen blank tabs from one heartbeat sync.
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def run(*args, **kwargs) -> subprocess.CompletedProcess:
    """subprocess.run that never opens a console window on Windows."""
    if CREATE_NO_WINDOW:
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | CREATE_NO_WINDOW
    return subprocess.run(*args, **kwargs)
