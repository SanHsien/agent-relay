"""Acceptance gates for dispatched work: no stubs, a proven red test, a real green run.

    worker finished in worktree relay/<id>
      ├─ stubs   changed .py functions whose body is only pass / ... / raise NotImplementedError
      ├─ red     base commit + the worker's new or changed test files → the tests must FAIL
      │          (the worker reproduced the problem before fixing it; pstack's TDD rule)
      ├─ green   the worktree → the verification command must PASS
      └─ log     every run is written to runs/<id>/verify-*.log with its exit code and sha256

The receiver decides what runs (dispatch.verify in config.json, or the repo's own
loop-policy.toml [verification] quick_gate from ai-quality-gates). Mail can only
ask for the TDD check; it can never supply a command.

These gates run code the worker wrote (its tests), so only turn them on for repos
whose senders you trust; they are the same commands you would run by hand.
"""

from __future__ import annotations

import ast
import hashlib
import os
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    tomllib = None

TEST_GLOBS = ("tests/**", "test/**", "**/test_*.py", "**/*_test.py", "**/*.test.*", "**/*.spec.*", "features/**")
DEFAULT_TIMEOUT_MIN = 10
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


@dataclass
class GateResult:
    status: str  # ok | stub-rejected | tdd-not-proven | verification-failed | unverified | skipped
    notes: list[str] = field(default_factory=list)
    logs: list[dict] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.status in ("ok", "skipped")


# ---------------------------------------------------------------- configuration


def verify_command(repo: Path, settings: dict) -> list[str] | None:
    """The receiver's command: dispatch.verify.command, or the repo's loop-policy quick_gate."""
    command = settings.get("command")
    if command == "loop-policy":
        if tomllib is None:
            return None
        try:
            policy = tomllib.loads((repo / "loop-policy.toml").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        command = (policy.get("verification") or {}).get("quick_gate")
    if isinstance(command, str) and command.strip():
        return shlex.split(command, posix=sys.platform != "win32")
    if isinstance(command, list) and command and all(isinstance(c, str) for c in command):
        return list(command)
    return None


def is_test_file(path: str, globs=TEST_GLOBS) -> bool:
    from .locks import match_path  # same matcher as check-out claims

    return any(match_path(path, pattern) for pattern in globs)


# ---------------------------------------------------------------- stubs


def _is_stub_body(body: list[ast.stmt]) -> bool:
    stmts = list(body)
    if stmts and isinstance(stmts[0], ast.Expr) and isinstance(getattr(stmts[0], "value", None), ast.Constant) \
            and isinstance(stmts[0].value.value, str):
        stmts = stmts[1:]  # docstring
    if not stmts:
        return True
    if len(stmts) != 1:
        return False
    only = stmts[0]
    if isinstance(only, ast.Pass):
        return True
    if isinstance(only, ast.Expr) and isinstance(only.value, ast.Constant) and only.value.value is Ellipsis:
        return True
    if isinstance(only, ast.Raise) and only.exc is not None:
        target = only.exc.func if isinstance(only.exc, ast.Call) else only.exc
        return isinstance(target, ast.Name) and target.id == "NotImplementedError"
    return False


def _abstract(node: ast.AST) -> bool:
    return any((isinstance(d, ast.Name) and d.id in ("abstractmethod", "overload"))
               or (isinstance(d, ast.Attribute) and d.attr in ("abstractmethod", "overload"))
               for d in getattr(node, "decorator_list", []))


def find_stubs(worktree: Path, changed: list[str]) -> list[str]:
    """`file:line name` for each placeholder function in the changed, non-test Python files."""
    found = []
    for rel in changed:
        if not rel.endswith(".py") or is_test_file(rel):
            continue
        path = worktree / rel
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError, ValueError):
            continue
        in_protocol = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and any(
                    getattr(b, "id", getattr(b, "attr", "")) in ("Protocol", "ABC") for b in node.bases):
                in_protocol.update(id(child) for child in node.body)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _is_stub_body(node.body) \
                    and not _abstract(node) and id(node) not in in_protocol:
                found.append(f"{rel}:{node.lineno} {node.name}()")
    return found


# ---------------------------------------------------------------- running


