"""Run a task envelope in a fresh agent session, isolated in a git worktree.

    commander ──task + "dispatch"──▶ mailbox ──watch──▶ dispatch doorbell
                                                         │ opens a Windows Terminal tab
                                                         ▼
                              relay dispatch <id>:  worktree relay/<id>
                                                    → headless claude / codex / agy
                                                    → result envelope back (reply_to)

The receiver decides what may run: `dispatch.runtimes`, `dispatch.repo_roots` and
(optionally) `dispatch.senders` in config.json. Mail only names a repo and a
runtime inside that policy. The worker commits on its own branch in its own
worktree and never pushes; merging is left to whoever reviews the result.

Command lines come from agent-cortex's launcher (SanHsien/agent-cortex,
paulsha_cortex/coordinator/launcher.py) when it can be imported, so permission
modes stay the ones cortex already hardened; the fallback builders below keep
the same conservative defaults (claude acceptEdits, codex workspace-write sandbox,
agy plan + sandbox).

Every run ends in exactly one result envelope and an ack, whatever goes wrong.
A run that dies without replying (tab closed, reboot) is found by `watch` through
its lease in runs/<id>/state.json and answered with `status: interrupted`.
"""

from __future__ import annotations

import calendar
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import config, context, doorbell, envelope, gitsync
from .context import redact
from .store import Store

RUNTIMES = ("claude", "codex", "agy")
MODES = ("build", "plan")
EFFORTS = ("low", "medium", "high", "xhigh")
MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,63}")
DEFAULT_TIMEOUT_MIN = 30
MAX_TIMEOUT_MIN = 120
LEASE_GRACE_S = 600
WORKTREE_DIR = ".relay-worktrees"
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
DETACHED_PROCESS, NEW_PROCESS_GROUP, BREAKAWAY_FROM_JOB = 0x00000008, 0x00000200, 0x01000000

# Workers only edit files; relay commits for them afterwards. A claude worker gets no
# Bash at all by default: claude has no shell sandbox on native Windows, so any
# "run the tests" rule would let mail text run arbitrary code (write a conftest.py,
# then pytest it). Receivers may opt in through dispatch.claude_allowed_tools.
DEFAULT_CLAUDE_TOOLS: tuple[str, ...] = ()


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
    if any(ch in spec["repo"] for ch in (chr(10), chr(13))):
        raise envelope.EnvelopeError("dispatch.repo must be one line")
    if spec.get("mode", "build") not in MODES:
        raise envelope.EnvelopeError(f"dispatch.mode must be one of {MODES}")
    timeout = spec.get("timeout_minutes", DEFAULT_TIMEOUT_MIN)
    if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= MAX_TIMEOUT_MIN:
        raise envelope.EnvelopeError(f"dispatch.timeout_minutes must be 1..{MAX_TIMEOUT_MIN}")
    if "model" in spec and not MODEL_RE.fullmatch(str(spec["model"])):
        raise envelope.EnvelopeError("dispatch.model must look like a model id")
    if "effort" in spec and spec["effort"] not in EFFORTS:
        raise envelope.EnvelopeError(f"dispatch.effort must be one of {EFFORTS}")
    return spec


def policy_check(env: dict, spec: dict, policy: dict) -> Path:
    """Return the resolved repo, or raise when the receiver has not allowed this."""
    senders = policy.get("senders")
    if senders is not None and env["from"] not in senders:
        raise DispatchError(f"sender {env['from']!r} not in dispatch.senders {senders}")
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


