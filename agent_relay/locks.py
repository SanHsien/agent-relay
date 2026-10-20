"""Check-out locks and the check-in/out audit trail, shared through the store.

    <store>/claims/<repo key>/<claim id>.json      who holds which paths of a repo, until when
    <store>/audit/<YYYY-MM-DD>/<stamp>-<claim>-<event>.json   checkout / checkin / blocked / released

A claim names repo-relative path patterns (`calc.py`, `src/api/**`, `docs/`).
Two claims conflict when their patterns may touch the same file; the overlap
test is the one claude-code-projects-workflow uses before dispatching parallel
worktrees (SanHsien/claude-code-projects-workflow, worktree_tasks.py, MIT).

Claims travel between machines with the rest of the store (git), so a lock taken
on one machine is seen on another after its next pull. On one machine, taking a
lock is serialised by an O_EXCL lock file so two runs cannot both win.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import secrets
import socket
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

CLAIM_SCHEMA = "agent-relay/claim@1"
AUDIT_SCHEMA = "agent-relay/audit@1"
WILDCARDS = set("*?[")
LOCK_WAIT_S = 10.0
STALE_LOCK_S = 60.0


class ClaimConflict(RuntimeError):
    def __init__(self, message: str, holders: list[dict]):
        super().__init__(message)
        self.holders = holders


# ---------------------------------------------------------------- path patterns (from worktree_tasks.py)


def normalize(path: str) -> str:
    return path.replace("\\", "/").strip("/")


def match_path(path: str, pattern: str) -> bool:
    """Segment-wise match. `**` eats zero or more segments; a trailing / means the whole folder."""
    path = normalize(path)
    if pattern.endswith("/"):
        pattern = pattern.rstrip("/") + "/**"
    return _match_parts(path.split("/"), normalize(pattern).split("/"))


def _match_parts(parts: list[str], pats: list[str]) -> bool:
    if not pats:
        return not parts
    head, rest = pats[0], pats[1:]
    if head == "**":
        return any(_match_parts(parts[i:], rest) for i in range(len(parts) + 1))
    if not parts:
        return False
    return fnmatch.fnmatchcase(parts[0].lower(), head.lower()) and _match_parts(parts[1:], rest)


def literal_prefix(pattern: str) -> str:
    if pattern.endswith("/"):
        pattern = pattern.rstrip("/") + "/**"
    out = []
    for part in normalize(pattern).split("/"):
        if WILDCARDS & set(part):
            break
        out.append(part)
    return "/".join(out)


def may_overlap(a: str, b: str) -> bool:
    """Conservatively: can the two patterns match the same (possibly not yet existing) file?"""
    pa, pb = literal_prefix(a).lower(), literal_prefix(b).lower()
    if not pa or not pb:
        return True
    return (pa == pb or pa.startswith(pb + "/") or pb.startswith(pa + "/")
            or match_path(pa, b) or match_path(pb, a))


def validate_paths(paths: object) -> list[str]:
    if not isinstance(paths, list) or not paths or len(paths) > 50:
        raise ValueError("paths must be a list of 1..50 repo-relative patterns")
    out = []
    for raw in paths:
        if not isinstance(raw, str) or not raw.strip() or any(ch in raw for ch in (chr(10), chr(13))):
            raise ValueError(f"bad path pattern: {raw!r}")
        path = normalize(raw.strip())
        if not path or path.startswith("../") or "/../" in f"/{path}/" or re.match(r"^[A-Za-z]:", path):
            raise ValueError(f"path pattern must stay inside the repo: {raw!r}")
        out.append(path + ("/" if raw.rstrip().endswith(("/", "\\")) else ""))
    return out


# ---------------------------------------------------------------- repo identity


def _git(repo: Path, *args: str) -> str | None:
    try:
        proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=20, check=False)
    except (subprocess.SubprocessError, OSError):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def repo_id(repo: Path) -> str:
    """The same repo on any machine: its origin URL without credentials; else its local git folder."""
    url = _git(repo, "config", "--get", "remote.origin.url")
    if url:
        url = re.sub(r"^([a-z+]+://)[^/@]*@", r"\1", url.strip())  # drop credentials
        url = re.sub(r"^git@([^:]+):", r"https://\1/", url)
        return re.sub(r"\.git$", "", url.rstrip("/")).lower()
    common = _git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if not common:
        raise ValueError(f"{repo} is not a git repository")
    return "local:" + str(Path(common).resolve()).lower()


def repo_key(rid: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", rid.split("/")[-1].lower()).strip("-")[:40] or "repo"
    return f"{slug}-{hashlib.sha1(rid.encode('utf-8')).hexdigest()[:12]}"


def toplevel(path: Path) -> Path | None:
    """The repository a file belongs to; for a linked worktree, the main checkout's identity still applies."""
    folder = path if path.is_dir() else path.parent
    while not folder.exists() and folder != folder.parent:
        folder = folder.parent
    top = _git(folder, "rev-parse", "--show-toplevel")
    return Path(top) if top else None


