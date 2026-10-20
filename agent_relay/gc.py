"""`relay gc`: clean up what finished dispatch work leaves behind (SPEC section 17).

Dry run by default. Only finished work older than the cut-off is touched:
a run whose local record says it replied (never running or queued), its worktree,
its local relay/<id> branch, the remote branch this machine pushed (only once the
commander answered accepted/rejected), this mailbox's done/ mail, and the
commander's accept logs.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

from . import config, dispatch
from .store import Store

DEFAULT_DAYS = 14


class GcError(RuntimeError):
    pass


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), "-c", "core.hooksPath=" + os.devnull, *args],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120, check=False)


def _finished_runs(cutoff: float) -> dict[str, dict]:
    runs = config.home() / "runs"
    out = {}
    for state_file in runs.glob("*/state.json") if runs.is_dir() else []:
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        finished = state.get("finished")
        if state.get("phase") == "replied" and isinstance(finished, (int, float)) and finished < cutoff:
            out[state_file.parent.name] = state
    return out


def _answered(cfg: dict, agent: str) -> set[str]:
    """Reply ids the commander answered with an accepted:/rejected: note (relay accept)."""
    out = set()
    for _, env in Store(cfg["store"]).list(agent, ("new", "cur", "done")):
        if env["kind"] == "note" and env.get("reply_to") and env["subject"].startswith(("accepted:", "rejected:")):
            out.add(env["reply_to"])
    return out


def _repos(cfg: dict, extra: list[str]) -> list[Path]:
    """Repos that may hold relay worktrees: --repo, and the repos directly under dispatch.repo_roots."""
    found = [Path(r).resolve() for r in extra]
    for root in (cfg.get("dispatch") or {}).get("repo_roots") or []:
        for candidate in Path(root).glob(f"*/{dispatch.WORKTREE_DIR}"):
            found.append(candidate.parent.resolve())
    return sorted(set(found))


def plan(cfg: dict, agent: str, days: float = DEFAULT_DAYS, repos: list[str] | None = None,
         now: float | None = None) -> list[dict]:
    """What gc would do, as a list of actions."""
    now = now or time.time()
    max_age_h = (cfg.get("dispatch") or {}).get("max_age_hours", dispatch.DEFAULT_MAX_AGE_H)
    if isinstance(max_age_h, (int, float)) and days * 24 < max_age_h:
        # The local run record is what refuses a replayed signed task (SPEC 13); keep it at least as
        # long as a task is accepted at all.
        raise GcError(f"--older-than {days} days is shorter than dispatch.max_age_hours ({max_age_h} h)")
    cutoff = now - days * 86400
    finished = _finished_runs(cutoff)
    answered = _answered(cfg, agent)
    actions: list[dict] = []
    for repo in _repos(cfg, repos or []):
        for wt in sorted((repo / dispatch.WORKTREE_DIR).glob("*")):
            if wt.name in finished:
                actions.append({"kind": "worktree", "repo": str(repo), "path": str(wt), "id": wt.name})
        branches = _git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/relay/").stdout.split()
        for branch in branches:
            env_id = branch[len("relay/"):]
            if env_id in finished:
                actions.append({"kind": "branch", "repo": str(repo), "branch": branch, "id": env_id})
                state = finished[env_id]
                if state.get("pushed_ref") == f"refs/heads/{branch}" and state.get("reply_id") in answered:
                    actions.append({"kind": "remote-branch", "repo": str(repo), "ref": state["pushed_ref"], "id": env_id})
    for env_id in sorted(finished):
        actions.append({"kind": "run-dir", "path": str(config.home() / "runs" / env_id), "id": env_id})
    store = Store(cfg["store"])
    for _, env in store.list(agent, ("done",)):
        created = datetime.fromisoformat(str(env["created_at"]).replace("Z", "+00:00")).timestamp()
        if created < cutoff:
            actions.append({"kind": "done-mail", "path": str(store.box(agent, "done") / f"{env['id']}.json"),
                            "id": env["id"]})
    accepts = config.home() / "accepts"
    for folder in sorted(accepts.glob("*")) if accepts.is_dir() else []:
        if folder.is_dir() and max((f.stat().st_mtime for f in folder.iterdir()), default=0) < cutoff:
            actions.append({"kind": "accept-logs", "path": str(folder), "id": folder.name})
    return actions


def apply(actions: list[dict]) -> list[dict]:
    """Carry out the actions; each one records its own outcome."""
    for action in actions:
        kind = action["kind"]
        try:
            if kind == "worktree":
                proc = _git(Path(action["repo"]), "worktree", "remove", "--force", action["path"])
                if proc.returncode != 0:
                    raise GcError((proc.stderr or proc.stdout).strip())
            elif kind == "branch":
                proc = _git(Path(action["repo"]), "branch", "-D", action["branch"])
                if proc.returncode != 0:
                    raise GcError((proc.stderr or proc.stdout).strip())
            elif kind == "remote-branch":
                proc = _git(Path(action["repo"]), "push", "--quiet", "--no-verify", "origin", "--delete", action["ref"])
                if proc.returncode != 0:
                    raise GcError((proc.stderr or proc.stdout).strip())
            elif kind in ("run-dir", "accept-logs"):
                shutil.rmtree(action["path"])
            elif kind == "done-mail":
                Path(action["path"]).unlink()
            action["done"] = True
        except (OSError, GcError, subprocess.SubprocessError) as exc:
            action["done"] = False
            action["error"] = str(exc)
    return actions


def render(actions: list[dict], applied: bool) -> str:
    if not actions:
        return "nothing to clean up"
    verb = "" if applied else "would "
    lines = []
    for a in actions:
        target = a.get("branch") or a.get("ref") or a.get("path")
        status = "" if not applied else (" ok" if a.get("done") else f" FAILED: {a.get('error')}")
        lines.append(f"{verb}remove {a['kind']}: {target}{status}")
    counts: dict[str, int] = {}
    for a in actions:
        counts[a["kind"]] = counts.get(a["kind"], 0) + 1
    lines.append("total: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
                 + ("" if applied else "  (dry run; add --apply)"))
    return "\n".join(lines)

