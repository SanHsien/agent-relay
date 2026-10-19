"""Run a task envelope in a fresh agent session, isolated in a git worktree.

    commander ──task + "dispatch"──▶ mailbox ──watch──▶ dispatch doorbell
                                                         │ opens a Windows Terminal tab
                                                         ▼
                              relay dispatch <id>:  worktree relay/<id>
                                                    → headless claude / codex / agy
                                                    → result envelope back (reply_to)

The receiver decides what may run: `dispatch.repo_roots` and `dispatch.runtimes`
in config.json. Mail only names a repo and a runtime inside that policy. The
worker commits on its own branch in its own worktree and never pushes; merging is
left to whoever reviews the result.

Command lines come from agent-cortex's launcher (SanHsien/agent-cortex,
paulsha_cortex/coordinator/launcher.py) when it can be imported, so permission
modes stay the ones cortex already hardened; the fallback builders below keep
the same conservative defaults (claude acceptEdits, codex workspace-write sandbox,
agy plan + sandbox).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import config, doorbell, envelope, gitsync
from .store import Store

RUNTIMES = ("claude", "codex", "agy")
MODES = ("build", "plan")
DEFAULT_TIMEOUT_MIN = 30
MAX_TIMEOUT_MIN = 120
WORKTREE_DIR = ".relay-worktrees"
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


class DispatchError(RuntimeError):
    pass


# ---------------------------------------------------------------- contract


def validate_spec(spec: object) -> dict:
    """Check the envelope's "dispatch" object (SPEC section 7)."""
    if not isinstance(spec, dict):
        raise envelope.EnvelopeError("dispatch must be an object")
    if spec.get("runtime") not in RUNTIMES:
        raise envelope.EnvelopeError(f"dispatch.runtime must be one of {RUNTIMES}")
    if not isinstance(spec.get("repo"), str) or not spec["repo"].strip():
        raise envelope.EnvelopeError("dispatch.repo is required")
    if spec.get("mode", "build") not in MODES:
        raise envelope.EnvelopeError(f"dispatch.mode must be one of {MODES}")
    timeout = spec.get("timeout_minutes", DEFAULT_TIMEOUT_MIN)
    if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= MAX_TIMEOUT_MIN:
        raise envelope.EnvelopeError(f"dispatch.timeout_minutes must be 1..{MAX_TIMEOUT_MIN}")
    for key in ("model", "effort", "repo"):
        if key in spec and any(ch in str(spec[key]) for ch in (chr(10), chr(13))):
            raise envelope.EnvelopeError(f"dispatch.{key} must be one line")
    return spec


def policy_check(spec: dict, policy: dict) -> Path:
    """Return the resolved repo, or raise when the receiver has not allowed this."""
    allowed = policy.get("runtimes") or []
    if spec["runtime"] not in allowed:
        raise DispatchError(f"runtime {spec['runtime']!r} not in this machine's dispatch.runtimes {allowed}")
    repo = doorbell.resolve_project(spec["repo"])
    if not doorbell._inside(repo, policy.get("repo_roots") or []):
        raise DispatchError(f"{repo} is not under dispatch.repo_roots {policy.get('repo_roots') or []}")
    if not (repo / ".git").exists():
        raise DispatchError(f"{repo} is not a git repository")
    return repo


# ---------------------------------------------------------------- worktree


def _git(cwd: Path, *args: str) -> str:
    try:
        proc = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=120, check=False)
    except (subprocess.SubprocessError, OSError) as exc:
        raise DispatchError(f"git {' '.join(args)}: {exc}") from exc
    if proc.returncode != 0:
        raise DispatchError(f"git {' '.join(args)}: {(proc.stderr or proc.stdout).strip()}")
    return proc.stdout.rstrip()  # keep leading spaces: `diff --stat` aligns with them


def _exclude(repo: Path) -> None:
    exclude = Path(_git(repo, "rev-parse", "--git-path", "info/exclude").strip())
    if not exclude.is_absolute():
        exclude = repo / exclude
    exclude.parent.mkdir(parents=True, exist_ok=True)
    lines = exclude.read_text(encoding="utf-8").splitlines() if exclude.exists() else []
    if f"/{WORKTREE_DIR}/" not in lines:
        with open(exclude, "a", encoding="utf-8") as fh:
            fh.write(f"\n/{WORKTREE_DIR}/\n")


