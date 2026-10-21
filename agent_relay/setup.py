"""`relay setup-local`: rebuild this machine's commander + worker setup from one command (SPEC 19).

Idempotent: every step only adds what is missing. Existing config values are never
overwritten, existing keys are kept, trust lines are not duplicated, and the two
scheduled tasks are re-registered with the same names. The private keys are not
backed up anywhere: nothing is encrypted with them, so a lost key is replaced by
running this again (new key, trusted again on both sides).
"""

from __future__ import annotations

import json
from pathlib import Path

from . import poller, signing

DEFAULT_WORKER_HOME = Path.home() / ".agent-relay-worker"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _fill(target: dict, defaults: dict) -> list[str]:
    """Add missing keys (recursively); return the dotted names that were added."""
    added = []
    for key, value in defaults.items():
        if key not in target:
            target[key] = value
            added.append(key)
        elif isinstance(value, dict) and isinstance(target[key], dict):
            added += [f"{key}.{sub}" for sub in _fill(target[key], value)]
    return added


def worker_defaults(store: str, agent: str, repo_roots: list[str], runtimes: list[str], auto: bool) -> dict:
    dispatch = {
        "runtimes": runtimes,
        "repo_roots": repo_roots,
        "senders": ["commander"],
        "cortex_path": str(Path(__file__).resolve().parent.parent.parent / "agent-cortex").replace("\\", "/"),
        "verify": {"command": "auto", "timeout_minutes": 15},
        "require_verification": True,
        "push_branch": True,
        "max_concurrent": 2,
        "daily_limit": 20,
    }
    if "claude-3p" in runtimes:
        dispatch["claude_3p"] = {"model": "gpt-6.1-sol"}  # a non-Anthropic gateway subscription
    if auto:
        dispatch["default"] = {"runtime": "claude", "timeout_minutes": 30}
    return {"store": store, "agent": agent, "doorbells": [{"type": "toast"}, {"type": "dispatch"}],
            "dispatch": dispatch}


def setup_local(commander_home: Path, worker_home: Path = DEFAULT_WORKER_HOME, worker_agent: str = "claude-code",
                repo_roots: list[str] | None = None, runtimes: list[str] | None = None, every: int = 5,
                auto: bool = False, install_tasks: bool = True) -> list[str]:
    """Run every step; returns a log of what was done or already in place."""
    steps: list[str] = []
    commander_home, worker_home = Path(commander_home), Path(worker_home)
    cfg_path = commander_home / "config.json"
    cfg = _load(cfg_path)
    if not cfg.get("store") or not Path(cfg["store"]).is_dir():
        raise RuntimeError(f"{cfg_path} has no store yet: run `relay init --agent commander --clone <store repo>` first")
    commander = cfg.get("agent") or "commander"
    repo_roots = repo_roots or ["C:\\GitHub"]
    runtimes = runtimes or ["claude", "codex", "hermes"]

    def key_for(home: Path, agent: str) -> tuple[Path, str]:
        key = signing.default_key(home, agent)
        if key.is_file():
            steps.append(f"key kept: {key}")
        else:
            signing.keygen(key, agent)
            steps.append(f"key created: {key}")
        return key, Path(str(key) + ".pub").read_text(encoding="utf-8").strip()

    _, commander_pub = key_for(commander_home, commander)

    wcfg_path = worker_home / "config.json"
    wcfg = _load(wcfg_path)
    added = _fill(wcfg, worker_defaults(cfg["store"], worker_agent, repo_roots, runtimes, auto))
    _save(wcfg_path, wcfg)
    steps.append(f"worker config {'created' if not added or 'store' in added else 'completed'}: {wcfg_path}"
                 + (f" (added {', '.join(added)})" if added else " (nothing missing)"))
    _, worker_pub = key_for(worker_home, wcfg.get("agent", worker_agent))

    signing.trust(worker_home / "allowed_signers", commander, commander_pub)
    signing.trust(commander_home / "allowed_signers", wcfg.get("agent", worker_agent), worker_pub)
    steps.append("trust: worker trusts commander; commander trusts the worker")

    accept_defaults = {"accept": {"verify": {"command": "auto", "timeout_minutes": 15}, "repo_roots": repo_roots}}
    if auto:
        accept_defaults["accept"]["auto"] = True
    added = _fill(cfg, accept_defaults)
    _save(cfg_path, cfg)
    steps.append(f"commander accept defaults: {'added ' + ', '.join(added) if added else 'already set'}")

    if install_tasks:
        steps.append("scheduled task: " + poller.install(commander, every, commander_home))
        steps.append("scheduled task: " + poller.install(wcfg.get("agent", worker_agent), every, worker_home))
    return steps
