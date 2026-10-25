"""Acceptance gates for dispatched work: no new stubs, a proven red test, a real green run.

    worker finished in worktree relay/<id>
      ├─ guard   no test file deleted, no test/config file (conftest, pyproject…) touched
      ├─ stubs   functions the worker ADDED or CHANGED whose body is only pass / ... / NotImplementedError
      ├─ base    base commit alone → the verification command must PASS (a green starting point)
      ├─ red     base commit + the worker's new or changed test files → it must FAIL
      │          (the worker reproduced the problem before fixing it; pstack's TDD rule)
      ├─ green   the worktree → the verification command must PASS
      └─ log     every run is written to runs/<id>/verify-*.log with its exit code and sha256

The receiver decides what runs (dispatch.verify in config.json, or the repo's own
loop-policy.toml [verification] quick_gate from ai-quality-gates). Mail can only
ask for the TDD check; it can never supply a command.

These gates run code the worker wrote (its tests), so only turn them on for repos
whose senders you trust. They catch "fixed without reproducing" and "claimed done
without a test run"; they do not stop a worker set on deceiving them (SPEC 11).
"""

from __future__ import annotations

import ast
import fnmatch
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import procs

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    tomllib = None

TEST_GLOBS = ("tests/**", "test/**", "**/test_*.py", "**/*_test.py", "**/*.test.*", "**/*.spec.*", "features/**")
# Files that decide what the test run does; a worker touching them can make green meaningless.
PROTECTED = tuple("**/" + name for name in (  # at any depth: monorepo sub-projects have their own
    "conftest.py", "pyproject.toml", "setup.cfg", "tox.ini", "pytest.ini", "noxfile.py",
    "loop-policy.toml", ".coveragerc", "package.json", "jest.config.*", "vitest.config.*"))
DEFAULT_TIMEOUT_MIN = 10
TAIL_LINES = 15
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
SAFE_GIT = ("-c", "core.hooksPath=" + os.devnull, "-c", "core.fsmonitor=false")


@dataclass
class GateResult:
    # ok | skipped | stub-rejected | tdd-not-proven | verification-failed | unverified | guarded | policy-violation
    status: str
    notes: list[str] = field(default_factory=list)
    logs: list[dict] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.status in ("ok", "skipped")


# ---------------------------------------------------------------- configuration


def detect_command(repo: Path) -> list[str] | None:
    """A verification command guessed from the MAIN checkout's own files (never the worker's branch)."""
    from_policy = verify_command(repo, {"command": "loop-policy"}) if (repo / "loop-policy.toml").is_file() else None
    if from_policy:
        return from_policy
    tests = repo / "tests"
    if tests.is_dir() and any(tests.rglob("test_*.py")):
        return ["python", "-m", "unittest", "discover", "-s", "tests"]
    try:
        scripts = json.loads((repo / "package.json").read_text(encoding="utf-8")).get("scripts") or {}
    except (OSError, ValueError, AttributeError):
        scripts = {}
    test_script = scripts.get("test") if isinstance(scripts, dict) else None
    if isinstance(test_script, str) and test_script.strip() and "no test specified" not in test_script:
        return ["npm", "test", "--silent"]
    if (repo / "go.mod").is_file():
        return ["go", "test", "./..."]
    return None