def _registered_branch(repo: Path, path: Path) -> str | None:
    """The branch checked out at `path` if git knows it as a worktree of `repo`."""
    current: Path | None = None
    for line in _git(repo, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            current = Path(line[len("worktree "):].strip()).resolve()
        elif line.startswith("branch ") and current == path.resolve():
            return line[len("branch "):].strip().removeprefix("refs/heads/")
    return None


def make_worktree(repo: Path, env_id: str) -> tuple[Path, str, str]:
    """Create <repo>/.relay-worktrees/<id> on branch relay/<id>. Returns (path, branch, base)."""
    branch = f"relay/{env_id}"
    path = repo / WORKTREE_DIR / env_id
    _exclude(repo)
    if path.exists():
        # Only reuse what is provably our own worktree; a stray folder would put the
        # worker inside the main checkout.
        if _registered_branch(repo, path) != branch:
            raise DispatchError(f"{path} exists but is not the {branch} worktree")
        base = _git(repo, "merge-base", "HEAD", branch).strip()
    else:
        base = _git(repo, "rev-parse", "HEAD").strip()
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
               cfg: dict) -> tuple[list[str], str, str | None]:
    """Return (argv, source, stdin_text).

    claude and codex take the prompt on stdin: Windows command lines are capped
    (32K) and a .cmd shim would cut the prompt at its first newline. Workers never
    get write access to git's own directories; relay commits after they finish.
    """
    runtime, plan = spec["runtime"], spec.get("mode", "build") == "plan"
    model = spec.get("model")
    tools = list((cfg.get("dispatch") or {}).get("claude_allowed_tools") or DEFAULT_CLAUDE_TOOLS)
    launcher = _cortex_launcher(cfg)
    if launcher is not None:
        common = {"prompt": prompt, "slice_id": slice_id, "log_dir": str(log_dir), "model": model}
        if runtime == "claude":
            argv = launcher.build_claude_argv(worktree=str(worktree), read_only=plan, **common)
            # cortex enables Remote Control for its own monitor; a relay run has no use for it.
            argv = [a for a in argv if a != "--remote-control"]
            if tools and not plan:
                argv += ["--allowedTools", *tools]
            return _prompt_to_stdin(argv, prompt), "agent-cortex", prompt
        if runtime == "codex":
            argv = launcher.build_codex_argv(worktree=str(worktree), read_only=plan,
                                             effort=spec.get("effort"), **common)
            return _prompt_to_stdin(argv, prompt), "agent-cortex", prompt
        return launcher.build_agy_argv(**common), "agent-cortex", None
    if runtime == "claude":
        argv = ["claude", "-p", "--output-format", "json",
                "--permission-mode", "plan" if plan else "acceptEdits", "--add-dir", str(worktree)]
        if tools and not plan:
            argv += ["--allowedTools", *tools]
        stdin = prompt
    elif runtime == "codex":
        argv = ["codex", "exec", "--json", "--sandbox", "read-only" if plan else "workspace-write",
                "-C", str(worktree)]
        if spec.get("effort"):
            argv += ["-c", f"model_reasoning_effort={json.dumps(spec['effort'])}"]
        stdin = prompt
    else:
        argv, stdin = ["agy", "--print", prompt, "--mode", "plan", "--sandbox"], None
    if model:
        argv += ["--model", model]
    return argv, "built-in", stdin


def _git_marker(worktree: Path) -> bytes | None:
    try:
        return (worktree / ".git").read_bytes()
    except OSError:
        return None


def commit_work(worktree: Path, env_id: str, marker: bytes | None) -> str:
    """Commit what the worker left, as relay. Hooks are off and the .git pointer must be untouched.

    The worker could edit files in the worktree, including the `.git` file that
    tells git where the repository lives; a forged one could point at hooks it
    wrote. So relay refuses to run git there if the pointer changed, and commits
    with hooks disabled.
    """
    if _git_marker(worktree) != marker:
        return "skipped: the worktree's .git pointer was changed by the worker; not committing"
    if not _git(worktree, "status", "--porcelain"):
        return "nothing to commit"
    safe = ("-c", "core.hooksPath=" + os.devnull, "-c", "core.fsmonitor=false")
    _git(worktree, *safe, "add", "-A")
    _git(worktree, *safe, "commit", "--quiet", "--no-verify", "-m", f"relay: {env_id}")
    return "committed: " + _git(worktree, "rev-parse", "--short", "HEAD").strip()


