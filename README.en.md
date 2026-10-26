English | [繁體中文](README.md)

# agent-relay

[![CI](https://github.com/SanHsien/agent-relay/actions/workflows/ci.yml/badge.svg)](https://github.com/SanHsien/agent-relay/actions/workflows/ci.yml)

**Protocol specification and reference CLI** for Claude Code, Codex, Antigravity, and Hermes to send messages, dispatch tasks, and report results to one another. Native Windows 11 first, built solely on the Python standard library; cross-machine delivery runs over `git push`/`pull` with no public IP and no extra API keys required.

This repository also serves as the **master index** of the "Multi-Agent Dispatch and Verification Pipeline": the protocol lives here, while each target repository adds only a thin adapter layer and links back to this index in its README. Task provenance and acceptance criteria live in [ai-inbox](https://github.com/SanHsien/ai-inbox) under *Multi-Agent Dispatch and Verification Pipeline*.

- Protocol specification: [SPEC.en.md](SPEC.en.md) ([繁體中文](SPEC.md))
- Operating guide for agents: [AGENTS.md](AGENTS.md)
- Receiving mail in an existing Antigravity App conversation: [sidecar setup, signing, and acceptance](docs/antigravity-app-mailbox.en.md) (distinct from `--run agy`, which opens a new read-only CLI worker)
- Nostr NIP-01 bridging evaluation (field mapping, signature gap, conclusion): [docs/nostr-nip01-evaluation.en.md](docs/nostr-nip01-evaluation.en.md)

## Quick Start

```powershell
git clone https://github.com/SanHsien/agent-relay.git
cd agent-relay

# Once per machine: set the mailbox name and clone the store's mailbox branch
python -m agent_relay init --agent claude-code --clone https://github.com/<you>/<store-repo>.git --branch mailbox

# Send mail, check inbox, read, and acknowledge
python -m agent_relay send --to hermes --kind task --subject "Run test suite" --body-file task.md
python -m agent_relay inbox
python -m agent_relay read <id>
python -m agent_relay ack <id>

# Poll and ring doorbells every 5 minutes (Windows Task Scheduler)
python -m agent_relay poller install --every 5
```

Automatic hand-over for new sessions (no manual context pasting):

```powershell
python -m agent_relay context --repo C:\GitHub\my-repo     # Inspect hand-over bundle
# Claude Code SessionStart hook: see SPEC.en.md Section 8
```

Pass `--json` to any command for machine-readable output; pass `--no-sync` to operate locally without touching git. Every command accepts `--home <dir>` to select a specific relay home.

### Single Machine: Commander + Local Worker

A single computer can act as both commander and receiving worker. Each side uses its own relay home, shares the same store, registers its own scheduled task, and trusts the other side's signing key. Set up or repair everything in one command (SPEC §19):

```powershell
python -m pip install -e C:\GitHub\agent-relay            # Run python -m agent_relay from any directory
python -m agent_relay setup-local --auto                   # Commander + local worker + two scheduled tasks; idempotent
```

Worker policy lives under `dispatch` in `$HOME\.agent-relay-worker\config.json` (SPEC §7, §11, §15, §18): allowed runtimes and `repo_roots`, `senders: ["commander"]`, `verify: {"command": "auto"}`, `push_branch`, `max_concurrent`, `daily_limit`, and `default` for plain tasks.

Day-to-day dispatch and acceptance (on the commander side):

```powershell
# Dispatch: start from origin/main (run inside the repo so --base resolves, or pass a 40-char SHA), require TDD, restrict editable paths
python -m agent_relay send --to claude-code --kind task --run codex --repo C:\GitHub\<repo> `
  --base origin/main --tdd --path src/ --path tests/ --subject "..." --body-file task.md
python -m agent_relay status                                   # Mailboxes, heartbeats, active dispatches
python -m agent_relay accept <reply-id> --repo C:\GitHub\<repo> # Re-run verification locally; merge only if green
```

See [docs/evidence/2026-10-21-real-e2e.md](docs/evidence/2026-10-21-real-e2e.md) for the complete log of the first real end-to-end work order.

## How It Works

```text
Commander ──send──▶ store/mailboxes/<worker>/new/ ──git push──▶ GitHub (private store repo, mailbox branch)
                                                                  │
                                    Worker machine scheduled watch --once ◀─┘ git pull
                                       ├─ Doorbell: Windows toast / Telegram / dropoff handoff card
                                       └─ Worker read → build → send --kind result → ack
```

## Feature Map

| Feature | Command / Config | SPEC | Evidence Record | ai-inbox Task |
| --- | --- | --- | --- | --- |
| Send, receive, cross-machine sync | `send` / `inbox` / `read` / `ack` / `sync` | 1–3 | [Phase 1](docs/evidence/2026-10-20-phase1.md) | T-20261019-01 |
| Doorbells (toast, Telegram, handoff card), scheduled polling | `watch`, `poller install` | 4–5 | [Phase 1](docs/evidence/2026-10-20-phase1.md) | T-20261019-02 |
| Auto-spawn new session from message (worktree) | `send --run claude\|codex\|agy\|hermes`, `dispatch` | 7 | [Phase 2](docs/evidence/2026-10-20-phase2.md) | T-20261018-15, T-20261017-31 |
| Auto hand-over for new sessions (hand-over bundle) | `context`, `hooks/session_start.py` | 8 | [T-08](docs/evidence/2026-10-20-t08-handover.md) | T-20261019-08 |
| Check-out locks, scope check, audit trail | `--path`, `claim` / `claims` / `release` | 9 | [T-07](docs/evidence/2026-10-20-t07-checkout.md) | T-20261019-07 |
| Quota-aware routing, auto-retry on disconnect | `--run auto`, `advise` | 10 | [T-16](docs/evidence/2026-10-20-t16-advisor.md) | T-20261018-16 |
| Verification gates (TDD, no stubs, real execution logs) | `--tdd`, `dispatch.verify` | 11 | [T-18](docs/evidence/2026-10-20-t18-gates.md) | T-20261018-18 |
| Fallback escalation debugging loop | `dispatch.escalation` | 12 | [Benchmark report](docs/reports/2026-10-20-space-bunny-benchmark.md) | T-20261017-32 |
| Hermes worker (writes confined to worktree, usage in reply) | `send --run hermes` | 7 | [T-03](docs/evidence/2026-10-21-t03-hermes.md) | T-20261021-03 |
| Sender verification (SSH message signing, deny-by-default senders) | `keygen`, `trust`, `dispatch.require_signature` | 13 | [T-07 Signing](docs/evidence/2026-10-21-t07-signing.md) | T-20261021-07 |
| Pinned base commit, structured result envelope | `send --base`, reply `result` field | 7 | [T-08](docs/evidence/2026-10-21-t08-base-result.md) | T-20261021-08 |
| Result branch push, commander verification & merge | `dispatch.push_branch`, `accept` | 14 | [T-05](docs/evidence/2026-10-21-t05-accept.md) | T-20261021-05 |
| Codex worker (`workspace-write` pinned to worktree) | `send --run codex` | 7 | [T-04](docs/evidence/2026-10-21-t04-codex.md) | T-20261021-04 |
| Concurrency / daily limits, duplicate-run guard | `dispatch.max_concurrent`, `daily_limit` | 15 | [T-09](docs/evidence/2026-10-21-t09-limits.md) | T-20261021-09 |
| Fleet overview and receiver heartbeats | `status`, `hosts/` | 16 | [T-10](docs/evidence/2026-10-21-t10-status.md) | T-20261021-10 |
| Garbage collection for worktrees / branches / run logs | `gc` | 17 | [T-11](docs/evidence/2026-10-21-t11-gc.md) | T-20261021-11 |
| Unattended mail handling (default worker, auto-accept, auto-ack notes) | `dispatch.default`, `accept.auto` | 18 | — | — |
| One-command local setup and recovery | `setup-local` | 19 | [Real E2E](docs/evidence/2026-10-21-real-e2e.md) | — |

Windows compatibility research (herdr, magpie, opencodex, cortex): [Report](docs/reports/2026-10-20-windows-session-control.md).

## Ecosystem Directory

The pipeline is organized into three phases, reusing the repositories below. The "Adapter Layer" column describes the thin integration added to each repository; completion evidence is attached to the corresponding task in ai-inbox.

### Phase 1: Message Transport

| Repo | Role | Adapter Layer | Task |
| --- | --- | --- | --- |
| [agentdeck](https://github.com/SanHsien/agentdeck) | Windows system tray, scheduled polling | Tray checks mailbox each cycle and shows toast notifications; its Task Scheduler pattern is reused here | T-20261019-01 |
| [Private Repo: Personal Memory Store] | Message store (`mailbox` branch) | Stores messages on an orphan branch without touching `main` | T-20261019-01 |
| [agent-session-restore](https://github.com/SanHsien/agent-session-restore) | Session registry and fast resume | Messages set `--project asr:<session-name>`; the doorbell resolves the project path from its session registry before dropping a handoff card | T-20261019-02 |
| [line-desktop-mcp](https://github.com/SanHsien/line-desktop-mcp) | LINE Desktop bridge | Mobile doorbell candidate. Because it requires manual confirmation per message, automated mobile notifications currently use Telegram (`command` doorbell) | T-20261019-02 |

### Phase 2: Dispatch Contracts

| Repo | Role | Task |
| --- | --- | --- |
| [agent-cortex](https://github.com/SanHsien/agent-cortex) | Native Windows control plane, spawning new sessions | T-20261018-15 |
| [opencodex](https://github.com/SanHsien/opencodex) | Universal provider proxy | T-20261018-15 |
| [claude-code-projects-workflow](https://github.com/SanHsien/claude-code-projects-workflow) | Pre-dispatch scope check and merge conflict prediction for worktrees | T-20261018-15, T-20261019-07 |
| [agent-advisor](https://github.com/SanHsien/agent-advisor) | Dispatch routing and contracts | T-20261017-31, T-20261018-16 |
| [agy-staff](https://github.com/SanHsien/agy-staff) | Antigravity CLI as execution worker | T-20261017-31 |
| [claude-obsidian](https://github.com/SanHsien/claude-obsidian) | Obsidian handoff note synchronization | T-20261019-08 |
| [cc-session-reader](https://github.com/SanHsien/cc-session-reader) | Compact session transcript reader | T-20261019-08 |
| [Private Repo: Personal Command Center] | Command center | T-20261019-08 |
| [codebase-memory-mcp](https://github.com/SanHsien/codebase-memory-mcp) | Codebase knowledge graph | T-20261019-07 |
| [Private Repo: Environment Audit Tool] | Environment auditing | T-20261019-07 |
| [codex-reset-checker](https://github.com/SanHsien/codex-reset-checker) | Quota and reset-time lookup | T-20261018-16 |
| [codex-auto-retry](https://github.com/SanHsien/codex-auto-retry) | In-place auto-resume on rate limits and disconnects | T-20261018-16 |
| [Antigravity-Manager](https://github.com/SanHsien/Antigravity-Manager) | Antigravity account switching | T-20261018-16 |

### Phase 3: Verification Loop

| Repo | Role | Task |
| --- | --- | --- |
| [harness-guard](https://github.com/SanHsien/harness-guard) | Blocking hooks (unevidenced completion claims, commits over red tests) | T-20261018-18, T-20261019-07 |
| [ai-quality-gates](https://github.com/SanHsien/ai-quality-gates) | Executable specifications, coverage, and mutation gates | T-20261018-18 |
| [agent-governance-toolkit](https://github.com/SanHsien/agent-governance-toolkit) | Runtime policy and sandbox auditing | T-20261018-18 |
| [SkillSpector](https://github.com/SanHsien/SkillSpector) | Skill supply-chain scanning | T-20261018-18 |
| [agent-advisor](https://github.com/SanHsien/agent-advisor) | Independent reviewer track and failure escalation re-dispatch | T-20261017-32 |

agent-governance-toolkit (AGT) is loaded from the `AGENT_GOVERNANCE_PATH` environment variable or the default path `C:/GitHub/agent-governance-toolkit`. If the receiver's `dispatch.verify` requests governance checks (`governance`, `no_credentials`, `blocked_paths`, `max_files`, `scan_subprocesses`, `strict`) but AGT cannot be loaded, the gate returns `unverified` and does not pass; environments without AGT must explicitly set `"governance_fallback": "builtin"` to degrade to simplified built-in checks (reported as degraded; the subprocess scan is reported as not run). See [SPEC.en.md](SPEC.en.md) Section 11.

`[Private Repo: …]` denotes a public alias for a private repository; real private repository names never appear in public files.

## Testing

```powershell
python -m unittest discover -s tests -v
```

## License

MIT