def make_worktree(repo: Path, env_id: str) -> tuple[Path, str, str]:
    """Create <repo>/.relay-worktrees/<id> on branch relay/<id>. Returns (path, branch, base)."""
    base = _git(repo, "rev-parse", "HEAD").strip()
    branch = f"relay/{env_id}"
    path = repo / WORKTREE_DIR / env_id
    _exclude(repo)
    if not path.exists():
        _git(repo, "worktree", "add", "--quiet", "-b", branch, str(path), base)
    return path, branch, base


# ---------------------------------------------------------------- command lines


def _cortex_launcher(cfg: dict):
    path = os.environ.get("AGENT_RELAY_CORTEX") or (cfg.get("dispatch") or {}).get("cortex_path")
    if path and path not in sys.path:
        sys.path.insert(0, path)
    try:
        from paulsha_cortex.coordinator import launcher  # type: ignore[import-not-found]
    except Exception:  # not installed, or a cortex version we cannot load
        return None
    return launcher


def build_argv(spec: dict, prompt: str, slice_id: str, log_dir: Path, worktree: Path,
               cfg: dict) -> tuple[list[str], str]:
    """Return (argv, source) where source says which builder made it."""
    runtime, plan = spec["runtime"], spec.get("mode", "build") == "plan"
    model = spec.get("model")
    launcher = _cortex_launcher(cfg)
    if launcher is not None:
        common = {"prompt": prompt, "slice_id": slice_id, "log_dir": str(log_dir), "model": model}
        if runtime == "claude":
            argv = launcher.build_claude_argv(worktree=str(worktree), read_only=plan,
                                              commit_required=not plan, **common)
            # cortex enables Remote Control for its own monitor; a relay run has no use for it.
            return [a for a in argv if a != "--remote-control"], "agent-cortex"
        if runtime == "codex":
            return launcher.build_codex_argv(worktree=str(worktree), read_only=plan,
                                             commit_required=not plan, effort=spec.get("effort"),
                                             **common), "agent-cortex"
        return launcher.build_agy_argv(**common), "agent-cortex"
    if runtime == "claude":
        argv = ["claude", "-p", prompt, "--output-format", "json",
                "--permission-mode", "plan" if plan else "acceptEdits", "--add-dir", str(worktree)]
    elif runtime == "codex":
        argv = ["codex", "exec", prompt, "--json", "--sandbox", "read-only" if plan else "workspace-write",
                "-C", str(worktree)]
    else:
        argv = ["agy", "--print", prompt, "--mode", "plan", "--sandbox"]
    if model:
        argv += ["--model", model]
    return argv, "built-in"


def build_prompt(env: dict, worktree: Path, branch: str, plan: bool) -> str:
    rules = (
        "只讀不改，最後輸出你的結論與證據。" if plan else
        f"只在這個 worktree 內改檔：{worktree}（分支 {branch}）。完成後 `git add` 並 "
        f"`git commit -m \"relay: {env['id']}\"`，不要 push、不要切分支、不要碰 worktree 以外的路徑。"
    )
    return (
        f"你是 agent-relay 派來的工人，任務信 id {env['id']}，寄件者 {env['from']}。\n"
        f"規則：{rules}\n"
        "最後一則訊息用三段回報：做了什麼、驗證證據（指令與輸出）、還沒做的。\n\n"
        f"# {env['subject']}\n\n{env['body']}"
    )


# ---------------------------------------------------------------- run


def _kill_tree(proc: subprocess.Popen) -> None:
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, check=False)
    else:
        proc.kill()