# ---------------------------------------------------------------- store files


def _now() -> float:
    return time.time()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(".tmp-" + path.name)
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def claims_dir(store: Path, key: str) -> Path:
    return Path(store) / "claims" / key


def active_claims(store: Path, key: str, now: float | None = None) -> list[dict]:
    now = now or _now()
    out = []
    folder = claims_dir(store, key)
    if not folder.is_dir():
        return out
    for path in folder.glob("*.json"):
        if path.name.startswith("."):
            continue
        try:
            claim = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (isinstance(claim, dict) and claim.get("schema") == CLAIM_SCHEMA and claim.get("id") == path.stem
                and isinstance(claim.get("paths"), list) and float(claim.get("expires", 0)) > now):
            out.append(claim)
    return sorted(out, key=lambda c: c.get("created", 0))


def conflicts(paths: list[str], claims: list[dict], holder: str) -> list[dict]:
    """Claims held by someone else whose paths may touch any of `paths`."""
    hits = []
    for claim in claims:
        if claim.get("holder") == holder:
            continue
        if any(may_overlap(mine, theirs) for mine in paths for theirs in claim["paths"]):
            hits.append(claim)
    return hits


def holders_of(file_rel: str, claims: list[dict], holder: str) -> list[dict]:
    """Claims held by someone else that cover this concrete file."""
    return [c for c in claims if c.get("holder") != holder and any(match_path(file_rel, p) for p in c["paths"])]


def audit(store: Path, event: str, **fields) -> Path:
    """One file per event: appends from several machines never collide in git."""
    now = _now()
    claim = re.sub(r"[^A-Za-z0-9_-]", "", str(fields.get("claim", "none")))[:40] or "none"
    name = f"{datetime.fromtimestamp(now, timezone.utc).strftime('%H%M%S%f')}-{claim}-{event}-{secrets.token_hex(2)}.json"
    path = Path(store) / "audit" / datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d") / name
    _write_json(path, {"schema": AUDIT_SCHEMA, "event": event, "at": _iso(now), "host": socket.gethostname(), **fields})
    return path


class _LocalLock:
    """O_EXCL lock file around check-then-write; a lock older than STALE_LOCK_S is broken."""

    def __init__(self, folder: Path):
        self.path = folder / ".lock"

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + LOCK_WAIT_S
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                return self
            except FileExistsError:
                try:
                    if time.time() - self.path.stat().st_mtime > STALE_LOCK_S:
                        self.path.unlink()
                        continue
                except OSError:
                    continue
                if time.monotonic() > deadline:
                    raise TimeoutError(f"could not lock {self.path}")
                time.sleep(0.05)

    def __exit__(self, *exc):
        try:
            self.path.unlink()
        except OSError:
            pass


def checkout(store: Path, repo: Path, holder: str, paths: list[str], ttl_s: float, **extra) -> dict:
    """Take a claim, or raise ClaimConflict naming who holds the overlapping paths."""
    paths = validate_paths(paths)
    rid = repo_id(repo)
    key = repo_key(rid)
    with _LocalLock(claims_dir(store, key)):
        clash = conflicts(paths, active_claims(store, key), holder)
        if clash:
            audit(store, "blocked", repo=rid, holder=holder, paths=paths,
                  blocked_by=[{"claim": c["id"], "holder": c["holder"], "paths": c["paths"]} for c in clash], **extra)
            names = "; ".join(f"{c['holder']} 持有 {', '.join(c['paths'])}（到 {c.get('expires_at', '?')}）" for c in clash)
            raise ClaimConflict(f"範圍和別人簽出的檔案重疊：{names}", clash)
        now = _now()
        claim = {"schema": CLAIM_SCHEMA, "id": f"c{int(now)}{secrets.token_hex(3)}", "repo": rid, "key": key,
                 "holder": holder, "paths": paths, "host": socket.gethostname(), "created": now,
                 "created_at": _iso(now), "expires": now + ttl_s, "expires_at": _iso(now + ttl_s), **extra}
        _write_json(claims_dir(store, key) / f"{claim['id']}.json", claim)
    audit(store, "checkout", claim=claim["id"], repo=rid, holder=holder, paths=paths, **extra)
    return claim


def release(store: Path, claim: dict, changed: list[str] | None = None, **extra) -> None:
    path = claims_dir(store, claim["key"]) / f"{claim['id']}.json"
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    audit(store, "checkin", claim=claim["id"], repo=claim["repo"], holder=claim["holder"],
          paths=claim["paths"], changed=changed or [], **extra)


def out_of_scope(changed: list[str], paths: list[str]) -> list[str]:
    return [f for f in changed if not any(match_path(f, p) for p in paths)]