def _prompt_to_stdin(argv: list[str], prompt: str) -> list[str]:
    out = list(argv)
    out.remove(prompt)  # the positional prompt; the CLI then reads it from stdin
    return out


def build_prompt(env: dict, worktree: Path, branch: str, plan: bool, handover: str = "") -> str:
    rules = (
        "只讀不改，最後輸出你的結論與證據。" if plan else
        f"只在這個 worktree 內改檔：{worktree}（分支 {branch}）。不要 commit、不要 push、不要切分支、"
        "不要碰 worktree 以外的路徑；你結束後 relay 會替你 commit。能執行指令就只在 worktree 內跑驗證；"
        "不能執行就在回報裡寫清楚寄件者該跑哪些驗證指令。"
    )
    return (
        f"你是 agent-relay 派來的工人，任務信 id {env['id']}，寄件者 {env['from']}。\n"
        f"規則：{rules}\n"
        "最後一則訊息用三段回報：做了什麼、驗證證據（指令與輸出）、還沒做的。\n\n"
        f"# {env['subject']}\n\n{env['body']}"
        + (f"\n\n---\n\n{handover}" if handover else "")
    )


def resolve_executable(name: str, cfg: dict) -> str:
    """A real .exe for the runtime. Batch shims are refused: cmd.exe re-parses arguments."""
    configured = ((cfg.get("dispatch") or {}).get("executables") or {}).get(name)
    exe = os.path.expandvars(os.path.expanduser(configured)) if configured else None
    if not exe and sys.platform == "win32":
        exe = shutil.which(name + ".exe")
    exe = exe or shutil.which(name)
    if not exe or not Path(exe).is_file():
        raise DispatchError(f"runtime {name!r} is not installed (set dispatch.executables.{name})")
    if Path(exe).suffix.lower() in (".bat", ".cmd"):
        raise DispatchError(f"{exe} is a batch shim; set dispatch.executables.{name} to the real .exe")
    return exe


# ---------------------------------------------------------------- run


def _kill_tree(proc: subprocess.Popen) -> None:
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, check=False)
    else:
        proc.kill()


def run(argv: list[str], cwd: Path, log_file: Path, timeout_s: int, stdin_text: str | None = None) -> tuple[str, int | None]:
    """Run argv (argv[0] already resolved) with a hard timeout. Returns (status, exit code)."""
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with open(log_file, "w", encoding="utf-8", errors="replace") as log:
        proc = subprocess.Popen(argv, cwd=str(cwd), stdout=log, stderr=subprocess.STDOUT,
                                stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                                creationflags=CREATE_NO_WINDOW)
        if stdin_text is not None:
            try:
                proc.stdin.write(stdin_text.encode("utf-8"))
                proc.stdin.close()
            except OSError:
                pass  # the worker exited early; its exit code says why
        try:
            code = proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                pass
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


# ---------------------------------------------------------------- lease


def _state_path(env_id: str) -> Path:
    return config.home() / "runs" / env_id / "state.json"


def write_state(env_id: str, **fields) -> None:
    path = _state_path(env_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(fields), encoding="utf-8")
    os.replace(tmp, path)