def run(argv: list[str], cwd: Path, log_file: Path, timeout_s: int) -> tuple[str, int | None]:
    """Run with a hard timeout. Returns (status, exit code). Output goes to log_file."""
    exe = shutil.which(argv[0])
    if exe is None:
        return f"not-installed: {argv[0]}", None
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with open(log_file, "w", encoding="utf-8", errors="replace") as log:
        proc = subprocess.Popen([exe, *argv[1:]], cwd=str(cwd), stdout=log, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
        try:
            code = proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            proc.wait(timeout=30)
            return "timeout", None
    return ("ok" if code == 0 else "failed"), code


def final_text(log_file: Path, limit: int = 4000) -> str:
    """The worker's last message: the `result` field of claude/codex JSON output, else the log tail."""
    try:
        text = log_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if isinstance(event.get("result"), str):
            return event["result"][-limit:]
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "agent_message" and isinstance(item.get("text"), str):
            return item["text"][-limit:]
    return text[-limit:]


def changes(worktree: Path, base: str) -> str:
    try:
        commits = _git(worktree, "log", "--oneline", f"{base}..HEAD") or "(no commits)"
        stat = _git(worktree, "diff", "--stat", base) or "(no diff)"
        dirty = _git(worktree, "status", "--porcelain") or "(clean)"
    except DispatchError as exc:
        return f"(could not read worktree: {exc})"
    return f"commits:\n{commits}\n\ndiff --stat {base[:10]}:\n{stat}\n\nuncommitted:\n{dirty}"


def dispatch(env_id: str, agent: str, cfg: dict, sync: bool = True) -> dict:
    """Run one task and send the result envelope. Worker failures become a result, not an exception."""
    store = Store(cfg["store"])
    if sync:
        gitsync.pull(Path(cfg["store"]))
    env = store.read(agent, env_id)
    run_dir = config.home() / "runs" / env_id
    report: dict = {"id": env_id, "status": "refused"}
    try:
        if env["kind"] != "task" or "dispatch" not in env:
            raise DispatchError("not a task with a dispatch section")
        spec = validate_spec(env["dispatch"])
        repo = policy_check(spec, cfg.get("dispatch") or {})
        plan = spec.get("mode", "build") == "plan"
        worktree, branch, base = make_worktree(repo, env_id)
        argv, source = build_argv(spec, build_prompt(env, worktree, branch, plan), f"relay-{env_id}",
                                  run_dir, worktree, cfg)
        started = time.monotonic()
        timeout_s = spec.get("timeout_minutes", DEFAULT_TIMEOUT_MIN) * 60
        status, code = run(argv, worktree, run_dir / "output.log", timeout_s)
        report.update(status=status, exit_code=code, runtime=spec["runtime"], builder=source,
                      worktree=str(worktree), branch=branch, seconds=round(time.monotonic() - started),
                      log=str(run_dir / "output.log"))
        body = (
            f"status: {status}（exit {code}）\nruntime: {spec['runtime']}（指令由 {source} 組）\n"
            f"worktree: {worktree}\nbranch: {branch}\n耗時: {report['seconds']} 秒\n"
            f"log（收件端機器）: {run_dir / 'output.log'}\n\n"
            f"## 工人最後回報\n{final_text(run_dir / 'output.log')}\n\n"
            f"## 變更\n```text\n{changes(worktree, base)}\n```\n"
        )
    except (DispatchError, envelope.EnvelopeError, FileNotFoundError, PermissionError) as exc:
        report["error"] = str(exc)
        body = f"status: refused\n原因: {exc}\n"
    title = f"{report['status']}: {env['subject']}"[:200]
    reply = envelope.build(sender=agent, to=env["from"], subject=title, body=body, kind="result",
                           reply_to=env["id"], thread=env.get("thread"),
                           priority="normal" if report["status"] == "ok" else "high")
    store.deliver(reply)
    store.ack(agent, env_id)
    if sync:
        gitsync.sync(Path(cfg["store"]), f"relay: {agent} dispatch {env_id} -> {report['status']}")
    report["reply_id"] = reply["id"]
    return report


# ---------------------------------------------------------------- doorbell


def ring_dispatch(env: dict, bell: dict) -> str:
    """Doorbell: start `relay dispatch <id>` in its own Windows Terminal tab (or detached).

    The scheduled watch task has a 10-minute limit, so the run must not live
    inside it. It is handed to Windows Terminal (a visible session the owner can
    watch) or, without wt.exe, started as a detached process.
    """
    if env["kind"] != "task" or "dispatch" not in env:
        return "skipped: no dispatch section"
    cmd = [sys.executable, "-m", "agent_relay", "dispatch", env["id"], "--agent", env["to"]]
    workdir = str(Path(__file__).resolve().parent.parent)
    child_env = {**os.environ, "PYTHONPATH": workdir}
    wt = shutil.which("wt.exe") if bell.get("window", True) else None
    if wt:
        subprocess.Popen([wt, "-w", "0", "new-tab", "--title", f"relay {env['id'][-6:]}", "-d", workdir, *cmd],
                         env=child_env, creationflags=CREATE_NO_WINDOW)
        return "ok: Windows Terminal tab"
    flags = (0x00000008 | 0x00000200) if sys.platform == "win32" else 0  # DETACHED_PROCESS | NEW_PROCESS_GROUP
    subprocess.Popen(cmd, cwd=workdir, env=child_env, creationflags=flags,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return "ok: detached process"
