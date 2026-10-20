"""`python -m agent_relay <command>` — the reference mailbox client.

Any agent that can run a shell command can use it: Claude Code, Codex,
Antigravity, Hermes. Add --json to get machine-readable output.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import advisor, config, doorbell, envelope, gitsync, locks, poller
from . import context as handover
from . import dispatch as dispatcher
from .store import Store, StoreError


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


def _dispatch_spec(args: argparse.Namespace) -> dict | None:
    if not args.run:
        return None
    if not args.repo:
        raise SystemExit("--run needs --repo (receiver-side path or asr:<session>)")
    spec = {"runtime": args.run, "repo": args.repo, "mode": args.mode, "timeout_minutes": args.timeout}
    if args.path:
        spec["paths"] = args.path
    if args.model:
        spec["model"] = args.model
    if args.effort:
        spec["effort"] = args.effort
    return spec


def cmd_dispatch(args, cfg) -> int:
    report = dispatcher.dispatch(args.id, _agent(args, cfg), cfg, sync=not args.no_sync, force=args.force)
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
        report.append({"id": env["id"], "subject": env["subject"], "bells": doorbell.ring(env, cfg["doorbells"])})
        rung.append(env["id"])
        doorbell.save_rung(agent, rung)  # per message: a run cut short does not re-ring the rest
    return report


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
        print(f"registered scheduled task {poller.install(agent, args.every)} (every {args.every} min)")
    else:
        print("removed" if poller.uninstall(agent) else "no such task")
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agent-relay", description="File + git mailbox for AI agents")
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
    s.add_argument("--home", help="relay home folder (the doorbell passes it; a new tab may not inherit env)")
    s.add_argument("--store", help="store folder override")
    s.add_argument("--force", action="store_true", help="run a task that is already done")
    s.set_defaults(func=cmd_dispatch)

    for name, func, helptext in (("read", cmd_read, "show a message and mark it read"), ("ack", cmd_ack, "mark done")):
        s = sub.add_parser(name, help=helptext)
        s.add_argument("id")
        s.add_argument("--agent")
        s.set_defaults(func=func)

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
    except (StoreError, envelope.EnvelopeError, gitsync.SyncError, dispatcher.DispatchError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
