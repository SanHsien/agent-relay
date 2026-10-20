"""Carry the mailbox between machines through a git remote.

No public IP and no extra API key: every machine already has push access to the
store repository, so `git push` is the send and `git pull` is the receive. Only
the `mailboxes/` path is ever staged (half-written `.tmp-*` files excluded), so a
store that shares a repository with other files never commits anything else.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

PUSH_RETRIES = 3
TIMEOUT = 120
SHARED_DIRS = ("mailboxes", "claims", "audit")  # everything relay shares between machines


class SyncError(RuntimeError):
    pass


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=TIMEOUT,
            check=False,
        )
    except (subprocess.SubprocessError, OSError) as exc:  # hung network, git missing
        raise SyncError(f"git {' '.join(args)} failed: {exc}") from exc
    if check and proc.returncode != 0:
        raise SyncError(f"git {' '.join(args)} failed: {(proc.stderr or proc.stdout).strip()}")
    return proc


def is_repo(root: Path) -> bool:
    return (root / ".git").exists()


def has_remote(root: Path) -> bool:
    return bool(_git(root, "remote", check=False).stdout.strip())


def _rebasing(root: Path) -> bool:
    git_dir = root / ".git"
    return (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists()


def pull(root: Path) -> None:
    """Rebase onto the remote. A failed rebase is aborted so the store is never left stuck."""
    root = Path(root)
    if not (is_repo(root) and has_remote(root)):
        return
    try:
        _git(root, "pull", "--rebase", "--autostash", "--quiet")
    except SyncError:
        if _rebasing(root):
            _git(root, "rebase", "--abort", check=False)
        raise


def commit(root: Path, message: str) -> bool:
    """Stage relay's own folders only and commit. Returns False when there was nothing to commit."""
    root = Path(root)
    dirs = [d for d in SHARED_DIRS if (root / d).is_dir() or _tracked(root, d)]
    if not is_repo(root) or not dirs:
        return False
    excludes = [f":(exclude,glob){d}/**/.tmp-*" for d in dirs] + [f":(exclude,glob){d}/**/.lock" for d in dirs]
    _git(root, "add", "-A", "--", *dirs, *excludes)
    staged = _git(root, "diff", "--cached", "--quiet", "--", *dirs, check=False)
    if staged.returncode == 0:
        return False
    # An empty folder (all claims released) is on disk but known to neither the index nor
    # HEAD; naming it to `git commit` fails the whole commit ("pathspec did not match").
    dirs = [d for d in dirs if _tracked(root, d) or _in_head(root, d)]
    _git(root, "commit", "--quiet", "-m", message, "--", *dirs)
    return True


def _tracked(root: Path, folder: str) -> bool:
    """A shared folder that was deleted locally (last claim released) still needs its removal committed."""
    return bool(_git(root, "ls-files", "--", folder, check=False).stdout.strip())


def _in_head(root: Path, folder: str) -> bool:
    return bool(_git(root, "ls-tree", "--name-only", "HEAD", "--", folder, check=False).stdout.strip())


def push(root: Path) -> None:
    root = Path(root)
    if not (is_repo(root) and has_remote(root)):
        return
    for attempt in range(PUSH_RETRIES):
        proc = _git(root, "push", "--quiet", check=False)
        if proc.returncode == 0:
            return
        if attempt == PUSH_RETRIES - 1:
            raise SyncError(f"git push failed: {(proc.stderr or proc.stdout).strip()}")
        # Someone else pushed first; envelopes have unique names, so a rebase is safe.
        pull(root)


def clone(url: str, dest: Path, branch: str) -> None:
    """Clone only the mailbox branch of the store repository."""
    try:
        proc = subprocess.run(
            ["git", "clone", "--quiet", "--single-branch", "--branch", branch, url, str(dest)],
            capture_output=True, text=True, errors="replace", timeout=300, check=False,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise SyncError(f"git clone failed: {exc}") from exc
    if proc.returncode != 0:
        raise SyncError(f"git clone failed: {proc.stderr.strip()}")


def sync(root: Path, message: str = "relay: sync mailboxes") -> bool:
    """Commit local mailbox changes, pull others', push. Returns True if we committed."""
    root = Path(root)
    committed = commit(root, message)
    pull(root)
    push(root)
    return committed
