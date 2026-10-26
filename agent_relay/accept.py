"""`relay accept`: the commander checks a worker's pushed branch again and merges it (SPEC section 14).

The receiver already ran its gates, but a reply is only a claim. The commander
fetches the exact commit the (signed) reply names, runs its *own* verification
command in a throwaway worktree, and merges with --no-ff only when that passes.
Every outcome is written to the audit log and answered to the receiver.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from . import config, dispatch, envelope, gates, gitsync, locks, procs, signing
from .store import Store

SHA_LEN = 40


class AcceptError(RuntimeError):
    """The branch is refused; the message says why."""


def _git(repo: Path, *args: str, check: bool = True) -> str:
    proc = procs.run(["git", "-C", str(repo), *gates.SAFE_GIT, *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=300, check=False)
    if check and proc.returncode != 0:
        raise AcceptError(f"git {' '.join(args)}: {(proc.stderr or proc.stdout).strip()}")
    return proc.stdout.strip()


def load_reply(cfg: dict, agent: str, reply_id: str) -> dict:
    store = Store(cfg["store"])
    hit = store.find(agent, reply_id)
    if hit is None:
        raise AcceptError(f"no message {reply_id} in mailbox {agent}")
    env = envelope.loads(hit[1].read_text(encoding="utf-8"))
    if env["to"] != agent or env["kind"] != "result" or not isinstance(env.get("result"), dict):
        raise AcceptError(f"{reply_id} is not a dispatch reply (kind=result with a result object)")
    return env


def check_reply(env: dict, cfg: dict) -> dict:
    """The result fields accept relies on, after the signature and shape checks."""
    settings = cfg.get("accept") or {}
    if settings.get("require_signature", True):
        problem = signing.verify(env, signing.settings(cfg, config.home())[1])
        if problem:
            raise AcceptError(f"reply from {env['from']!r} failed the signature check: {problem}")
    result = env["result"]
    if result.get("status") != "ok":
        raise AcceptError(f"the receiver reported status {result.get('status')!r}, not ok")
    for key in ("commit", "base"):
        value = result.get(key)
        if not (isinstance(value, str) and len(value) == SHA_LEN and all(c in "0123456789abcdef" for c in value)):
            raise AcceptError(f"reply has no full {key} sha")
    pushed = result.get("pushed")
    if not isinstance(pushed, dict) or pushed.get("commit") != result["commit"]:
        raise AcceptError("the receiver did not push this commit (dispatch.push_branch)")
    ref = pushed.get("ref")
    branch = result.get("branch")
    if not (isinstance(branch, str) and branch.startswith("relay/") and ref == f"refs/heads/{branch}"
            and envelope.safe_name(branch[len("relay/"):], envelope.ID_RE)):
        raise AcceptError("reply names an unexpected branch")
    return result


def _is_ancestor(repo: Path, older: str, newer: str) -> bool:
    return procs.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", older, newer],
                          capture_output=True, check=False, timeout=120).returncode == 0


def preflight(cfg: dict, env: dict, repo: Path, into: str | None) -> tuple[str, list[str]]:
    """Local problems (dirty tree, wrong branch, no verify command): fix them and run accept again.

    These raise AcceptError before anything is recorded: the receiver must not be told
    its work was rejected because of the commander's own checkout.
    """
    command = gates.verify_command(repo, ((cfg.get("accept") or {}).get("verify") or {}))
    if not command:
        raise AcceptError("no accept.verify.command in this machine's config; refusing to merge unverified work")
    current = _git(repo, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    if not current:
        raise AcceptError("the target repo has a detached HEAD; check out the branch to merge into")
    if into and into != current:
        raise AcceptError(f"--into {into} is not the checked-out branch ({current}); check it out first")
    if _git(repo, "status", "--porcelain", "--untracked-files=no"):
        raise AcceptError("the target repo has uncommitted changes; commit or stash them first")
    commit = (env.get("result") or {}).get("commit")
    if isinstance(commit, str) and len(commit) == SHA_LEN and _git(repo, "cat-file", "-t", commit, check=False) == "commit" \
            and _is_ancestor(repo, commit, "HEAD"):
        raise AcceptError(f"{commit[:12]} is already merged into {current}")
    return current, command


def accept(cfg: dict, agent: str, reply_id: str, repo: Path, into: str | None = None,
           sync: bool = True, push: bool = False) -> dict:
    """Fetch, merge in a scratch worktree, verify the merge, fast-forward. A refusal is status 'rejected'.

    Raises AcceptError only for problems on this side (see preflight); those record nothing.
    """
    repo = Path(repo).resolve()
    notes: list[str] = []
    if sync:
        try:
            gitsync.pull(Path(cfg["store"]))
        except gitsync.SyncError as exc:
            notes.append(f"store pull failed, using the local copy: {exc}")
    env = load_reply(cfg, agent, reply_id)
    current, command = preflight(cfg, env, repo, into)
    report: dict = {"reply": reply_id, "repo": str(repo), "into": current, "status": "rejected"}
    try:
        result = check_reply(env, cfg)
        report.update(branch=result["branch"], commit=result["commit"], base=result["base"])
        if result["branch"] != f"relay/{env.get('reply_to')}":
            raise AcceptError(f"reply {reply_id} answers {env.get('reply_to')} but names branch {result['branch']}")
        _git(repo, "fetch", "--quiet", "--no-tags", "origin", result["pushed"]["ref"])
        fetched = _git(repo, "rev-parse", "FETCH_HEAD")
        if fetched != result["commit"]:
            # Someone pushed to the branch after the receiver replied: that is not what was verified.
            raise AcceptError(f"origin {result['branch']} is at {fetched[:12]}, the reply names {result['commit'][:12]}")
        if not _is_ancestor(repo, result["base"], result["commit"]):
            raise AcceptError(f"commit {result['commit'][:12]} does not descend from base {result['base'][:12]}")
        head = _git(repo, "rev-parse", "HEAD")
        message = f"relay accept: {result['branch']} (reply {reply_id})"
        merged, report["verify"] = merge_and_verify(repo, head, result["commit"], message, command, cfg, reply_id, notes)
        if report["verify"]["exit"] != 0:
            raise AcceptError(f"verification failed on this machine (exit {report['verify']['exit']}); "
                              f"log {report['verify']['log']}")
        if _git(repo, "rev-parse", "HEAD") != head:
            raise AcceptError(f"{current} moved while verifying; run accept again")
        # The exact merge commit that was verified, nothing else: a fast-forward to it.
        _git(repo, "merge", "--ff-only", "--quiet", merged)
        report.update(status="accepted", merge=merged)
        policy = cfg.get("accept") or {}
        if push or policy.get("push"):
            # The same rule as the receiver's push (SPEC section 14): every push url must be the owner's.
            urls = [u for u in _git(repo, "remote", "get-url", "--push", "--all", "origin").splitlines() if u.strip()]
            bad = [u for u in urls if not dispatch.push_allowed(u, policy)]
            if not urls or bad:
                notes.append(f"merged, not pushed: origin {', '.join(bad or ['(none)'])} is not one of the "
                             "owner's repos or accept.push_remotes")
            else:
                # Only the branch that was verified and fast-forwarded; hooks stay on (the owner's repo).
                proc = procs.run(["git", "-C", str(repo), "push", "--quiet", "origin", current],
                                 capture_output=True, text=True, encoding="utf-8", errors="replace",
                                 check=False, timeout=300)
                if proc.returncode == 0:
                    report["pushed"] = f"origin/{current}"
                else:
                    notes.append(f"merged, but push failed: {(proc.stderr or proc.stdout).strip()[:300]}")
    except AcceptError as exc:
        report["reason"] = str(exc)
    except (subprocess.SubprocessError, OSError) as exc:
        report["reason"] = f"{type(exc).__name__}: {exc}"
    report["notes"] = notes
    try:
        _record(cfg, agent, env, report, sync)
    except Exception as exc:  # noqa: BLE001 - the merge (if any) already happened; say so instead of a traceback
        notes.append(f"could not record or answer: {type(exc).__name__}: {exc}")
    return report


def merge_and_verify(repo: Path, head: str, commit: str, message: str, command: list[str], cfg: dict,
                     reply_id: str, notes: list[str]) -> tuple[str, dict]:
    """Merge `commit` into `head` in a throwaway worktree and run the verify command on that merge.

    What gets verified is what would land: the target branch may have moved past the
    task's base since it was sent.
    """
    settings = (cfg.get("accept") or {}).get("verify") or {}
    timeout_s = gates.timeout_seconds(settings)
    run_dir = config.home() / "accepts" / reply_id
    holder = Path(tempfile.mkdtemp(prefix="relay-accept-"))
    scratch = holder / "wt"
    try:
        try:
            gates._scratch_worktree(repo, head, scratch)
        except subprocess.CalledProcessError as exc:
            raise AcceptError(f"could not create a verification worktree: {exc.stderr!r}") from exc
        try:
            proc = procs.run(["git", "-C", str(scratch), *gates.SAFE_GIT, "merge", "--no-ff", "--no-edit",
                                   "-m", message, commit], capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", check=False, timeout=300)
            if proc.returncode != 0:
                raise AcceptError(f"does not merge cleanly into the target branch: {(proc.stderr or proc.stdout).strip()[:500]}")
            merged = _git(scratch, "rev-parse", "HEAD")
            run = gates.run_logged(command, scratch, run_dir / f"verify-{time.strftime('%Y%m%d-%H%M%S')}.log", timeout_s)
        finally:
            gates._drop_worktree(repo, scratch, notes)
    finally:
        shutil.rmtree(holder, ignore_errors=True)
    return merged, {k: run[k] for k in ("command", "exit", "seconds", "log", "sha256", "tail")}


def _record(cfg: dict, agent: str, env: dict, report: dict, sync: bool) -> None:
    """Audit event plus an answer to the receiver: it learns whether its branch was taken."""
    store_path = Path(cfg["store"])
    verify_run = report.get("verify") or {}
    try:
        locks.audit(store_path, "accept", claim="none", env=env.get("reply_to", env["id"]), reply=env["id"],
                    status=report["status"], commit=report.get("commit"), merge=report.get("merge"),
                    reason=report.get("reason"), verify_sha256=verify_run.get("sha256"))
    except OSError:
        pass
    status = report["status"]
    body = (f"status: {status}\nbranch: {report.get('branch')}\ncommit: {report.get('commit')}\n"
            + (f"merge: {report['merge']}（{report.get('into')}）\n" if report.get("merge") else "")
            + (f"原因: {report['reason']}\n" if report.get("reason") else "")
            + (f"\n指揮者端驗證：`{verify_run['command']}` → exit {verify_run['exit']}，sha256 {verify_run['sha256']}\n"
               f"```text\n{verify_run['tail']}\n```\n" if verify_run else ""))
    note = envelope.build(sender=agent, to=env["from"], subject=f"{status}: {env['subject']}"[:200], body=body,
                          kind="note", reply_to=env["id"], thread=env.get("thread"),
                          priority="normal" if status == "accepted" else "high")
    key = signing.settings(cfg, config.home())[0]
    if key:
        note = signing.sign(note, key)
    store = Store(cfg["store"])
    store.deliver(note)
    store.ack(agent, env["id"])
    report["answer_id"] = note["id"]
    if sync:
        try:
            gitsync.sync(store_path, f"relay: {agent} accept {env['id']} -> {status}")
        except gitsync.SyncError as exc:
            report["notes"].append(f"store sync failed: {exc}")