def run_logged(argv: list[str], cwd: Path, log_file: Path, timeout_s: int) -> dict:
    """Run a gate command; keep its real terminal output, exit code and sha256."""
    log_file.parent.mkdir(parents=True, exist_ok=True)
    exe = shutil.which(argv[0]) or argv[0]
    started = time.monotonic()
    try:
        proc = subprocess.run([exe, *argv[1:]], cwd=str(cwd), capture_output=True, timeout=timeout_s,
                              stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW, check=False)
        output, code = proc.stdout + proc.stderr, proc.returncode
    except subprocess.TimeoutExpired as exc:
        output, code = (exc.stdout or b"") + (exc.stderr or b"") + b"\n[timeout]", None
    except OSError as exc:
        output, code = f"[could not start: {exc}]".encode("utf-8"), None
    header = f"$ {' '.join(argv)}\n(cwd {cwd})\n".encode("utf-8")
    log_file.write_bytes(header + output + f"\n[exit {code}]\n".encode("utf-8"))
    text = output.decode("utf-8", errors="replace")
    return {"command": " ".join(argv), "exit": code, "seconds": round(time.monotonic() - started, 1),
            "log": str(log_file), "sha256": hashlib.sha256(log_file.read_bytes()).hexdigest(),
            "tail": "\n".join(text.strip().splitlines()[-15:])}


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), "-c", "core.hooksPath=" + os.devnull,
                    *args], capture_output=True, check=True, timeout=120)


def red_check(repo: Path, worktree: Path, base: str, tests: list[str], command: list[str],
              run_dir: Path, timeout_s: int) -> dict:
    """Base code + the worker's tests: the run must fail, or the tests never caught the problem."""
    scratch = run_dir / "red"
    if scratch.exists():
        shutil.rmtree(scratch, ignore_errors=True)
    _git(repo, "worktree", "add", "--quiet", "--detach", str(scratch), base)
    try:
        for rel in tests:
            source = worktree / rel
            if source.is_file():
                target = scratch / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
        return run_logged(command, scratch, run_dir / "verify-red.log", timeout_s)
    finally:
        subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(scratch)],
                       capture_output=True, check=False, timeout=120)


def check(repo: Path, worktree: Path, base: str, changed: list[str], run_dir: Path,
          settings: dict, want_tdd: bool) -> GateResult:
    """Run the configured gates. `settings` is dispatch.verify from the receiver's config."""
    if not settings:
        return GateResult("skipped", ["收件端沒有設定驗收閘門（dispatch.verify）"])
    notes: list[str] = []
    if settings.get("no_stubs", True):
        stubs = find_stubs(worktree, changed)
        if stubs:
            return GateResult("stub-rejected", ["空殼函式（只有 pass／...／raise NotImplementedError）：" + "、".join(stubs[:10])])
        notes.append("空殼檢查：通過")
    command = verify_command(repo, settings)
    if command is None:
        return GateResult("unverified", notes + ["找不到驗證指令（dispatch.verify.command 或 loop-policy.toml quick_gate）"])
    timeout_s = int(settings.get("timeout_minutes", DEFAULT_TIMEOUT_MIN)) * 60
    logs = []
    if want_tdd or settings.get("tdd"):
        tests = [f for f in changed if is_test_file(f, tuple(settings.get("test_globs") or TEST_GLOBS))]
        if not tests:
            return GateResult("tdd-not-proven", notes + ["要求 TDD，但工人沒有新增或修改任何測試檔"])
        red = red_check(repo, worktree, base, tests, command, run_dir, timeout_s)
        logs.append({"phase": "red", **red})
        if red["exit"] == 0:
            return GateResult("tdd-not-proven", notes + [f"新測試在修改前的程式上也通過（{', '.join(tests[:5])}），沒有重現問題"], logs)
        notes.append(f"TDD 紅燈：修改前跑新測試失敗（exit {red['exit']}），問題有被重現")
    green = run_logged(command, worktree, run_dir / "verify-green.log", timeout_s)
    logs.append({"phase": "green", **green})
    if green["exit"] != 0:
        return GateResult("verification-failed", notes + [f"驗證指令失敗（exit {green['exit']}）"], logs)
    notes.append(f"綠燈：驗證指令通過（exit 0，{green['seconds']} 秒）")
    return GateResult("ok", notes, logs)