def read_state(env_id: str) -> dict | None:
    try:
        return json.loads(_state_path(env_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _reply(store: Store, agent: str, env: dict, status: str, body: str) -> dict:
    reply = envelope.build(sender=agent, to=env["from"], subject=f"{status}: {env['subject']}"[:200],
                           body=redact(body), kind="result", reply_to=env["id"], thread=env.get("thread"),
                           priority="normal" if status == "ok" else "high")
    store.deliver(reply)
    store.ack(agent, env["id"])
    write_state(env["id"], phase="replied", status=status, reply_id=reply["id"], finished=time.time())
    return reply


def _sync(cfg: dict, message: str) -> None:
    for _ in range(2):  # a scheduled watch may hold index.lock for a moment
        try:
            gitsync.sync(Path(cfg["store"]), message)
            return
        except gitsync.SyncError as exc:
            doorbell._log(f"dispatch sync failed: {exc}")
            time.sleep(5)


# ---------------------------------------------------------------- main entry


def dispatch(env_id: str, agent: str, cfg: dict, sync: bool = True, force: bool = False) -> dict:
    """Run one task and send the result envelope. Every outcome ends in one reply and an ack."""
    store = Store(cfg["store"])
    if sync:
        try:
            gitsync.pull(Path(cfg["store"]))
        except gitsync.SyncError as exc:  # work from the local copy; the reply syncs later
            doorbell._log(f"dispatch pull failed: {exc}")
    hit = store.find(agent, env_id)
    if hit is None:
        raise DispatchError(f"no message {env_id} in mailbox {agent}")
    if hit[0] == "done" and not force:
        raise DispatchError(f"{env_id} is already done; pass --force to run it again")
    env = store.read(agent, env_id)
    run_dir = config.home() / "runs" / env_id
    report: dict = {"id": env_id, "status": "refused"}
    try:
        if env["kind"] != "task" or "dispatch" not in env:
            raise DispatchError("not a task with a dispatch section")
        spec = validate_spec(env["dispatch"])
        timeout_s = spec.get("timeout_minutes", DEFAULT_TIMEOUT_MIN) * 60
        write_state(env_id, phase="running", pid=os.getpid(), started=time.time(),
                    deadline=time.time() + timeout_s + LEASE_GRACE_S)
        repo = policy_check(env, spec, cfg.get("dispatch") or {})
        plan = spec.get("mode", "build") == "plan"
        worktree, branch, base = make_worktree(repo, env_id)
        marker = _git_marker(worktree)
        # The worker starts with the repo's hand-over bundle instead of a pasted context.
        handover = context.bundle(repo, cfg) if (cfg.get("dispatch") or {}).get("context", True) else ""
        argv, source, stdin_text = build_argv(spec, build_prompt(env, worktree, branch, plan, handover),
                                              f"relay-{env_id}", run_dir, worktree, cfg)
        argv = [resolve_executable(spec["runtime"], cfg), *argv[1:]]
        started = time.monotonic()
        status, code = run(argv, worktree, run_dir / "output.log", timeout_s, stdin_text)
        commit = "plan mode: nothing to commit" if plan else commit_work(worktree, env_id, marker)
        report.update(status=status, exit_code=code, runtime=spec["runtime"], builder=source, commit=commit,
                      worktree=str(worktree), branch=branch, seconds=round(time.monotonic() - started),
                      log=str(run_dir / "output.log"))
        body = (
            f"status: {status}（exit {code}）\nruntime: {spec['runtime']}（指令由 {source} 組）\n"
            f"worktree: {worktree}\nbranch: {branch}\nrelay commit: {commit}\n耗時: {report['seconds']} 秒\n"
            f"log（收件端機器）: {run_dir / 'output.log'}\n\n"
            f"## 工人最後回報\n{final_text(run_dir / 'output.log')}\n\n"
            f"## 變更\n```text\n{changes(worktree, base)}\n```\n"
        )
    except (DispatchError, envelope.EnvelopeError, FileNotFoundError, PermissionError) as exc:
        report["error"] = str(exc)
        body = f"status: refused\n原因: {exc}\n"
    except Exception as exc:  # anything else still owes the sender an answer
        report.update(status="error", error=f"{type(exc).__name__}: {exc}")
        body = f"status: error\n原因: {type(exc).__name__}: {exc}\n"
    report["reply_id"] = _reply(store, agent, env, report["status"], body)["id"]
    if sync:
        _sync(cfg, f"relay: {agent} dispatch {env_id} -> {report['status']}")
    return report


def _never_started(env: dict, cfg: dict, rung: set[str], now: float) -> bool:
    """Doorbell rang, but no run ever wrote a lease (the tab failed to open, say)."""
    if env["id"] not in rung or read_state(env["id"]) is not None:
        return False
    if not any(bell.get("type") == "dispatch" for bell in cfg.get("doorbells", [])):
        return False  # without a dispatch doorbell someone runs it by hand; leave it
    created = calendar.timegm(time.strptime(env["created_at"][:19], "%Y-%m-%dT%H:%M:%S"))
    return now - created > LEASE_GRACE_S * 3


def reap_interrupted(agent: str, cfg: dict, now: float | None = None, rung: list[str] | None = None) -> list[str]:
    """Answer dispatch tasks that will never reply on their own.

    - in cur/ with a running lease past its deadline: the run died mid-way;
    - in new/, already rung, no lease after a while: the run never started.
    One bad message is logged and skipped; it must not stop the watch.
    """
    store = Store(cfg["store"])
    now = now or time.time()
    rung_ids = set(rung if rung is not None else doorbell.load_rung(agent))
    reaped = []
    for state, env in store.list(agent, ("new", "cur")):
        if env["kind"] != "task" or "dispatch" not in env:
            continue
        try:
            if state == "cur":
                lease = read_state(env["id"])
                if not lease or lease.get("phase") != "running" or now < float(lease.get("deadline")):
                    continue
                started = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(lease.get("started", 0))))
                body = (f"status: interrupted\n派工在 {started} 開始，超過期限仍未回報（分頁被關、當機或重開機）。"
                        "worktree 若已建立會保留在原處。\n")
            elif _never_started(env, cfg, rung_ids, now):
                store.read(agent, env["id"])
                body = ("status: interrupted\n門鈴已響，但派工一直沒有啟動（例如新分頁沒開成）。"
                        "可以重寄，或在收件端手動 `relay dispatch <id>`。\n")
            else:
                continue
            _reply(store, agent, env, "interrupted", body)
            reaped.append(env["id"])
        except Exception as exc:  # noqa: BLE001 - one bad message must not stop the watch
            doorbell._log(f"reap {env['id']} skipped: {type(exc).__name__}: {exc}")
    return reaped


