"""Carry the mailbox between machines through a git remote.

No public IP and no extra API key: every machine already has push access to the
store repository, so `git push` is the send and `git pull` is the receive. Only
the `mailboxes/` path is ever staged, so a store that shares a repository with
other files never commits anything else.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

PUSH_RETRIES = 3


class SyncError(RuntimeError):
    pass


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    if check and proc.returncode != 0:
        raise SyncError(f"git {' '.join(args)} failed: {(proc.stderr or proc.stdout).strip()}")
    return proc


def is_repo(root: Path) -> bool:
    return (root / ".git").exists()


def has_remote(root: Path) -> bool:
    return bool(_git(root, "remote", check=False).stdout.strip())


def pull(root: Path) -> None:
    if is_repo(root) and has_remote(root):
        _git(root, "pull", "--rebase", "--autostash", "--quiet")


def commit(root: Path, message: str) -> bool:
    """Stage mailboxes/ only and commit. Returns False when there was nothing to commit."""
    if not is_repo(root):
        return False
    _git(root, "add", "-A", "--", "mailboxes")
    staged = _git(root, "diff", "--cached", "--quiet", "--", "mailboxes", check=False)
    if staged.returncode == 0:
        return False
    _git(root, "commit", "--quiet", "-m", message, "--", "mailboxes")
    return True


def push(root: Path) -> None:
    if not (is_repo(root) and has_remote(root)):
        return
    for attempt in range(PUSH_RETRIES):
        proc = _git(root, "push", "--quiet", check=False)
        if proc.returncode == 0:
            return
        if attempt == PUSH_RETRIES - 1:
            raise SyncError(f"git push failed: {(proc.stderr or proc.stdout).strip()}")
        # Someone else pushed first; envelopes have unique names, so a rebase is safe.
        _git(root, "pull", "--rebase", "--quiet")


def clone(url: str, dest: Path, branch: str) -> None:
    """Clone only the mailbox branch of the store repository."""
    proc = subprocess.run(
        ["git", "clone", "--quiet", "--single-branch", "--branch", branch, url, str(dest)],
        capture_output=True, text=True, errors="replace", timeout=300, check=False,
    )
    if proc.returncode != 0:
        raise SyncError(f"git clone failed: {proc.stderr.strip()}")


def sync(root: Path, message: str = "relay: sync mailboxes") -> bool:
    """Commit local mailbox changes, pull others', push. Returns True if we committed."""
    root = Path(root)
    committed = commit(root, message)
    pull(root)
    push(root)
    return committed
