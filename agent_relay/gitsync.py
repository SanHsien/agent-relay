"""Carry the mailbox between machines through a git remote.

No public IP and no extra API key: every machine already has push access to the
store repository, so `git push` is the send and `git pull` is the receive. Only
the `mailboxes/` path is ever staged (half-written `.tmp-*` files excluded), so a
store that shares a repository with other files never commits anything else.
"""

from __future__ import annotations

import functools
import os
import secrets
import subprocess
import threading
import time
from pathlib import Path

from .procs import pid_alive
from . import procs

PUSH_RETRIES = 3
TIMEOUT = 120
SHARED_DIRS = ("mailboxes", "claims", "audit", "hosts")  # everything relay shares between machines
LOCK_WAIT_S = 300
LOCK_STALE_S = 900  # longer than any pull + commit + push with retries


class SyncError(RuntimeError):
    pass


_held = threading.local()
_thread_lock = threading.RLock()


def _locked(func):
    """One store operation at a time on this machine.

    The doorbell opens a tab per task, and each tab pulls and pushes the same clone:
    concurrent `git pull`s race on FETCH_HEAD ("Cannot rebase onto multiple branches")
    and index.lock. An O_EXCL file in .git serialises them across processes; nested
    calls (sync -> pull) in the same thread do not take it twice.
    """
    @functools.wraps(func)
    def wrapper(root, *args, **kwargs):
        git_dir = Path(root) / ".git"
        if not git_dir.is_dir():
            return func(root, *args, **kwargs)
        with _thread_lock:
            depth = getattr(_held, "depth", 0)
            if depth:
                _held.depth = depth + 1
                try:
                    return func(root, *args, **kwargs)
                finally:
                    _held.depth -= 1
            lock = git_dir / "agent-relay-sync.lock"
            token = _acquire(lock)
            _held.depth = 1
            try:
                return func(root, *args, **kwargs)
            finally:
                _held.depth = 0
                _release(lock, token)
    return wrapper


def _acquire(lock: Path) -> str:
    token = f"{os.getpid()}-{secrets.token_hex(4)}"
    give_up = time.time() + LOCK_WAIT_S
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(token)
            return token
        except (FileExistsError, PermissionError):  # Windows: PermissionError while the old file is being deleted
            try:
                owner = lock.read_text(encoding="utf-8").split("-")[0]
                gone = owner.isdigit() and not pid_alive(int(owner))
                if gone or time.time() - lock.stat().st_mtime > LOCK_STALE_S:  # its owner crashed
                    os.replace(lock, lock.with_name(f"{lock.name}.stale-{token}"))
                    os.unlink(lock.with_name(f"{lock.name}.stale-{token}"))
                    continue
            except OSError:
                continue
            if time.time() > give_up:
                raise SyncError(f"another relay process holds {lock} for more than {LOCK_WAIT_S} s")
            time.sleep(0.5)


def _release(lock: Path, token: str) -> None:
    try:
        if lock.read_text(encoding="utf-8") == token:
            lock.unlink()
    except OSError:
        pass


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        proc = procs.run(
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


@_locked
def pull(root: Path) -> None:
    """Rebase onto the remote. A failed rebase is aborted so the store is never left stuck."""
    root = Path(root)
    if not (is_repo(root) and has_remote(root)):
        return
    try:
        # No directory-rename detection: acking the last message in new/ looks like "new/
        # renamed to done/" to git, and then mail another machine added to new/ conflicts.
        _git(root, "-c", "merge.directoryRenames=false", "pull", "--rebase", "--autostash", "--quiet")
    except SyncError:
        if _rebasing(root):
            _git(root, "rebase", "--abort", check=False)
        raise


@_locked
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


@_locked
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
        proc = procs.run(
            ["git", "clone", "--quiet", "--single-branch", "--branch", branch, url, str(dest)],
            capture_output=True, text=True, errors="replace", timeout=300, check=False,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise SyncError(f"git clone failed: {exc}") from exc
    if proc.returncode != 0:
        raise SyncError(f"git clone failed: {proc.stderr.strip()}")


@_locked
def sync(root: Path, message: str = "relay: sync mailboxes") -> bool:
    """Commit local mailbox changes, pull others', push. Returns True if we committed."""
    root = Path(root)
    committed = commit(root, message)
    pull(root)
    push(root)
    return committed
