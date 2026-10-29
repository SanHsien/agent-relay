"""`python -m agent_relay <command>` — the reference mailbox client.

Any agent that can run a shell command can use it: Claude Code, Codex,
Antigravity, Hermes. Add --json to get machine-readable output.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from . import __version__, advisor, config, doorbell, envelope, gitsync, locks, poller, signing
from . import context as handover
from . import gc as relay_gc
from . import status as relay_status
from . import accept as accepter
from . import dispatch as dispatcher
from .store import Store, StoreError
from . import procs


def _out(args: argparse.Namespace, data, text: str) -> None:
    if getattr(args, "json", False):
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(text)


def _agent(args: argparse.Namespace, cfg: dict, attr: str = "agent") -> str:
    name = getattr(args, attr, None) or cfg.get("agent")
    if not name:
        raise SystemExit("no agent name: pass --agent/--from, set AGENT_RELAY_AGENT, or run init")
    return name


def _sync(args: argparse.Namespace, cfg: dict, message: str) -> None:
    if not getattr(args, "no_sync", False):
        gitsync.sync(Path(cfg["store"]), message)


def cmd_init(args, cfg) -> int:
    home = config.home()
    home.mkdir(parents=True, exist_ok=True)
    store = Path(args.store).expanduser() if args.store else home / "store"
    if args.clone and not store.exists():
        gitsync.clone(args.clone, store, args.branch)
    store.mkdir(parents=True, exist_ok=True)
    path = home / "config.json"
    current = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    current.update({"store": str(store), "agent": args.agent})
    current.setdefault("doorbells", [{"type": "toast"}, {"type": "handoff", "project_roots": []}])
    path.write_text(json.dumps(current, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"config: {path}\nstore:  {store}\nagent:  {args.agent}")
    return 0


def _full_sha(ref: str) -> str:
    """--base takes any ref in the current repo; the envelope always carries the full sha."""
    if len(ref) == 40 and all(c in "0123456789abcdef" for c in ref.lower()):
        return ref.lower()
    import subprocess

    proc = procs.run(["git", "rev-parse", "--verify", "--quiet", "--end-of-options", ref + "^{commit}"],
                          capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise SystemExit(f"--base {ref!r} is not a commit in the current repository; pass a full 40-char sha")
    return proc.stdout.strip()


def _dispatch_spec(args: argparse.Namespace) -> dict | None:
    if not args.run:
        return None
    if not args.repo:
        raise SystemExit("--run needs --repo (receiver-side path or asr:<session>)")
    if args.run == "agy":
        raise SystemExit(dispatcher.AGY_POLICY_REASON)
    spec = {"runtime": args.run, "repo": args.repo, "mode": args.mode, "timeout_minutes": args.timeout}
    if args.path:
        spec["paths"] = args.path
    if args.tdd:
        spec["tdd"] = True
    if args.base:
        spec["base"] = _full_sha(args.base)
    if args.model:
        spec["model"] = args.model
    if args.effort:
        spec["effort"] = args.effort
    return spec


def cmd_dispatch(args, cfg) -> int:
    report = dispatcher.dispatch(args.id, _agent(args, cfg), cfg, sync=not args.no_sync, force=args.force,
                                 use_default=args.default)
    text = chr(10).join(f"{k}: {v}" for k, v in report.items())
    _out(args, report, text)
    return 0 if report["status"] == "ok" else 4


def cmd_advise(args, cfg) -> int:
    body = args.body or ""
    if args.body_file:
        body = sys.stdin.read() if args.body_file == "-" else Path(args.body_file).read_text(encoding="utf-8")
    allowed = args.allow or (cfg.get("dispatch") or {}).get("runtimes") or None
    decision = advisor.decide(args.subject, body, args.path, args.mode, advisor.snapshot(cfg), allowed)
    if args.card:
        print(advisor.handoff_card(decision, args.subject, body, args.path))
        return 0
    data = decision.as_dict()
    route = f"{decision.runtime}/{decision.model or 'default'}/effort {decision.effort or 'default'}" if decision.runtime else "wait"
    _out(args, data, f"{decision.complexity} → {route}\n" + "\n".join(f"- {r}" for r in decision.reasons))
    return 0 if decision.runtime else 6


def cmd_claim(args, cfg) -> int:
    holder = args.holder or locks.default_holder(_agent(args, cfg))
    if not args.no_sync:
        gitsync.pull(Path(cfg["store"]))
    try:
        claim = locks.checkout(Path(cfg["store"]), Path(args.repo or "."), holder, args.path, args.ttl * 60)
    except locks.ClaimConflict as exc:
        _out(args, {"blocked": str(exc), "holders": exc.holders}, f"blocked: {exc}")
        return 5
    _sync(args, cfg, f"relay: checkout {claim['id']} by {holder}")
    _out(args, claim, f"claimed {claim['id']} ({', '.join(claim['paths'])}) until {claim['expires_at']}")
    return 0


def cmd_release(args, cfg) -> int:
    store = Path(cfg["store"])
    for claim in locks.active_claims(store, locks.repo_key(locks.repo_id(Path(args.repo or ".")))):
        if claim["id"] == args.claim_id:
            me = args.holder or locks.default_holder(_agent(args, cfg))
            if claim["holder"] != me and not args.force:
                _out(args, {"error": "not your claim", "holder": claim["holder"]},
                     f"{args.claim_id} belongs to {claim['holder']}; pass --force to release it anyway")
                return 2
            locks.release(store, claim, changed=args.changed or [], released_by=me)
            _sync(args, cfg, f"relay: release {claim['id']}")
            _out(args, {"released": claim["id"]}, f"released {claim['id']}")
            return 0
    _out(args, {"error": "no such active claim"}, f"no active claim {args.claim_id} in this repo")
    return 2


def cmd_claims(args, cfg) -> int:
    if not args.no_sync:
        gitsync.pull(Path(cfg["store"]))
    claims = locks.active_claims(Path(cfg["store"]), locks.repo_key(locks.repo_id(Path(args.repo or "."))))
    text = chr(10).join(f"{c['id']}  {c['holder']:>24}  until {c['expires_at']}  {', '.join(c['paths'])}" for c in claims)
    _out(args, claims, text or "(no active claims)")
    return 0


def cmd_context(args, cfg) -> int:
    if args.hook:
        return handover.main_hook()
    text = handover.bundle(Path(args.repo or "."), cfg)
    print(text or "(nothing to hand over)")
    return 0


def cmd_send(args, cfg) -> int:
    body = args.body
    if args.body_file:
        body = sys.stdin.read() if args.body_file == "-" else Path(args.body_file).read_text(encoding="utf-8")
    sender = _agent(args, cfg, "sender")
    store = Store(cfg["store"])
    if not args.no_sync:
        gitsync.pull(Path(cfg["store"]))
    thread = args.thread
    if args.reply_to and not thread:
        # A reply joins the original's thread, so a reply to a reply keeps the chain.
        hit = store.find(sender, args.reply_to)
        if hit is not None:
            thread = envelope.loads(hit[1].read_text(encoding="utf-8")).get("thread")
    env = envelope.build(
        sender=sender,
        to=args.to,
        subject=args.subject,
        body=body or "",
        kind=args.kind,
        priority=args.priority,
        reply_to=args.reply_to,
        thread=thread,
        project=args.project,
        refs=args.ref,
        dispatch=_dispatch_spec(args),
    )
    key = signing.settings(cfg, config.home())[0]
    if key:
        env = signing.sign(env, key)
    elif env.get("dispatch"):
        print("warning: no signing key (run `relay keygen`); receivers that require signatures will refuse this task",
              file=sys.stderr)
    path = store.deliver(env)
    try:
        _sync(args, cfg, f"relay: {env['from']} -> {env['to']} {env['id']}")
    except gitsync.SyncError as exc:
        # Already committed locally; the next sync pushes it. Re-sending would duplicate it.
        _out(args, {"id": env["id"], "path": str(path), "queued": True, "error": str(exc)},
             f"queued {env['id']} locally, not pushed yet ({exc}); run `sync` later, do not re-send")
        return 3
    _out(args, {"id": env["id"], "path": str(path)}, f"sent {env['id']} -> {env['to']}")
    return 0


def cmd_accept(args, cfg) -> int:
    try:
        report = accepter.accept(cfg, _agent(args, cfg), args.id, Path(args.repo), args.into, sync=not args.no_sync,
                                 push=args.push)
    except subprocess.SubprocessError as exc:  # git timed out before anything was changed
        raise SystemExit(f"error: {type(exc).__name__}: {exc}") from exc
    lines = [f"{k}: {v}" for k, v in report.items() if k not in ("verify", "notes")]
    if report.get("verify"):
        lines.append(f"verify: {report['verify']['command']} -> exit {report['verify']['exit']} "
                     f"(sha256 {report['verify']['sha256']})")
    lines += [f"note: {n}" for n in report.get("notes", [])]
    _out(args, report, chr(10).join(lines))
    return 0 if report["status"] == "accepted" else 7


def cmd_keygen(args, cfg) -> int:
    agent = _agent(args, cfg)
    key = Path(args.key).expanduser().resolve() if args.key else signing.default_key(config.home(), agent)
    public = signing.keygen(key, agent)
    if args.key:  # a non-default location has to be in the config to be used
        path = config.home() / "config.json"
        current = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        current.setdefault("signing", {})["key"] = str(key)
        path.write_text(json.dumps(current, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _out(args, {"key": str(key), "public": public},
         f"key: {key}\npublic key (give it to each receiver: relay trust {agent} <this line>):\n{public}")
    return 0


def cmd_trust(args, cfg) -> int:
    source = args.public_key
    text = sys.stdin.read() if source == "-" else (
        Path(source).read_text(encoding="utf-8") if Path(source).is_file() else source)
    allowed = signing.settings(cfg, config.home())[1]
    signing.trust(allowed, args.agent, text.strip())
    _out(args, {"agent": args.agent, "allowed_signers": str(allowed)},
         f"{args.agent} may now sign envelopes for this machine ({allowed})")
    return 0


def cmd_inbox(args, cfg) -> int:
    agent = _agent(args, cfg)
    if not args.no_sync:
        gitsync.pull(Path(cfg["store"]))
    states = ("new", "cur", "done") if args.all else ("new", "cur")
    items = Store(cfg["store"]).list(agent, states)
    rows = [{"state": s, "id": e["id"], "from": e["from"], "kind": e["kind"], "subject": e["subject"]} for s, e in items]
    text = "\n".join(f"{r['state']:4}  {r['id']}  {r['from']:>12}  {r['kind']:6}  {r['subject']}" for r in rows)
    _out(args, rows, text or f"(mailbox {agent} is empty)")
    return 0


def cmd_read(args, cfg) -> int:
    agent = _agent(args, cfg)
    if not args.no_sync:
        gitsync.pull(Path(cfg["store"]))
    env = Store(cfg["store"]).read(agent, args.id)
    _sync(args, cfg, f"relay: {agent} read {args.id}")
    text = (
        f"id:      {env['id']}\nfrom:    {env['from']} ({env.get('host', '?')})\nto:      {env['to']}\n"
        f"kind:    {env['kind']}  priority: {env.get('priority', 'normal')}\nthread:  {env.get('thread')}\n"
        f"subject: {env['subject']}\n\n{env['body']}"
    )
    _out(args, env, text)
    return 0


def cmd_ack(args, cfg) -> int:
    agent = _agent(args, cfg)
    if not args.no_sync:
        gitsync.pull(Path(cfg["store"]))
    path = Store(cfg["store"]).ack(agent, args.id)
    _sync(args, cfg, f"relay: {agent} ack {args.id}")
    _out(args, {"id": args.id, "path": str(path)}, f"acked {args.id}")
    return 0


def cmd_sync(args, cfg) -> int:
    committed = gitsync.sync(Path(cfg["store"]))
    print("synced (committed local changes)" if committed else "synced")
    return 0


def watch_once(agent: str, cfg: dict, sync: bool = True) -> list[dict]:
    """Ring for every message in new/ that this machine has not rung yet."""
    if sync:
        gitsync.pull(Path(cfg["store"]))
    rung = doorbell.load_rung(agent)
    reaped = dispatcher.reap_interrupted(agent, cfg, rung=rung)
    for env_id in reaped:
        doorbell._log(f"dispatch {env_id} interrupted: answered with status interrupted")
    if reaped and sync:  # push the replies now rather than on the next send
        gitsync.sync(Path(cfg["store"]), f"relay: {agent} reaped {len(reaped)} interrupted dispatch")
    fresh = [env for state, env in Store(cfg["store"]).list(agent, ("new",)) if env["id"] not in rung]
    report = []
    for env in fresh:
        entry = {"id": env["id"], "subject": env["subject"], "bells": doorbell.ring(env, cfg["doorbells"])}
        auto = automate(env, agent, cfg)
        if auto:
            entry["auto"] = auto
            doorbell._log(f"auto {env['id']} -> {auto}")
        report.append(entry)
        rung.append(env["id"])
        doorbell.save_rung(agent, rung)  # per message: a run cut short does not re-ring the rest
    if relay_status.heartbeat(cfg, agent) and sync:
        gitsync.sync(Path(cfg["store"]), f"relay: {agent} heartbeat")
    return report


def cmd_status(args, cfg) -> int:
    if not args.no_sync:
        try:
            gitsync.pull(Path(cfg["store"]))
        except gitsync.SyncError as exc:
            print(f"warning: store pull failed, showing the local copy: {exc}", file=sys.stderr)
    data = relay_status.overview(cfg, args.stale_minutes)
    _out(args, data, relay_status.render(data))
    return 8 if any(h["stale"] for h in data["hosts"]) or data["overdue"] else 0


def cmd_gc(args, cfg) -> int:
    agent = _agent(args, cfg)
    actions = relay_gc.plan(cfg, agent, args.older_than, args.repo, force=args.force)
    if args.apply:
        relay_gc.apply(actions)
        if any(a["kind"] == "done-mail" and a.get("done") for a in actions):
            _sync(args, cfg, f"relay: {agent} gc {sum(a['kind'] == 'done-mail' for a in actions)} done messages")
    _out(args, actions, relay_gc.render(actions, args.apply))
    return 9 if any(a.get("error") for a in actions) else 0


def accept_repo(env: dict, cfg: dict) -> Path | None:
    """The commander's clone for a dispatch reply: the original task's repo folder name under accept.repo_roots."""
    hit = Store(cfg["store"]).find(env["from"], env.get("reply_to", ""))
    if hit is None:
        return None
    try:
        task = envelope.loads(hit[1].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    repo = (task.get("dispatch") or {}).get("repo") or task.get("project")
    if not repo:
        return None
    name = Path(str(repo).replace("\\", "/")).name
    for root in (cfg.get("accept") or {}).get("repo_roots") or []:
        candidate = Path(root) / name
        if (candidate / ".git").exists():
            return candidate
    return None


def automate(env: dict, agent: str, cfg: dict) -> str | None:
    """What this machine does on its own with a new message, beyond ringing (SPEC 18)."""
    if env["kind"] == "note" and env.get("reply_to") and env["subject"].startswith(("accepted:", "rejected:")) \
            and cfg.get("auto_ack_notes", True):
        Store(cfg["store"]).ack(agent, env["id"])  # pure information: the accept already happened
        return "acked accept note"
    settings = cfg.get("accept") or {}
    result = env.get("result") or {}
    if env["kind"] == "result" and settings.get("auto") and result.get("status") == "ok" and result.get("pushed"):
        repo = accept_repo(env, cfg)
        if repo is None:
            return "auto-accept skipped: no clone of the task's repo under accept.repo_roots"
        args = ["accept", env["id"], "--repo", str(repo), "--agent", agent] + (["--push"] if settings.get("push") else [])
        return "auto-accept " + dispatcher.launch(args, f"accept {env['id'][-6:]}", settings.get("window", False))
    return None


def cmd_setup_local(args, cfg) -> int:
    from . import setup as relay_setup

    try:
        steps = relay_setup.setup_local(config.home(), Path(args.worker_home).expanduser(), args.worker_agent,
                                        args.repo_root or None, args.runtime or None, args.every, args.auto,
                                        install_tasks=not args.no_tasks)
    except (RuntimeError, signing.SigningError) as exc:
        raise SystemExit(f"error: {exc}") from exc
    _out(args, steps, chr(10).join(steps))
    return 0


def cmd_watch(args, cfg) -> int:
    agent = _agent(args, cfg)
    while True:
        try:
            report = watch_once(agent, cfg, sync=not args.no_sync)
        except (gitsync.SyncError, OSError) as exc:
            doorbell._log(f"watch {agent} failed: {exc}")
            if args.once:
                print(f"watch failed: {exc}", file=sys.stderr)
                return 1
            report = []
        if report or args.once:
            _out(args, report, "\n".join(f"rang {r['id']} {r['subject']} {r['bells']}" for r in report) or "no new mail")
        if args.once:
            return 0
        time.sleep(args.interval)


def cmd_poller(args, cfg) -> int:
    agent = _agent(args, cfg)
    if args.action == "install":
        print(f"registered scheduled task {poller.install(agent, args.every, config.home())} (every {args.every} min)")
    else:
        print("removed" if poller.uninstall(agent) else "no such task")
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agent-relay", description="File + git mailbox for AI agents")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="write ~/.agent-relay/config.json")
    s.add_argument("--agent", required=True)
    s.add_argument("--store")
    s.add_argument("--clone", help="git URL of the store repository")
    s.add_argument("--branch", default="mailbox")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("send", help="deliver an envelope")
    s.add_argument("--to", required=True)
    s.add_argument("--from", dest="sender")
    s.add_argument("--subject", required=True)
    s.add_argument("--body", default="")
    s.add_argument("--body-file", help="path, or - for stdin")
    s.add_argument("--kind", default="note", choices=envelope.KINDS)
    s.add_argument("--priority", default="normal", choices=envelope.PRIORITIES)
    s.add_argument("--reply-to")
    s.add_argument("--thread", help="defaults to the original message's thread when replying")
    s.add_argument("--project", help="receiver-side project folder (enables the handoff doorbell)")
    s.add_argument("--ref", action="append", help="related path or URL; repeatable")
    g = s.add_argument_group("dispatch (kind=task only): run this task in a fresh agent session")
    g.add_argument("--run", choices=(*dispatcher.RUNTIMES, "auto"),
                   help="runtime the receiver should start; auto = receiver picks by complexity and its quotas")
    g.add_argument("--repo", help="receiver-side git repo, or asr:<session>")
    g.add_argument("--mode", default="build", choices=dispatcher.MODES)
    g.add_argument("--model")
    g.add_argument("--effort")
    g.add_argument("--timeout", type=int, default=dispatcher.DEFAULT_TIMEOUT_MIN, help="minutes")
    g.add_argument("--path", action="append", help="file pattern the worker may change (checked out); repeatable")
    g.add_argument("--tdd", action="store_true", help="require a test that fails before the change (red) and passes after")
    g.add_argument("--base", help="commit sha the worker starts from (receiver fetches origin if needed)")
    s.set_defaults(func=cmd_send)

    s = sub.add_parser("advise", help="pick runtime/model for a task from its complexity and local quotas")
    s.add_argument("--subject", required=True)
    s.add_argument("--body", default="")
    s.add_argument("--body-file")
    s.add_argument("--path", action="append")
    s.add_argument("--mode", default="build", choices=dispatcher.MODES)
    s.add_argument("--allow", action="append", help="runtimes that may be picked (default: dispatch.runtimes)")
    s.add_argument("--card", action="store_true", help="print the hand-over card instead")
    s.set_defaults(func=cmd_advise)

    s = sub.add_parser("claim", help="check out file patterns of a repo before changing them")
    s.add_argument("--repo")
    s.add_argument("--path", action="append", required=True)
    s.add_argument("--holder", help="who holds it (default: AGENT_RELAY_HOLDER or manual:<agent>)")
    s.add_argument("--ttl", type=int, default=60, help="minutes")
    s.add_argument("--agent")
    s.set_defaults(func=cmd_claim)

    s = sub.add_parser("release", help="check a claim back in")
    s.add_argument("claim_id")
    s.add_argument("--repo")
    s.add_argument("--changed", action="append", help="file actually changed; repeatable")
    s.add_argument("--holder", help="who you are (default: AGENT_RELAY_HOLDER or manual:<agent>@<host>)")
    s.add_argument("--agent")
    s.add_argument("--force", action="store_true", help="release someone else's claim")
    s.set_defaults(func=cmd_release)

    s = sub.add_parser("claims", help="list active claims of a repo")
    s.add_argument("--repo")
    s.set_defaults(func=cmd_claims)

    s = sub.add_parser("context", help="print a repo's hand-over bundle (or act as a SessionStart hook)")
    s.add_argument("--repo", help="repository folder (default: current folder)")
    s.add_argument("--hook", action="store_true", help="read a Claude Code SessionStart payload on stdin")
    s.set_defaults(func=cmd_context)

    s = sub.add_parser("dispatch", help="run a dispatch task in a worktree and reply with the result")
    s.add_argument("id")
    s.add_argument("--agent")
    s.add_argument("--store", help="store folder override")
    s.add_argument("--force", action="store_true", help="run a task that is already done")
    s.add_argument("--default", action="store_true", help="a plain task: use this machine's dispatch.default")
    s.set_defaults(func=cmd_dispatch)

    for name, func, helptext in (("read", cmd_read, "show a message and mark it read"), ("ack", cmd_ack, "mark done")):
        s = sub.add_parser(name, help=helptext)
        s.add_argument("id")
        s.add_argument("--agent")
        s.set_defaults(func=func)

    s = sub.add_parser("accept", help="fetch a worker's pushed branch, verify it here and merge (SPEC section 14)")
    s.add_argument("id", help="the dispatch reply (kind=result) in your mailbox")
    s.add_argument("--repo", required=True, help="your clone of the repo to merge into")
    s.add_argument("--into", help="the branch to merge into (must be checked out; default: the current one)")
    s.add_argument("--push", action="store_true", help="push the branch to origin after a successful merge")
    s.add_argument("--agent")
    s.set_defaults(func=cmd_accept)

    s = sub.add_parser("keygen", help="create this mailbox's envelope signing key (SPEC section 13)")
    s.add_argument("--agent")
    s.add_argument("--key", help="where to write it (default ~/.agent-relay/keys/<agent>_ed25519)")
    s.set_defaults(func=cmd_keygen)

    s = sub.add_parser("trust", help="allow a public key to sign as a mailbox on this machine")
    s.add_argument("agent", help="the mailbox this key may speak for")
    s.add_argument("public_key", help="a .pub file, the key line itself, or - for stdin")
    s.set_defaults(func=cmd_trust)

    s = sub.add_parser("status", help="open mail per mailbox, receiver heartbeats, runs on this machine")
    s.add_argument("--stale-minutes", type=float, default=relay_status.DEFAULT_STALE_MIN,
                   help="a heartbeat older than this counts as a stopped receiver")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("gc", help="remove finished dispatch worktrees, branches, run records and old done mail")
    s.add_argument("--older-than", type=float, default=relay_gc.DEFAULT_DAYS, help="days since the run replied")
    s.add_argument("--repo", action="append", default=[], help="also look in this repo (repo_roots are scanned)")
    s.add_argument("--agent")
    s.add_argument("--apply", action="store_true", help="really delete (default: dry run)")
    s.add_argument("--force", action="store_true",
                   help="also remove dirty worktrees and unmerged branches of work never accepted")
    s.set_defaults(func=cmd_gc)

    s = sub.add_parser("setup-local", help="create or repair this machine's commander + worker setup (idempotent)")
    s.add_argument("--worker-home", default=str(Path.home() / ".agent-relay-worker"))
    s.add_argument("--worker-agent", default="claude-code")
    s.add_argument("--repo-root", action="append", help="repos the worker may work in (default C:/GitHub)")
    s.add_argument("--runtime", action="append", help="worker runtimes (default claude, codex, hermes)")
    s.add_argument("--every", type=int, default=5, help="watch interval in minutes")
    s.add_argument("--auto", action="store_true",
                   help="also turn on dispatch.default and accept.auto (no human in the loop, SPEC 18)")
    s.add_argument("--no-tasks", action="store_true", help="do not (re)register the scheduled tasks")
    s.set_defaults(func=cmd_setup_local)

    s = sub.add_parser("inbox", help="list open messages")
    s.add_argument("--agent")
    s.add_argument("--all", action="store_true", help="include done")
    s.set_defaults(func=cmd_inbox)

    s = sub.add_parser("sync", help="commit, pull and push the store")
    s.set_defaults(func=cmd_sync)

    s = sub.add_parser("watch", help="ring doorbells for new mail")
    s.add_argument("--agent")
    s.add_argument("--once", action="store_true")
    s.add_argument("--interval", type=int, default=60, help="seconds between checks")
    s.set_defaults(func=cmd_watch)

    s = sub.add_parser("poller", help="Windows scheduled task running watch --once")
    s.add_argument("action", choices=("install", "uninstall"))
    s.add_argument("--agent")
    s.add_argument("--every", type=int, default=5, help="minutes")
    s.set_defaults(func=cmd_poller)

    for s in sub.choices.values():
        s.add_argument("--json", action="store_true")
        s.add_argument("--no-sync", action="store_true", help="skip git pull/push")
        # Several relay homes can live on one machine (a commander and a worker); a scheduled task
        # or a new terminal tab does not inherit AGENT_RELAY_HOME, so it is passed explicitly.
        s.add_argument("--home", help="relay home folder (default: AGENT_RELAY_HOME or ~/.agent-relay)")
    return p


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = parser().parse_args(argv)
    if args.cmd == "context" and args.hook:
        return handover.main_hook()  # before config.load: a hook must never fail
    for attr, var in (("home", "AGENT_RELAY_HOME"), ("store", "AGENT_RELAY_STORE")):
        if getattr(args, attr, None):
            os.environ[var] = getattr(args, attr)
    cfg = config.load()
    try:
        return args.func(args, cfg)
    except (StoreError, envelope.EnvelopeError, gitsync.SyncError, dispatcher.DispatchError,
            signing.SigningError, relay_gc.GcError, accepter.AcceptError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