def verify_command(repo: Path, settings: dict) -> list[str] | None:
    """The command for this repo: verify.by_repo[<folder name>], else verify.command.

    verify.command may be a command (list or string), "loop-policy" (the MAIN repo's
    quick_gate), or "auto" (loop-policy, then tests/, package.json, go.mod).
    """
    by_repo = settings.get("by_repo")
    if isinstance(by_repo, dict) and Path(repo).name in by_repo:
        settings = {**settings, "command": by_repo[Path(repo).name]}
    command = settings.get("command")
    if command == "auto":
        return detect_command(Path(repo))
    if command == "loop-policy":
        if tomllib is None:
            return None
        try:
            policy = tomllib.loads((repo / "loop-policy.toml").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        command = (policy.get("verification") or {}).get("quick_gate")
    if isinstance(command, str) and command.strip():
        parts = shlex.split(command, posix=sys.platform != "win32")
        return [p[1:-1] if len(p) >= 2 and p[0] == p[-1] == '"' else p for p in parts]
    if isinstance(command, list) and command and all(isinstance(c, str) for c in command):
        return list(command)
    return None


def _matches(path: str, globs) -> bool:
    from .locks import match_path  # same matcher as check-out claims

    return any(match_path(path, pattern) for pattern in globs)


def is_test_file(path: str, globs=TEST_GLOBS) -> bool:
    return _matches(path, globs)


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
    if isinstance(only, ast.Return) and (only.value is None or
                                         (isinstance(only.value, ast.Constant) and only.value.value is None)):
        return True
    if isinstance(only, ast.Raise) and only.exc is not None:
        target = only.exc.func if isinstance(only.exc, ast.Call) else only.exc
        return isinstance(target, ast.Name) and target.id == "NotImplementedError"
    return False


def _exempt(node: ast.AST) -> bool:
    return any((isinstance(d, ast.Name) and d.id in ("abstractmethod", "overload"))
               or (isinstance(d, ast.Attribute) and d.attr in ("abstractmethod", "overload"))
               for d in getattr(node, "decorator_list", []))


def _stub_functions(source: str) -> dict[str, str]:
    """name → body dump of each placeholder function (Protocol members and abstract/overload exempt)."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return {}
    protocol_members = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and any(getattr(b, "id", getattr(b, "attr", "")) == "Protocol"
                                                  for b in node.bases):
            protocol_members.update(id(child) for child in node.body)
    found = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _is_stub_body(node.body) \
                and not _exempt(node) and id(node) not in protocol_members:
            found[f"{node.name}:{node.lineno}"] = ast.dump(ast.Module(body=node.body, type_ignores=[]))
    return found


def _base_source(worktree: Path, base: str, rel: str) -> str:
    proc = procs.run(["git", "-C", str(worktree), *SAFE_GIT, "show", f"{base}:{rel}"],
                          capture_output=True, timeout=60, check=False)
    return proc.stdout.decode("utf-8", errors="replace") if proc.returncode == 0 else ""


def find_stubs(worktree: Path, changed: list[str], base: str | None = None, test_globs=TEST_GLOBS) -> list[str]:
    """`file:line name()` for each placeholder the worker added; ones already in base are left alone."""
    found = []
    for rel in changed:
        if not rel.endswith(".py") or is_test_file(rel, test_globs):
            continue
        try:
            now = (worktree / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        before = _stub_functions(_base_source(worktree, base, rel)) if base else {}
        before_bodies = {(key.split(":")[0], body) for key, body in before.items()}
        for key, body in _stub_functions(now).items():
            name, line = key.split(":")
            if (name, body) not in before_bodies:
                found.append(f"{rel}:{line} {name}()")
    return found


# ---------------------------------------------------------------- governance (AGT)


FALLBACK_BUILTIN = "builtin"  # settings["governance_fallback"]: the only value that allows degrading without AGT
DEFAULT_BLOCKED_PATHS = (
    "*.env", "*.env.*", "**/.env", "**/.env.*",
    "**/id_rsa*", "**/*.pem", "**/*.key", "**/*.pfx",
    "**/secrets/**", "**/.aws/**", "**/.ssh/**", "**/credentials/**"
)

_AGT_LOADED = False
_DiffPolicy = None
_DiffFile = None
_CredentialRedactor = None
_scan_code_for_subprocesses = None


def load_governance_toolkit() -> bool:
    """Discover and import agent-governance-toolkit policy and scanning modules."""
    global _AGT_LOADED, _DiffPolicy, _DiffFile, _CredentialRedactor, _scan_code_for_subprocesses
    if _AGT_LOADED:
        return True

    try:
        from agent_os.diff_policy import DiffPolicy, DiffFile
        from agent_os.credential_redactor import CredentialRedactor
        from agent_sandbox.code_scanner import scan_code_for_subprocesses
        _DiffPolicy, _DiffFile = DiffPolicy, DiffFile
        _CredentialRedactor = CredentialRedactor
        _scan_code_for_subprocesses = scan_code_for_subprocesses
        _AGT_LOADED = True
        return True
    except ImportError:
        pass

    candidates = []
    env_path = os.environ.get("AGENT_GOVERNANCE_PATH")
    if env_path:
        candidates.append(Path(env_path))
    candidates.append(Path("C:/GitHub/agent-governance-toolkit"))
    repo_root = Path(__file__).resolve().parent.parent
    candidates.append(repo_root.parent / "agent-governance-toolkit")

    for root in candidates:
        if root.is_dir():
            py_root = root / "agent-governance-python"
            if py_root.is_dir():
                os_src = py_root / "agent-os" / "src"
                sandbox_src = py_root / "agent-sandbox" / "src"
                if os_src.is_dir() and str(os_src) not in sys.path:
                    sys.path.insert(0, str(os_src))
                if sandbox_src.is_dir() and str(sandbox_src) not in sys.path:
                    sys.path.insert(0, str(sandbox_src))

    try:
        from agent_os.diff_policy import DiffPolicy, DiffFile
        from agent_os.credential_redactor import CredentialRedactor
        from agent_sandbox.code_scanner import scan_code_for_subprocesses
        _DiffPolicy, _DiffFile = DiffPolicy, DiffFile
        _CredentialRedactor = CredentialRedactor
        _scan_code_for_subprocesses = scan_code_for_subprocesses
        _AGT_LOADED = True
        return True
    except ImportError:
        return False


def evaluate_governance_policy(worktree: Path, changed: list[str], settings: dict) -> GateResult:
    """Run runtime governance policies from agent-governance-toolkit.

    Validates:
      1. Diff scope & path boundaries (DiffPolicy): file count, line count, blocked paths
      2. Secret & credential leak prevention (CredentialRedactor): detect API keys / tokens
      3. Dangerous subprocess & system calls (CodeScanner): static AST inspection

    Fail closed: when the checks were requested but AGT cannot be loaded, the result is
    "unverified" (the checks did not run, so nothing may be called a pass). Only an explicit
    governance_fallback = "builtin" allows the simplified built-in checks; the notes then say
    "降級：內建簡化檢查", and the subprocess scan (no built-in substitute) is reported "未執行".
    """
    agt = load_governance_toolkit()
    notes = []
    want_credentials = bool(settings.get("no_credentials", True))
    want_scan = bool(settings.get("scan_subprocesses", False) or settings.get("strict", False))
    degraded = False

    if not agt:
        if settings.get("governance_fallback") != FALLBACK_BUILTIN:
            missing = ["路徑邊界"] + (["機密外洩"] if want_credentials else []) + (["危險指令審查"] if want_scan else [])
            return GateResult("unverified", [f"AGT 未載入，無法執行{name}檢查" for name in missing]
                              + ["無真實檢查證據不放行；請安裝 agent-governance-toolkit（環境變數 AGENT_GOVERNANCE_PATH 或"
                                 "預設路徑 C:/GitHub/agent-governance-toolkit），或明確設定 governance_fallback=\"builtin\" 降級"])
        degraded = True
        notes.append("降級：內建簡化檢查（AGT 未載入，governance_fallback=builtin）")

    # 1. Path Boundary / Diff Policy
    blocked = list(settings.get("blocked_paths") or DEFAULT_BLOCKED_PATHS)
    max_files = settings.get("max_files")
    max_lines = settings.get("max_lines")

    if not degraded:
        diff_files = []
        for rel in changed:
            lines = 0
            target = worktree / rel
            if target.is_file():
                try:
                    lines = len(target.read_text(encoding="utf-8", errors="replace").splitlines())
                except OSError:
                    lines = 0
            diff_files.append(_DiffFile(path=rel.replace("\\", "/"), additions=lines, deletions=0))
        policy = _DiffPolicy(max_files=max_files, max_lines=max_lines, blocked_paths=blocked)
        result = policy.evaluate(diff_files)
        if not result.allowed:
            return GateResult("policy-violation", [f"治理政策違規（路徑邊界）：{v}" for v in result.violations])
    else:
        for rel in changed:
            norm_rel = rel.replace("\\", "/")
            if any(fnmatch.fnmatch(norm_rel, pat) or fnmatch.fnmatch(Path(norm_rel).name, pat) for pat in blocked):
                return GateResult("policy-violation", [f"治理政策違規（路徑邊界）：檔案 {rel} 命中禁止路徑規則"])
        if max_files and len(changed) > max_files:
            return GateResult("policy-violation", [f"治理政策違規（路徑邊界）：變更檔案數 {len(changed)} 超過上限 {max_files}"])

    notes.append("路徑邊界政策：通過" + ("（降級：內建簡化檢查）" if degraded else ""))

    # 2. Secret / Credential Leak Policy
    if want_credentials:
        for rel in changed:
            target = worktree / rel
            if not target.is_file():
                continue
            try:
                content = target.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            if not degraded:
                types = _CredentialRedactor.detect_credential_types(content)
                if types:
                    return GateResult("policy-violation", [
                        f"治理政策違規（機密外洩）：檔案 {rel} 檢測到機密憑證（{', '.join(types)}），已拒絕放行"
                    ])
            else:
                if any(k in content for k in ("sk-", "ghp_", "AKIA")):
                    return GateResult("policy-violation", [
                        f"治理政策違規（機密外洩）：檔案 {rel} 檢測到疑似敏感金鑰特徵，已拒絕放行"
                    ])

        notes.append("機密外洩防護：通過" + ("（降級：內建簡化檢查，只搜 sk-／ghp_／AKIA）" if degraded else ""))

    # 3. Dangerous Subprocess / Code Scanner Policy
    if want_scan and degraded:
        notes.append("危險指令審查：未執行（AGT 未載入，內建檢查沒有替代掃描）")
    elif want_scan:
        globs = tuple(settings.get("test_globs") or TEST_GLOBS)
        for rel in changed:
            if not rel.endswith(".py") or is_test_file(rel, globs):
                continue
            target = worktree / rel
            if not target.is_file():
                continue
            try:
                content = target.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            if _scan_code_for_subprocesses is not None:
                violations = _scan_code_for_subprocesses(content)
                if violations:
                    first = violations[0]
                    return GateResult("policy-violation", [
                        f"治理政策違規（危險指令）：檔案 {rel}:{first.line} 包含禁止的行程調用（{first.pattern}）"
                    ])

        notes.append("危險指令審查：通過")

    return GateResult("ok", notes)


# ---------------------------------------------------------------- running


def _kill_tree(proc: subprocess.Popen) -> None:
    if sys.platform == "win32":
        procs.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, check=False)
    else:
        proc.kill()


def run_logged(argv: list[str], cwd: Path, log_file: Path, timeout_s: int) -> dict:
    """Run a gate command straight into a log file; on timeout kill the whole process tree.

    Output goes to a file, not a pipe, so a grandchild that outlives the timeout can
    neither keep us waiting nor fill memory.
    """
    log_file.parent.mkdir(parents=True, exist_ok=True)
    exe = shutil.which(argv[0]) or argv[0]
    started = time.monotonic()
    code: int | None = None
    with open(log_file, "wb") as log:
        log.write(f"$ {' '.join(argv)}\n(cwd {cwd})\n".encode("utf-8"))
        log.flush()
        try:
            # No bytecode cache: a fix made within the same second as an earlier run, with the
            # same file size, would otherwise be tested against the stale .pyc of the old code.
            proc = subprocess.Popen([exe, *argv[1:]], cwd=str(cwd), stdout=log, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW,
                                    env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
            try:
                code = proc.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                _kill_tree(proc)
                try:
                    proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    pass
                log.write(b"\n[timeout: process tree killed]")
        except OSError as exc:
            log.write(f"[could not start: {exc}]".encode("utf-8"))
        log.write(f"\n[exit {code}]\n".encode("utf-8"))
    data = log_file.read_bytes()
    text = data.decode("utf-8", errors="replace")
    if _CredentialRedactor is not None:
        text = _CredentialRedactor.redact(text)
    return {"command": " ".join(argv), "exit": code, "seconds": round(time.monotonic() - started, 1),
            "log": str(log_file), "sha256": hashlib.sha256(data).hexdigest(),
            "tail": "\n".join(text.strip().splitlines()[-TAIL_LINES:])}


def _scratch_worktree(repo: Path, base: str, path: Path) -> None:
    procs.run(["git", "-C", str(repo), *SAFE_GIT, "worktree", "prune"], capture_output=True, check=False, timeout=60)
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    procs.run(["git", "-C", str(repo), *SAFE_GIT, "worktree", "add", "--quiet", "--force", "--detach", str(path), base],
                   capture_output=True, check=True, timeout=120)


def _drop_worktree(repo: Path, path: Path, notes: list[str]) -> None:
    proc = procs.run(["git", "-C", str(repo), *SAFE_GIT, "worktree", "remove", "--force", str(path)],
                          capture_output=True, check=False, timeout=120)
    if proc.returncode != 0:
        notes.append(f"暫時 worktree 沒能移除：{path}（下次會先 prune）")


def base_and_red(repo: Path, worktree: Path, base: str, tests: list[str], command: list[str],
                 run_dir: Path, timeout_s: int, notes: list[str]) -> tuple[dict, dict | None]:
    """Run the base alone (must pass), then base + the worker's tests (must fail)."""
    scratch = run_dir / "red"
    _scratch_worktree(repo, base, scratch)
    try:
        baseline = run_logged(command, scratch, run_dir / "verify-base.log", timeout_s)
        if baseline["exit"] != 0:
            return baseline, None
        for rel in tests:
            source = worktree / rel
            if source.is_file():
                target = scratch / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
        return baseline, run_logged(command, scratch, run_dir / "verify-red.log", timeout_s)
    finally:
        _drop_worktree(repo, scratch, notes)


def check(repo: Path, worktree: Path, base: str, changed: list[str], run_dir: Path,
          settings: dict, want_tdd: bool) -> GateResult:
    """Run the configured gates. `settings` is dispatch.verify from the receiver's config."""
    if not settings:
        return GateResult("skipped", ["收件端沒有設定驗收閘門（dispatch.verify）"])
    notes: list[str] = []
    globs = tuple(settings.get("test_globs") or TEST_GLOBS)
    deleted = [f for f in changed if is_test_file(f, globs) and not (worktree / f).exists()]
    if deleted and not settings.get("allow_test_deletion"):
        return GateResult("guarded", [f"工人刪除了測試檔：{', '.join(deleted[:10])}（要允許就設 verify.allow_test_deletion）"])
    touched = [f for f in changed if _matches(f, tuple(settings.get("protected") or PROTECTED))]
    if touched and not settings.get("allow_config_changes"):
        return GateResult("guarded", [f"工人改了測試設定檔：{', '.join(touched[:10])}"
                                      "（可能讓綠燈失去意義；要允許就設 verify.allow_config_changes）"])
    if settings.get("no_stubs", True):
        stubs = find_stubs(worktree, changed, base, globs)
        if stubs:
            return GateResult("stub-rejected", ["新加的空殼函式（只有 pass／...／return None／raise NotImplementedError）："
                                                + "、".join(stubs[:10])])
        notes.append("空殼檢查：通過")
    if (settings.get("governance") or "no_credentials" in settings or "blocked_paths" in settings
            or "max_files" in settings or settings.get("scan_subprocesses") or settings.get("strict")):
        gov_settings = settings.get("governance") if isinstance(settings.get("governance"), dict) else settings
        if "governance_fallback" not in gov_settings and "governance_fallback" in settings:
            gov_settings = {**gov_settings, "governance_fallback": settings["governance_fallback"]}
        gov_res = evaluate_governance_policy(worktree, changed, gov_settings)
        if not gov_res.passed:
            return gov_res
        notes.extend(gov_res.notes)
    command = verify_command(repo, settings)
    if command is None:
        return GateResult("unverified", notes + ["找不到驗證指令（dispatch.verify.command 或 loop-policy.toml quick_gate）"])
    timeout_s = int(settings.get("timeout_minutes", DEFAULT_TIMEOUT_MIN)) * 60
    logs = []
    if want_tdd or settings.get("tdd"):
        tests = [f for f in changed if is_test_file(f, globs) and (worktree / f).exists()]
        if not tests:
            return GateResult("tdd-not-proven", notes + ["要求 TDD，但工人沒有新增或修改任何測試檔"])
        baseline, red = base_and_red(repo, worktree, base, tests, command, run_dir, timeout_s, notes)
        logs.append({"phase": "base", **baseline})
        if red is None:
            return GateResult("tdd-not-proven", notes + [f"修改前的程式本身就沒通過驗證（exit {baseline['exit']}），"
                                                         "無法證明新測試的失敗是它造成的"], logs)
        logs.append({"phase": "red", **red})
        if red["exit"] == 0:
            return GateResult("tdd-not-proven", notes + [f"新測試在修改前的程式上也通過（{', '.join(tests[:5])}），沒有重現問題"], logs)
        notes.append(f"TDD：修改前驗證通過（base 綠），加上新測試後失敗（exit {red['exit']}），問題有被重現")
    green = run_logged(command, worktree, run_dir / "verify-green.log", timeout_s)
    logs.append({"phase": "green", **green})
    if green["exit"] != 0:
        return GateResult("verification-failed", notes + [f"驗證指令失敗（exit {green['exit']}）"], logs)
    notes.append(f"綠燈：驗證指令通過（exit 0，{green['seconds']} 秒）")
    return GateResult("ok", notes, logs)