# ---------------------------------------------------------------- doorbell


def _console_python() -> str:
    """The scheduled task runs pythonw.exe; a visible tab needs the console python.exe."""
    exe = Path(sys.executable)
    console = exe.with_name("python.exe")
    return str(console if exe.name.lower() == "pythonw.exe" and console.is_file() else exe)


def ring_dispatch(env: dict, bell: dict) -> str:
    """Doorbell: start `relay dispatch <id>` in its own Windows Terminal tab (or detached).

    The scheduled watch task has a 10-minute limit, so the run must not live
    inside it. A Windows Terminal tab may be created by an already running wt
    process that does not inherit our environment, so the relay home and store
    are passed as arguments, not environment variables.
    """
    if env["kind"] != "task" or "dispatch" not in env:
        return "skipped: no dispatch section"
    cmd = [_console_python(), "-m", "agent_relay", "dispatch", env["id"], "--agent", env["to"],
           "--home", str(config.home())]
    if os.environ.get("AGENT_RELAY_STORE"):
        cmd += ["--store", os.environ["AGENT_RELAY_STORE"]]
    workdir = str(Path(__file__).resolve().parent.parent)
    wt = shutil.which("wt.exe") if bell.get("window", True) else None
    if wt:
        subprocess.Popen([wt, "-w", "0", "new-tab", "--title", f"relay {env['id'][-6:]}", "-d", workdir, *cmd],
                         creationflags=CREATE_NO_WINDOW)
        return "ok: Windows Terminal tab"
    flags = DETACHED_PROCESS | NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    kwargs = {"cwd": workdir, "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    try:  # leave the scheduler's job object so the run survives the watch task
        subprocess.Popen(cmd, creationflags=flags | (BREAKAWAY_FROM_JOB if flags else 0), **kwargs)
    except OSError:
        subprocess.Popen(cmd, creationflags=flags, **kwargs)
    return "ok: detached process"
