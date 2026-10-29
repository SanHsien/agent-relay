English | [繁體中文](SPEC.md)

# agent-relay Protocol Specification v1

This specification defines how agents (Claude Code, Codex, Antigravity, Hermes, and the commander) send mail, receive mail, and ring doorbells for one another. The reference implementation lives in `agent_relay/` in this repository. Any language that reads and writes files according to this specification can interoperate without depending on the reference CLI.

```text
Sender                         Shared store (mailbox branch of git repo)              Receiver
relay send ──write new/──▶ mailboxes/<recipient>/new/<id>.json ──git push──▶ remote
                                                                              │ git pull (scheduled every N min)
                                                        relay watch --once ◀──┘
                                                           ├─ Doorbell: toast / command / handoff card
                                                           ▼
                                       relay read (new→cur) ─▶ build ─▶ relay send --kind result --reply-to <id>
                                                                        relay ack (cur→done)
```

## 1. Envelope

One message = one UTF-8 JSON file named `<id>.json`.

| Field | Required | Type | Description |
| --- | --- | --- | --- |
| `schema` | Yes | string | Fixed as `agent-relay/envelope@1`. Unknown versions must be rejected rather than guessed |
| `id` | Yes | string | `YYYYMMDDTHHMMSSmmmZ-<6 hex>` (UTC). Lexicographical order = chronological order. See "Naming Rules" below |
| `from` | Yes | string | Sender mailbox name; see "Naming Rules" below |
| `to` | Yes | string | Recipient mailbox name; same rules |
| `created_at` | Yes | string | ISO 8601 UTC, e.g., `2026-10-20T01:23:45Z` |
| `kind` | Yes | enum | `task` dispatch, `result` execution report, `reply` general reply, `note` notification |
| `subject` | Yes | string | Single-line title; cannot be blank or contain newlines |
| `body` | Yes | string | Markdown body. Task messages must state the goal, acceptance criteria, and report format |
| `priority` | No | enum | `high` / `normal` (default) / `low` |
| `thread` | No | string | Thread ID. For a new message, equals its own `id`; replies inherit the original message's `thread` (not its `id`), so replies to replies stay in the same thread |
| `reply_to` | No | string | The `id` of the message being replied to |
| `host` | No | string | Sender hostname for diagnostics; cannot contain newlines |
| `project` | No | string | Project directory on the receiver machine, or `asr:<session-name-or-id>` (resolved via the [agent-session-restore](https://github.com/SanHsien/agent-session-restore) registry). When `kind=task` and this field is set, the `handoff` doorbell drops a handoff card in that project |
| `refs` | No | string[] | Related file paths or URLs |

Unrecognized fields must be preserved without raising an error so new fields can be added later. `refs` must be an array of strings; `subject`, `host`, and `project` must not contain newlines (they are written into handoff card frontmatter).

### Naming Rules

Mailbox names and `id` (as well as `thread` and `reply_to`) become directory and file names, so they must match the entire string (do not use `^...$` with `re.match`, which allows trailing newlines):

- Mailbox name: `[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9_-])?`
- ID: `[0-9A-Za-z](?:[0-9A-Za-z._-]{0,126}[0-9A-Za-z_-])?`
- Cannot end with `.` (Windows strips trailing dots, turning `codex.` into the same folder as `codex`).
- Cannot be a Windows reserved device name: `con`, `prn`, `aux`, `nul`, `com0-9`, `lpt0-9` (case-insensitive, including with extensions such as `nul.txt`).

### Secrets and Public Repositories

- Never put API keys, tokens, or passwords in envelope bodies. Even when the store is a private repository, messages enter git history and are hard to purge completely.
- In any file committed to a public repository, private repositories must be written as `[Private Repo: <Alias>]` (see `private-aliases.json` in [ai-inbox](https://github.com/SanHsien/ai-inbox)).

## 2. Mailbox Directory Layout (Store)

```text
<store>/mailboxes/<agent>/new/<id>.json    Delivered, unread
<store>/mailboxes/<agent>/cur/<id>.json    Read, not yet acknowledged
<store>/mailboxes/<agent>/done/<id>.json   Completed (ack)
```

- **Atomic writes**: write to a temporary file in the same directory (prefixed with `.tmp-`), then rename to `<id>.json`. Readers ignore `.tmp-*`, and git never commits them.
- **File identity integrity**: readers skip any file whose filename does not equal `<id>.json` or whose `to` field does not match the containing mailbox (otherwise a file could spoof another message's ID and silence its doorbell).
- **State transitions via rename only**: `new → cur → done` without modifying file contents, ensuring git merges across machines never conflict on the same line.
- **Single-owner mailbox**: only the owner of the recipient mailbox moves a message after delivery. Once the sender writes to `new/`, it never touches that file again.
- Duplicate `id` values within the same mailbox must be rejected.

## 3. Cross-Machine Transport (git)

- The store is a working tree of a git repository. The reference deployment uses an **orphan `mailbox` branch** of `[Private Repo: Personal Memory Store]`, cloned separately on each machine so it never touches `main` and is unaffected by daily auto-sync on `main`.
- Sending: write file → `git add -A -- mailboxes` → commit (only `mailboxes/`) → `pull --rebase` → `push`. If push is rejected, retry with rebase up to 3 times. Because filenames are unique, rebases do not conflict; if a rebase ever fails, run `rebase --abort` so the store is never left mid-rebase.
- If push still fails after retries, the message is already committed locally and will go out on the next sync. The reference CLI exits with code 3 and notes "queued locally, not pushed"—**do not resend** (resending creates a duplicate message).
- Receiving: `git pull --rebase`.
- No public IP and no extra API keys required: each machine can already push to its own GitHub repository.
- Latency = the polling interval of the receiver's scheduled task (5 minutes by default).

## 4. Doorbells

Doorbells are notifications only; **the message files in the store are the source of truth**. A doorbell failure never affects message delivery.

- `relay watch --once`: after pulling, rings every configured doorbell in order for each message in `new/` that has not yet rung on this machine. Rung IDs are recorded in `~/.agent-relay/rung-<agent>.json` (local per machine, not tracked in git). **Save atomically after each message rings** (write temp file, then rename) so if a cycle is killed mid-way, the next cycle does not re-ring messages that already rang.
- If one doorbell fails, log the error and continue to the next doorbell.
- Three built-in doorbell types:

| type | What it does | Configuration |
| --- | --- | --- |
| `toast` | Windows bottom-right toast notification (WinRT, no third-party packages required) | None |
| `command` | Executes a command directly (no shell), substituting `{id}`, `{from}`, `{to}`, `{subject}`, `{kind}`, `{summary}` in arguments. **Rejects `.bat`/`.cmd`**: Windows hands those to `cmd.exe`, which re-parses arguments and turns `&` inside message text into shell commands. Because values may start with `-`, pass `--` before positional placeholders if the target program accepts flags | `argv`, `timeout` (seconds) |
| `handoff` | When `kind=task` and `project` is set, writes a dropoff/pickup handoff card into `<project>/.claude/handoffs/` (named `relay-<id>.md`, idempotent on re-ring) so the next session's `/pickup` picks it up. The project must reside under `project_roots` (refused if unset, because the sender chooses the project path and the receiver must pre-approve allowed parent directories). The reply command inside the handoff card omits the message subject | `project_roots` (array of directories, required) |

Every doorbell accepts an optional `"kinds": ["task", "result"]` filter. Mobile notifications use `command` wired to an existing Telegram script, for example:

```json
{"type": "command", "kinds": ["task", "result"], "argv": ["bash", "C:/Users/<you>/.claude/scripts/telegram-send.sh", "{summary}"]}
```

## 5. Scheduled Polling (Windows)

`relay poller install --agent <name> --every 5` registers a Windows Scheduled Task named `agent-relay-watch-<name>` that runs `watch --once` every 5 minutes. A single machine can host multiple relay homes—for example, `~/.agent-relay` for the commander and `~/.agent-relay-worker` for the worker. Every command accepts `--home`; scheduled tasks registered from a non-default home automatically pass `--home`, since scheduled tasks and new terminal tabs do not inherit environment variables. The task is registered via XML so `StartWhenAvailable` is enabled: if a cycle is missed during sleep or shutdown, Windows runs it upon wake/boot (reusing the scheduled auto-resume pattern from [agentdeck](https://github.com/SanHsien/agentdeck)).

## 6. Task Body Conventions

The `body` of a `kind=task` message should include at least these four sections; Phase 2 dispatch contracts add structured envelope fields alongside them:

```markdown
## 目標與動機 (Goal & Motivation)
## 驗收條件（DoD） (Definition of Done)
## 回報格式 (Report Format)
## 指定 model / runtime（選填） (Requested model / runtime, optional)
```

Once finished, the receiver sends `kind=result` with `reply_to=<original-id>`, attaching concrete evidence (test output, commit SHA, file paths) in the body, and then runs `ack` on the original message.

## 7. Dispatch Contract (`dispatch`)

A `kind=task` message may carry a `dispatch` object instructing the receiver to **automatically spawn a new agent session** to perform the work and send back the result when done.

```text
Commander send --run claude --repo <receiver-repo>
   └─▶ Receiver watch (scheduled) ── dispatch doorbell ──▶ New Windows Terminal tab: relay dispatch <id>
                                                            ├─ git worktree: <repo>/.relay-worktrees/<id>, branch relay/<id>
                                                            ├─ headless claude / codex / hermes (kills entire process tree on timeout)
                                                            └─ send kind=result (reply_to, same thread) + ack original message
```

| Field | Required | Description |
| --- | --- | --- |
| `runtime` | Yes | `claude`, `claude-3p`, `codex`, `hermes` (`agy` disabled by policy) |
| `repo` | Yes | Git repository path on the receiver machine, or `asr:<session-name>` |
| `mode` | No | `build` (default: worker edits files inside the worktree and relay commits afterward) or `plan` (read-only) |
| `model`, `effort` | No | Model ID and reasoning effort passed to the runtime. `model` only allows `[A-Za-z0-9._:/-]` and cannot start with `-`; `effort` must be `low` / `medium` / `high` / `xhigh` |
| `timeout_minutes` | No | 1–120, default 30. On timeout, kills the entire process tree and replies with `status: timeout` |
| `base` | No | Full 40-character lowercase hex commit SHA for the starting point (short prefixes are rejected because anyone who can push to `origin` could brute-force a collision). On the sender side, `--base` accepts any ref and resolves it to a 40-char SHA in the current repo. If the receiver lacks that commit locally, it runs `git fetch origin` once and replies `refused` if still missing. When omitted, the receiver uses its current HEAD—which might be on another branch or carry unpushed commits—so any dispatch whose result will be merged back (Section 14) should specify `base` |

**Structured field in result replies**: dispatch replies (`kind=result`) include a structured `result` object for programmatic consumption without parsing Markdown. It is covered by the message signature:

```json
"result": {"status": "ok", "runtime": "hermes", "branch": "relay/<id>", "commit": "<40-char sha>",
           "base": "<40-char sha>", "gate": {"status": "ok", "logs": [{"phase": "green", "exit": 0, "sha256": "…"}]},
           "usage": [{"attempt": 1, "model": "…", "total_tokens": 0}]}
```

`commit` is present only when relay actually created a commit. Refused or error replies contain only `{"status": …}`.

**Receiver policy (`dispatch` in `config.json`, deny-all if unset)**:

```json
"dispatch": {
  "runtimes": ["claude", "codex"],
  "repo_roots": ["C:/GitHub"],
  "senders": ["commander"],
  "cortex_path": "C:/GitHub/agent-cortex",
  "executables": {"claude": "%USERPROFILE%/.local/bin/claude.exe"}
}
```

- Any of the following conditions aborts execution and sends back a `status: refused` reply explaining why:
  - `runtime` is not in `runtimes`;
  - `repo` is not under `repo_roots`;
  - sender is not in `senders`. **If `senders` is unset or empty, all dispatches are refused**;
  - message lacks a locally trusted signature (Section 13; `require_signature` defaults to `true`).
- **Only real `.exe` binaries are executed**: checks `executables` first, then `<runtime>.exe` on `PATH`. `.bat` / `.cmd` wrappers are rejected because `cmd.exe` re-parses arguments and drops everything after the first newline in a prompt along with permission flags, causing the worker to run with the user's default permissions (observed in practice).
- Prompts for `claude` and `codex` are passed via **stdin**, never on the command line (avoiding the 32K length limit and newline truncation).
- **Workers only edit files; they do not commit**: `claude` workers have **no shell access by default** (`claude` has no native shell sandbox on Windows; allowing "run tests" would let a message write a `conftest.py` and execute arbitrary code unsandboxed, and `git diff --output=` could write outside the worktree). `codex` workers run inside their own `workspace-write` sandbox, where they can run checks but cannot write to `.git`. Neither can modify git's own metadata directory.
- **Relay commits on the worker's behalf**: after the worker exits, relay runs `git add -A` and `git commit -m "relay: <id>"` in the worktree with hooks disabled (`core.hooksPath` pointed at the null device, `--no-verify`, `core.fsmonitor=false`). If the worker modified the worktree's `.git` pointer file (which could redirect to attacker-controlled hooks), relay refuses to commit and notes this in the reply.
- A receiver can opt in to `claude_allowed_tools` to allow specific commands for a `claude` worker (e.g., `["Bash(python -m unittest *)"]`), **which is equivalent to allowing messages to execute unsandboxed code on your machine**—enable only when every trusted sender is fully trusted.
- Verification responsibility ultimately rests with the commander: the worker's report states which verification command to run, and the commander runs it before merging.
- Workers only modify the `relay/<id>` branch inside the worktree—**they do not push or touch the main working tree**. The commander decides whether to merge after inspecting the result.
- Command lines are assembled by the [agent-cortex](https://github.com/SanHsien/agent-cortex) launcher (when `cortex_path` is set or importable), inheriting its hardened permission modes: `claude` uses `acceptEdits` (`plan` in plan mode), `codex` uses `workspace-write` sandbox (`read-only` in plan mode); the `agy` worker is disabled by policy. If `agent-cortex` is unavailable, an equally conservative built-in builder is used. **Never** uses `bypassPermissions` or `--dangerously-*` flags.
- Reply body contents: status and exit code, runtime, worktree and branch, elapsed time, worker final report, commit list, and `diff --stat`. Common token patterns (`sk-`, `ghp_`, `AKIA`, `Bearer`, etc.) are redacted to `[REDACTED]` before entering git history.
- **Every dispatch is guaranteed to send one reply and ack**: `ok`, `failed`, `timeout`, `refused`, `error` (unexpected exception), or `interrupted`.
- **Crash recovery**: at launch, relay writes `~/.agent-relay/runs/<id>/state.json` (with deadline = timeout + 10 minutes). If the terminal tab is closed, crashes, or the machine reboots, the message stays in `cur/`; on the next `watch` cycle after the deadline passes without a reply, relay sends a `status: interrupted` reply, acks the message, and syncs immediately. Messages whose doorbell rang but never started (`state.json` missing after 30 minutes) are similarly recovered when the dispatch doorbell is enabled. An error while recovering one message is logged and skipped so it never halts `watch`.
- Messages already in `done` are not dispatched again; pass `relay dispatch <id> --force` to re-run. When a worktree path already exists, relay reuses it only if git lists it as the worktree for branch `relay/<id>` and computes diffs against that branch's merge-base; if the path is any other directory, relay refuses.
- Known boundary: worktrees share the repository's git object store, but workers lack write access to `.git` and all commits are performed by relay.
- **claude-3p**: runs the same Claude Code CLI routed through the Claude Desktop 3P gateway (e.g., local opencodex at `127.0.0.1:10100`). Permissions, stdin prompt delivery, and no-shell rules are identical to `claude`.
  - The gateway URL and API key are read **at execution time** from the 3P configuration: by default, the `opencodex` profile (or the currently active profile) in `%LOCALAPPDATA%\Claude-3p\configLibrary\_meta.json`, or the profile named in `dispatch.claude_3p.profile`.
  - The resolved values are injected into the worker environment as `ANTHROPIC_BASE_URL` / `ANTHROPIC_API_KEY`, while `ANTHROPIC_AUTH_TOKEN` is removed so official login credentials are not used. Keys are never written to relay config, messages, argv, or logs.
  - A model must be specified via the message's `model` field or the receiver's `dispatch.claude_3p.model`. The model ID is validated against the gateway-resolved target, not just the display alias.
  - **Non-Anthropic models only**: `claude-3p` exists to use non-Anthropic subscriptions exposed through the gateway, such as OpenAI `gpt-6.1-sol` or Google `google-antigravity/gemini-3.8-flash`. The following three categories are always `refused`:
    - Claude models (`claude-*`, `anthropic/*`): running them through the gateway consumes official quota; use `--run claude` instead;
    - Disallowed providers: `kimi`, `minimax`, `qwen`, `deepseek`, `glm`, etc.;
    - Router-selected aliases: `combo/*`, `*/auto*`, which might route to Claude.

    `setup-local` sets the default `claude-3p` model to `gpt-6.1-sol`.
  - If the 3P config cannot be read, relay replies `refused`. `runtime: auto`, quota fallback, and escalation never pick `claude-3p` because its quota cannot be queried locally; it must be requested explicitly with `--run claude-3p`.
- **Fabricated tool calls**: if a worker's output contains plain-text tool-call markup such as `<function_calls>` or `<invoke name=`, it may have hallucinated file contents without actually calling a tool (observed once with `claude-3p`). Relay attaches a warning in the reply and sets `result.warnings` to `["fake-tool-calls"]`; read-only prompts also instruct the worker to read real files with tools first and cite file paths and line numbers.
- **Tools for read-only (`plan`) claude workers**: passes `--tools Read,Glob,Grep`. Previously, `agent-cortex`'s read-only builder passed `--tools ""`, disabling every tool so the worker could not read any files; write operations remain blocked by `plan` permission mode.
- **Isolation for claude workers** (applies to both `claude` and `claude-3p`):
  - Passes `--strict-mcp-config --mcp-config <empty-list>` so workers cannot access the user's MCP servers (workers were previously observed attempting to call GitHub, web-scraping, and search MCP tools).
  - Passes `--setting-sources project,local` so user-level hooks and plugins are not loaded.
  - `claude-3p` additionally passes `--bare` to skip loading `CLAUDE.md` and auto-memory. Official `claude` cannot use `--bare` because it relies on OAuth login, which `--bare` disables; therefore official `claude` workers still load global `CLAUDE.md`.
- **hermes** (Hermes Windows desktop CLI): runs `hermes.exe -z <short-instruction> --in <worktree> -t file --usage-file runs/<id>/hermes-usage.json`, optionally with `-m` and `--reasoning`.
  - Single-shot `-z` mode **automatically skips interactive approval**, so safety relies on two controls: the toolset is restricted to `file` (`read_file` / `write_file` / `patch` / `search_files`, with no terminal or code execution), and `HERMES_WRITE_SAFE_ROOT` is set to the worktree so writes outside the worktree are rejected by Hermes. In `plan` mode, the write root is pointed at an empty directory under the run folder, making the worktree read-only.
  - **Reads are not directory-scoped**: `HERMES_WRITE_SAFE_ROOT` only restricts writes, while `read_file` / `search_files` only block Hermes's own credentials and `.env` files. A worker can still read `~/.ssh`, other repositories, and the relay store; under prompt injection, it could copy read data into the worktree where relay would commit it into git history. Codex's `workspace-write` has a similar read boundary. Only allow `hermes` for trusted senders.
  - Exiting 0 with `completed: false` in the usage file (iteration limit reached or interrupted) is treated as `failed`. Old usage files are deleted before each attempt; the reply lists usage for every Hermes attempt, preserved even if later escalated to another runtime.
  - `hermes` is never selected by `runtime: auto`, quota fallback, or `dispatch.escalation` (those only select `claude` / `codex`, and Hermes quota cannot be queried locally). Request it explicitly with `--run hermes`.
  - Never uses `--yolo` and does not go through `agent-cortex`.
  - Writes the full prompt to `runs/<id>/prompt.md` and passes only a short "read this file" instruction on the command line.
  - Attaches `--usage-file` metrics (model, tokens, `api_calls`, estimated cost) in the reply.
  - Because Hermes has no shell, like `claude` workers it cannot run tests itself; verification is handled by the verification gate.
- **Pinning the codex sandbox to the worktree**: a user's `~/.codex/config.toml` might widen `workspace-write` via `sandbox_workspace_write.writable_roots` (or `%TEMP%`), which in testing allowed a worker to write into the main repo's `.git/hooks`. Relay always passes `-c sandbox_workspace_write.writable_roots=[]`, `exclude_tmpdir_env_var=true`, and `exclude_slash_tmp=true`. The exact command line (excluding the prompt) for each attempt is saved to `runs/<id>/argv.json` (with previous attempts kept as `argv.N.json`) and echoed in the reply. Also sets `network_access=false`. Prerequisite: file-write isolation on Windows relies on Codex's Windows sandbox, tested with `[windows] sandbox = "elevated"` in `~/.codex/config.toml`; on any new receiver machine, verify with a negative test task first to confirm out-of-tree writes are blocked.
- Note when running `codex` through an OpenCodex proxy: invoking `codex.exe` directly bypasses `ocx.exe ensure` inside the `codex.CMD` wrapper. If the proxy is not already running, the worker will fail (`failed`), so ensure the proxy daemon is active.
- **`agy` Worker Disabled by Policy**: Per maintainer policy dated 2026-10-27, third-party agents must not use Antigravity personal subscriptions via `agy -p`. If a task specifies `--run agy` or the receiver configures `dispatch.default` as `agy`, relay strictly refuses the run (replying with `status: refused` citing the policy). Dispatching to Antigravity is done via `python -m agent_relay send --to antigravity --kind task --subject <subject> --project <repo> --ref antigravity-session:new` instead (where the sidecar uses `agentapi` to open a new conversation in Desktop App running under its own login; see Section 20 and [docs/antigravity-app-mailbox.en.md](docs/antigravity-app-mailbox.en.md)).

**Doorbell**: add `{"type": "dispatch"}` to `doorbells`. It opens a new Windows Terminal tab running `relay dispatch <id>` (using console `python.exe` and passing relay `--home` and `--store` as explicit arguments rather than relying on environment variables) so you can watch progress live; if `wt.exe` is unavailable or `"window": false` is set, it spawns a detached background process instead. Dispatch runs outside the scheduled task process because scheduled runs have a 10-minute ceiling.

## 8. Hand-over Bundle (`context`)

New sessions do not need manual context pasting: `relay context --repo <repo>` compiles the sources below into a size-bounded Markdown bundle and feeds it automatically into a new session.

```text
<repo>/HANDOFF.md                        Handoff sheet (stage-1 output of cc-session-reader / agent-handoff)
<vault>/handoffs/<repo-folder-name>.md   Obsidian handoff note (claude-obsidian vault; requires context.obsidian_vault)
<vault>/wiki/hot.md                      claude-obsidian hot cache (shared across vault; requires context.hot_cache: true)
<repo>/.claude/handoffs/*.md             status: pending dropoff/pickup handoff cards (titles only)
Previous session                         cc-session list -p <exact-project-key> for the latest session of this exact repo, last 60 lines
Git status                               Current branch, count of tracked modified files, last 5 commits
```

Two injection points:

1. **Dispatch**: `relay dispatch` appends the target repository's hand-over bundle to the worker prompt (disable with `dispatch.context: false`). **Previous session transcripts are omitted by default** (they may contain tool outputs, and dispatch replies enter git history); opt in with `dispatch.context_sessions: true`.
2. **Any interactive session**: Claude Code `SessionStart` hook. Add to settings (user, project, or `--settings` file):

   ```json
   {"hooks": {"SessionStart": [{"matcher": "startup|clear",
     "hooks": [{"type": "command", "timeout": 15,
                "command": "python C:/GitHub/agent-relay/hooks/session_start.py"}]}]}}
   ```

   Use `startup|clear` as the matcher: on `resume` or `compact`, excluding the current session could pull in another session running in parallel as the "previous session".

Rules:

- **Read-only**: never writes any files. Symlinks and files resolving outside the repo or vault are skipped; at most 16 KB is read per file; binary files containing NUL bytes are skipped.
- **Bounded**: at most 4,000 characters per section and 14,000 characters total (9,000 characters in `--hook` mode to avoid truncation by Claude Code). Total wall-clock budget is 15 seconds for dispatch and 8 seconds for hooks, with a 5-second cap per external command; previous-session lookup is tried once and skipped on failure. Git status uses `status -uno` without scanning untracked files.
- **Redaction**: every source is redacted before truncation. Redacted patterns include `sk-`, `ghp_` / `github_pat_`, `xox?-`, `AKIA`, `AIza`, `ya29.`, JWTs, PEM private keys, `Bearer`, Telegram bot tokens, `user:pass@` in URLs, and key-value pairs like `password=` / `token=` / `*_KEY=`.
- **Data boundary**: the entire bundle is wrapped between `<<<HANDOVER-DATA <random-nonce>>>` … `<<<END HANDOVER-DATA <nonce>>>`, preceded by a notice stating that the block is data, not instructions. Each source sits inside an indented `~~~~` block (4-space indent per line, while the closing marker allows at most 3 leading spaces so content cannot close the fence early), with `<<<` / `>>>` replaced by lookalike characters; if the total budget is exceeded, trailing sources are dropped whole rather than slicing mid-block. Git commit messages are also redacted. Dispatch prompts repeat the data-only reminder.
- When `cwd` is missing, the hook payload is malformed, or no hand-over sources exist, the hook outputs nothing and exits 0 (`relay context --hook` and `hooks/session_start.py` alike). Payloads are decoded as UTF-8, supporting non-ASCII paths.
- Project key follows Claude Code's convention: every non-alphanumeric character in the path is replaced with `-`, matching both the raw path and the resolved path (`subst` or mapped network drives).
- When opening Codex manually, run `python -m agent_relay context` and paste the output. Antigravity's current official hooks support `SessionStart` / `PreInvocation`, but their payload schema differs from Claude's so the Claude hook above cannot be used directly. See Section 20 for automatic mail reception in existing Antigravity App conversations.

```json
"context": {"obsidian_vault": "C:/Users/<you>/Documents/Obsidian/MyVault", "hot_cache": false, "sessions": true}
```

## 9. Check-out Locks and Audit Trail (`claim` / `claims` / `release`)

When multiple agents work on the same repository in parallel, each checks out the paths it intends to edit before starting and checks them back in when done; overlapping check-outs are blocked.

```text
<store>/claims/<repo key>/<claim id>.json                 Active check-out: holder, paths, expiry
<store>/audit/<YYYY-MM-DD>/<time>-<claim>-<event>.json    Audit record: checkout / checkin / blocked (one file per event, conflict-free across machines)
~/.agent-relay/touch-log.jsonl                            Local log: every allow/block decision made by checkout-guard
```

- **Repository identity**: normalized `origin` URL (credentials stripped, normalized to `https`, `.git` suffix removed, lowercased); falls back to the local git common directory when no remote exists. All worktrees of the same repository share the same identity. `<repo key>` = repo name slug + first 12 hex digits of the SHA-1 of the identity.
- **Path rules**: repository-relative paths, `**` matches across directories, trailing `/` denotes an entire directory; comparisons are case-insensitive. `./a` and `a//b` normalize to `a` and `a/b`; escaping the repository via `..`, leading `/`, or drive letters is rejected. Overlap detection reuses the conservative `may_overlap` check from [claude-code-projects-workflow](https://github.com/SanHsien/claude-code-projects-workflow): two claims overlap if they could touch the same file (even one that does not exist yet).
- **Check-out**: locally wraps "check + write" inside `claims/<key>/.lock` (`O_EXCL`), preventing local race conditions. Across machines, claims sync via git and become visible after `pull`; if two machines check out simultaneously in the same sync window, conflict prediction (below) flags it in the reply.
- **Dispatch**: `dispatch.paths` in a task message defines the worker's allowed file scope. Relay acquires a claim before creating the worktree (`holder = relay:<message-id>`, expiry = timeout + 10 minutes); if it overlaps an existing claim, relay replies `status: refused` stating who holds which paths and until when. Setting `dispatch.require_paths: true` on the receiver requires every dispatch to declare `paths`.
- **Post-execution**:
  - If the worker modified any file outside its allowed paths → relay does not commit and replies `status: out-of-scope` listing those files. Changed files are enumerated with `--no-renames -z` and `core.quotePath=false` so non-ASCII filenames match verbatim and renames count as "delete old + add new" (catching a move from outside scope into scope). If the changed-file list cannot be read or the worker modified the worktree's `.git` pointer, relay treats it as out-of-scope and does not commit. Git inside the worktree always runs with hooks and fsmonitor disabled.
  - **Conflict prediction**: lists files modified on the base branch since `base` as well as files currently claimed by other holders so they can be addressed before merging.
  - Releases the claim regardless of outcome and writes an audit entry recording the files actually changed.
- **Interactive sessions**: `checkout-guard` (`PreToolUse`: `Edit`, `Write`, `MultiEdit`, `NotebookEdit`) in [harness-guard](https://github.com/SanHsien/harness-guard) reads claims from the local store and blocks edits to files claimed by someone else (exit code 2, naming the holder). "Self" is `AGENT_RELAY_HOLDER` (automatically set by relay for its spawned workers) or `manual:<config-agent>@<hostname>`, matching `relay claim`'s default. The hook locates the store using the same precedence as the CLI (`config.json`, overridden by `AGENT_RELAY_STORE` / `AGENT_RELAY_AGENT`, defaulting to `<home>/store`). Malformed or schema-less claim files are skipped so they never break the hook.
- **Known limitation**: two interactive sessions on the same machine share the default identity (`manual:<agent>@<host>`) and will not block each other unless given distinct `AGENT_RELAY_HOLDER` values. `checkout-guard` does not intercept file writes performed via `Bash`.
- **Manual check-out**: `relay claim --repo . --path calc.py --ttl 60`, `relay claims --repo .`, `relay release <claim id> --changed calc.py` (only releases your own claim unless `--force` is passed; the audit record logs who released it).

## 10. Quota-Aware Routing and Auto-Retry (`runtime: auto`)

When a task message specifies `--run auto`, the **receiver** selects the runtime, model, and reasoning effort based on task complexity and **its own local quota** (since execution consumes the receiver's accounts). Run `relay advise` to preview the decision and handoff card beforehand.

```text
Task (subject, body, paths, mode)
   │ Complexity: high (debug, root cause, race, architecture, refactor, security…) / low (typo, format, comment, docs…) / medium
   ▼
Quota ── claude: ~/.claude/agentdeck-status.json (written by agentdeck statusline; expired reset time = 0%)
   │     codex:  codex-reset-checker --json (SanHsien/codex-reset-checker; set advisor.codex_quota_command)
   │     agy:    no local source, treated as unknown (available)
   ▼
Routing table (aligned with agent-advisor)            Quota ceiling: skip if 5h ≥ 90% or weekly ≥ 95%
  high   claude/opus → codex(effort high) → claude/sonnet
  medium claude/sonnet → codex(effort medium)
  low    claude/haiku → codex(effort low)
  only picks from dispatch.runtimes
```

- `auto` cannot be combined with explicit `model` or `effort` (those are runtime-specific IDs chosen together by the receiver).
- Sender, repository, and path-scope policy checks run **before** quota lookup: unauthorized senders receive `refused` without exposing local quota state. If `dispatch.runtimes` contains no runtime capable of the requested mode (e.g., a build task when `dispatch.runtimes` lists neither `claude` nor `codex`), relay also replies `refused`.
- If every candidate runtime is over the quota threshold → relay does not execute and replies `status: deferred` with the earliest reset timestamp. Missing or malformed quota output is treated as "unknown (available)" and noted in the routing rationale.
- Keyword matching uses word boundaries for English terms and ignores file paths, filenames, and inline `code` spans; when a high-complexity keyword is present, low-complexity keywords never downgrade the task.
- The reply includes an `Auto Routing` section detailing the complexity signals, skipped runtimes and reasons, and the selected route.
- **Auto-retry on disconnect or rate limit**: when a worker fails, relay inspects the tail of its log:
  - Transient network or server errors (timeout, `ECONNRESET`, `502`/`503`/`504`, `overloaded`, etc.) → waits briefly and retries in place in the same worktree (following the [codex-auto-retry](https://github.com/SanHsien/codex-auto-retry) pattern).
  - Rate limits (`429`, `rate limit`, `usage limit`, `quota exceeded`, etc.) → re-routes to the next runtime in the routing table.
  - Other failures are not retried. Retry count is controlled by `dispatch.retries` (0–3, default 1); every attempt is listed in the reply's `Attempts` section, and prior logs are preserved as `output.<n>.log`.
  - Classification inspects only runtime-reported error channels: JSON event `is_error` results, `api_error_status`, Codex `error` / `turn.failed` events, plus the last few non-JSON stderr lines. Normal worker output (tool outputs or prose mentioning `429` or `rate_limiter.py`) is ignored.
  - A runtime that hit a rate limit is never selected again during the same dispatch.
  - **Before each retry attempt and before running verification gates, relay refreshes the lease and extends the claim expiry**; the reaper also checks whether the dispatch process is still alive before marking a task `interrupted`.
- **Handoff card**: `relay advise --card` prints an Obsidian/Markdown card with frontmatter (`status`, `complexity`, `runtime`, `model`, `effort`, `tags`) plus assignee, scope, task description, acceptance criteria, fallback route, and quota snapshot at dispatch time.

```json
"advisor": {"codex_quota_command": ["node", "C:/GitHub/codex-reset-checker/bin/codex-reset-checker.js"],
            "claude_status_file": "C:/Users/<you>/.claude/agentdeck-status.json"}
```

Out of scope: account switching via Antigravity-Manager (account mutation is never performed automatically by relay) and local quota lookup for `agy`.

## 11. Verification Gates (Mandatory TDD, No Stubs, Real Execution Logs)

After the worker finishes and passes the path-scope check, relay runs the verification gates on the receiver; if any gate fails, relay does not commit and sets the reply status to the failing gate.

```text
Guard       Worker deleted a test file or modified test configuration (conftest.py, pyproject.toml, setup.cfg, tox.ini, pytest.ini,
            loop-policy.toml, package.json…) → guarded (override with verify.allow_test_deletion / allow_config_changes)
Stub check  Any non-test .py function **added or modified** by the worker whose body is only pass / ... / return None / raise NotImplementedError → stub-rejected
            (pre-existing no-ops in base are ignored; @abstractmethod, @overload, and Protocol members are ignored; unannotated methods on ABC subclasses still count)
TDD         When dispatch.tdd: true or verify.tdd: true, opens a temporary worktree at the base commit:
              run verify command on clean base → must pass (proves base was green, else tdd-not-proven)
              copy in worker's added/modified test files and run again → must fail (proves the test reproduces the issue, else tdd-not-proven)
Green       Run verify command in the worker's worktree → if it fails, verification-failed
Logs        Full terminal output of each phase is written to runs/<id>/verify-base.log, verify-red.log, verify-green.log;
            on timeout, kills the entire process tree (including grandchildren); reply attaches command, exit code, last 15 lines, and log sha256;
            store/audit records a verify event (without raw output)
```

Receiver configuration (messages **can only request** TDD; they can never supply the command to execute):

```json
"dispatch": {
  "verify": {"command": ["python", "-m", "unittest"], "tdd": false, "no_stubs": true, "timeout_minutes": 10},
  "require_verification": true
}
```

- `command` can be set to `"loop-policy"` to read `[verification].quick_gate` from the repository's own `loop-policy.toml` (the [ai-quality-gates](https://github.com/SanHsien/ai-quality-gates) format).
- `command` can also be set to `"auto"`, which checks in order:
  1. `loop-policy.toml`;
  2. `test_*.py` under `tests/`: `python -m unittest discover -s tests`;
  3. `package.json` with a real `test` script (not npm's default placeholder): `npm test --silent`;
  4. `go.mod`: `go test ./...`.

  If none match, no gate command is configured. Useful when one worker machine serves many repositories.
- `by_repo`: e.g., `{"by_repo": {"harness-guard": ["python", "tests/run-tests.py"]}}` overrides `command` by repository folder name.
- All three resolution modes read strictly from the **main working tree**, never from the worker's branch, so a worker cannot tamper with how it is verified.
- When `require_verification: true` is set (or the message requests TDD) and no verification command resolves, relay replies `unverified` without committing. Otherwise the gate is inactive when unconfigured.
- **Governance checks (agent-governance-toolkit, AGT)**: when verify sets `governance`, `no_credentials`, `blocked_paths`, `max_files`, `max_lines`, `scan_subprocesses` or `strict`, the path-boundary / credential-leak / dangerous-subprocess checks run. AGT is loaded from the `AGENT_GOVERNANCE_PATH` environment variable or the default path `C:/GitHub/agent-governance-toolkit`. If governance is requested but AGT cannot be loaded, the gate **fails closed**: status `unverified` (the checks did not run; this is not a violation), the report says "AGT 未載入，無法執行 XX 檢查" (AGT not loaded, cannot run the XX check), and the verification command does not run. Only an explicit `"governance_fallback": "builtin"` degrades to the simplified built-in checks (fnmatch path matching, `max_files`/`max_lines` limits, searching only `sk-`/`ghp_`/`AKIA`); the report is labelled "降級：內建簡化檢查" (degraded: built-in simplified checks), and the subprocess scan, which has no built-in substitute, is reported "未執行" (not run) and never counts as passed.
- Because the verification gate executes test code written by the worker, enable it only for trusted senders and repositories.
- When a verification gate (or TDD) is active and the worker does not exit cleanly (`failed` / `timeout`), relay does not commit partial work.
- **Known limitations**: the red phase checks only for a non-zero exit code (a test that imports a newly added module will also fail with `ImportError` on `base`); the temporary worktree has no gitignored files (`.venv`, `node_modules`, build artifacts), so commands that rely on local untracked directories will fail on `base` with `tdd-not-proven` (failing closed). The stub checker inspects Python ASTs only (not lambdas or other languages). Untracked gitignored files do not appear in the diff; stub comparison keys on `(function_name, body_shape)` across the file without class qualification (if class A in `base` already has `__init__: pass`, a new `__init__: pass` in class B in the same file will not be flagged). A worker could still game tests through unprotected helper files—gates catch "fixed without reproducing" and "claimed done without running tests", not deliberate sabotage; always review the diff before merging.
- Interactive sessions use the corresponding guardrails in [harness-guard](https://github.com/SanHsien/harness-guard): `claim-guard` (blocks completion claims without test evidence) and `test-gate-guard` (blocks commit/push when tests are red). The runtime policy engine in [agent-governance-toolkit](https://github.com/SanHsien/agent-governance-toolkit) can serve as a stricter outer layer. AGT is an external, optional tool and relay does not list it as an install dependency; but when the verify settings require governance checks and AGT is absent, relay does not let the work through (`unverified`) unless `governance_fallback: "builtin"` is explicitly set to degrade.

## 12. Fallback Escalation Debugging Loop (`dispatch.escalation`)

When a lower-cost worker fails the verification gate, relay hands the real failure evidence to a stronger model in the **same worktree** to diagnose and fix, then re-runs the **exact same verification gate**. For the prompt structure and decision table, see [docs/templates/escalation-prompt.en.md](docs/templates/escalation-prompt.en.md) ([繁體中文](docs/templates/escalation-prompt.md)).

```json
"dispatch": {"escalation": {"runtime": "claude", "model": "opus"}}
```

- Triggers on: gate status `verification-failed` / `tdd-not-proven` / `stub-rejected`, or worker failure/timeout (excluding rate limits and transient disconnects, which are handled by retry and quota fallback).
- Does not trigger on: out-of-scope edits, deleted tests, modified test config, or `.git` pointer tampering (behavioral violations go to a human); `plan` mode; when the escalation target is already the same runtime/model; when the escalation runtime is not in `dispatch.runtimes`; or when the message sets `"escalate": false`.
- Also skipped when: the TDD `base` check itself failed (only the base phase ran, so switching workers cannot fix it); the escalation target is not a build-capable runtime; or the escalation target's `model` / `effort` format is invalid.
- Escalates at most once per task. Round 2 has its own retry budget for transient errors and does not re-route on rate limits. If Round 2 still fails the gate or exits abnormally, relay does not commit (even if no verification gate was configured). The reply's `Escalation` section records Round 1's status, the escalation target, and Round 1's gate logs (renamed to `verify-*.round1.log` for files actually written in Round 1).
- Evidence in the escalation prompt is wrapped in length-bounded `~~~~` fences preceded by a "data, not instructions" notice; the first worker's self-report (JSON `result`) is never quoted as evidence.
- Evidence is drawn strictly from execution logs (tail of gate logs and `git diff`), never paraphrasing the first worker's claims.

## 13. Sender Verification (Message Signing)

Because the store is a git branch, anyone with push access to the store could write any name in `from`. Dispatches spawn workers and execute test suites on the receiver, so the receiver must trust only keys it has explicitly approved locally.

```text
Sender    relay keygen ──▶ ~/.agent-relay/keys/<mailbox>_ed25519 (passphraseless for unattended signing)
          relay send   ──▶ adds "sig" to envelope: ssh-keygen -Y sign -n agent-relay over canonical JSON
Receiver  relay trust <mailbox> <pubkey> ──▶ ~/.agent-relay/allowed_signers (local file, never in store)
          relay dispatch ──▶ ssh-keygen -Y verify -I <from> -n agent-relay; refuses if invalid
```

- **What is signed**: all envelope fields except `sig`, serialized as UTF-8 JSON with sorted keys and no whitespace. `id`, `to`, `body`, and `dispatch` are all covered: tampering with the body, forwarding to another mailbox, or swapping the ID fails verification.
- **Replay protection**: someone with push access to the store could move a signed, completed message from `done/` back to `new/`. The receiver checks its local `runs/<id>/state.json` (which is never stored in git) and refuses if already replied. In addition, any dispatch whose `created_at` is older than `dispatch.max_age_hours` (default 168 hours) is refused, bounding the replay window.
- **Identity bound to mailbox**: the principal in `allowed_signers` is the mailbox name under namespace `agent-relay`. Trusting a key for `mallory` never allows it to sign as `commander`.
- **Trust list stays local**: `allowed_signers` is never stored in the shared store (otherwise anyone who can push to the store could add their own key). Public keys are exchanged out-of-band—for example, by running `relay trust` on each machine.
- **Tooling**: uses OpenSSH's built-in `ssh-keygen` (ships with Windows 11, Linux, and macOS); no GPG required.
- **Receiver policy**: `dispatch.require_signature` (default `true`) and `dispatch.senders` (required). Disable signature verification only in isolated unit tests.
- **Configuration**: `signing.key` and `signing.allowed_signers` override the default paths (`~/.agent-relay/keys/<agent>_ed25519` and `~/.agent-relay/allowed_signers`). `trust` only accepts valid mailbox names because OpenSSH principals support wildcards and comma-separated lists (`*` would let one key sign for every mailbox).
- **Upgrading from pre-2026-10-21 configs**: after upgrading, a receiver without sender signing configured will refuse all dispatches until you:
  1. Add `"senders": [...]` under `dispatch` on the receiver;
  2. Run `relay keygen` on the sender;
  3. Run `relay trust <sender-mailbox> <pubkey>` on the receiver.

  Running `keygen` on the receiver and `trust` on the commander signs result replies as well.
- **Result replies are also signed**: when the receiver has a signing key, dispatch replies (including `interrupted`) are signed so `relay accept` on the commander (Section 14) can verify the reply genuinely came from the worker. If signing fails on the reply path, the unsigned reply is still sent and the error is logged so results are never dropped.
- **Known limitation**: private keys have no passphrase and rely on OS file permissions (`ssh-keygen` restricts access to the current user). An attacker who already has code execution under the receiver's OS user account is outside this threat model.

## 14. Result Branch Push and Commander Verification & Merge (`push_branch` + `relay accept`)

```text
Receiver   dispatch ok, gate passed, relay committed ──▶ push relay/<id> to repo origin (dispatch.push_branch)
                                                         reply result.pushed = {remote, ref, commit} (signed)
Commander  relay accept <reply-id> --repo <local-clone> [--into <branch>]
             ├─ Local preflight (failures exit with error without logging or notifying receiver): accept.verify.command is set,
             │  not detached HEAD, current branch matches --into, working tree is clean, commit not already merged
             ├─ Verify reply signature, status == ok, full 40-char commit/base SHAs, pushed points to relay/<original-task-id>
             ├─ fetch origin relay/<id>: FETCH_HEAD must equal reply commit (rejects branches force-pushed after replying)
             ├─ base must be an ancestor of commit
             ├─ Temporary worktree: git merge --no-ff <commit> into current HEAD, then run commander's own
             │  accept.verify.command on the **merged tree** (never reading config from the worker branch)
             └─ If green and HEAD has not moved, git merge --ff-only the main repo to that verified merge commit.
                Any other failure = rejected; records audit entry and sends a note back to receiver
```

**Receiver (pushing)**:

- Pushes only when `dispatch.push_branch: true`, the worker exited `ok`, the verification gate passed (or none was configured), and relay created a commit. Hooks are disabled before pushing.
- Pushes strictly to the receiver clone's `origin`; messages cannot specify a remote. Relay inspects `git remote get-url --push --all origin` (every URL git would actually push to, including `pushurl` and `pushInsteadOf`). Every URL must match one of the following, or relay refuses to push and explains why in the reply:
  - The maintainer's own GitHub repository, matching `https://github.com/SanHsien/<repo>`, `git@github.com:SanHsien/<repo>`, or `ssh://git@github.com/SanHsien/<repo>` (with optional `.git` suffix). Prefix matching alone is insufficient because `SanHsien/../other/x.git` would normalize to someone else's repository.
  - A remote explicitly listed in `dispatch.push_remotes` (exact path match for local paths).
- A push failure does not change the dispatch `status`; the reply's `Push` section and `push_error` explain the failure, and `result.pushed` is omitted.

**Commander (acceptance)**:

```json
"accept": {
  "verify": {"command": ["python", "-m", "unittest"], "timeout_minutes": 10},
  "require_signature": true
}
```

- Refuses to merge if `accept.verify.command` is unset—never merging unverified code. Like the receiver, `accept.verify` supports `"auto"`, `"loop-policy"`, and `by_repo` (resolved from the commander clone's main working tree, never from the incoming branch).
- Verifies the **merge result of the target branch + worker commit**, not just the worker commit in isolation, because the target branch may have advanced since dispatch. Verification runs in a temporary worktree and writes logs to `~/.agent-relay/accepts/<reply-id>/verify-*.log`, recording their SHA-256 hashes in the audit trail and in the notification note sent to the receiver.
- Merge commit message: `relay accept: relay/<id> (reply <reply-id>)`. Merge conflicts result in `rejected` without touching the main working tree.
- The main repository is updated strictly via `merge --ff-only` to the exact merge commit that passed verification. Hooks are disabled inside the temporary worktree during merge.
- Git timeouts or I/O errors during acceptance are recorded as `rejected` with the reason; a failure while writing the audit log or sending the notification note never masks a completed merge (noted in `notes`).
- Once finished, the reply message is acknowledged (`ack`) and the receiver gets a note prefixed with `accepted:` or `rejected:`.
- Known limitation: running the verification command executes code from the incoming branch (which is inherent to testing it); only accept replies signed by trusted workers.
- **Test topology note**: cross-machine behaviors in Sections 7–17 have been verified using two separate store/repo clones on the same physical host rather than two separate physical machines. Before running across two physical machines for the first time:
  - Ensure both machines synchronize their clocks via NTP (`max_age_hours`, heartbeat staleness, and lease deadlines compare timestamps against the local clock).
  - Verify each machine's independent git credentials and SSH keys.
  - Run one negative test task over the real network to confirm retry and refusal paths.

## 15. Receiver Concurrency / Daily Limits and Duplicate-Run Guard

```json
"dispatch": {"max_concurrent": 2, "queue_wait_minutes": 240, "daily_limit": 20}
```

- **Concurrency**: `max_concurrent` caps how many workers can run simultaneously on this machine. Each active slot is a file `runs/.slots/slot-<id>` storing the PID and expiry timestamp. A slot is reclaimed when any of the following holds:
  - The owner process is no longer alive;
  - The task has already replied (prevents PID reuse from keeping a dead slot);
  - The slot expiry has passed.

  Reclaiming stale slots, counting active slots, checking the daily limit, acquiring a slot, and recording the ledger entry all happen inside a single `runs/.limits.lock` (`O_EXCL`, held for milliseconds), so two tabs launched at the same instant cannot both grab the last slot or bypass the daily limit. When all slots are full, the dispatch queues inside its own tab with lease phase `queued`, polling every 15 seconds until a slot opens or `queue_wait_minutes` (default 240) expires (which replies `deferred`). Slots are released when the dispatch finishes, whether it succeeded or failed.
- **Daily limit**: `daily_limit` counts how many dispatches actually acquired a worker slot today (local date), recorded in `runs/.ledger/<date>.txt`. Once reached, new dispatches reply `deferred` without spawning a worker. Queued dispatches re-check the daily limit on each poll.
- **Crash while queued**: if a queued tab crashes, the next `watch` cycle recovers it with `status: interrupted` (Section 7). While alive, a queued tab refreshes its lease deadline every 15 seconds so it is never reaped prematurely. Limit checks run after sender and signature verification so spoofed messages cannot exhaust slots or daily quota.
- **At most one worker per task**: before starting, relay creates an ownership record `runs/<id>/.owner` inside the same lock and removes it only after sending the reply. While the owner process is alive, any second trigger is rejected—even with `--force` (including legacy leases in phase `running` / `queued` whose PID is still alive). The rejected duplicate neither replies nor acks, leaving the original worker to finish normally.
- Cost tracking: replies include per-attempt usage metrics when available (currently provided by `hermes`; `claude` and `codex` usage is tracked via their respective quota tools in SPEC §10).

## 16. Fleet Overview and Receiver Heartbeats (`relay status`)

```text
Receiver watch (each cycle) ──▶ store/hosts/<mailbox>@<host>.json: {at, version, running:[…], queued:[…]}
                                rewritten and pushed only when older than status.heartbeat_minutes (default 30) or when running/queued changes
Commander relay status      ──▶ unread/read counts per mailbox, minutes since each receiver's last heartbeat, local unfinished dispatches
```

- Any heartbeat older than `--stale-minutes` (default 90 minutes) is flagged as `stale`. Common causes: the receiver's scheduled task stopped (e.g., user logged out, since the task uses `InteractiveToken`) or the machine is powered off.
- Local dispatches: lists tasks in `runs/*/state.json` with phase `running` or `queued`, along with their deadline and whether the PID is alive. Expired tasks are flagged as `overdue` and will be recovered as `interrupted` on the next `watch` cycle (Section 7).
- Exit code: returns `8` when any heartbeat is stale or any local dispatch is overdue (for easy scripting/monitoring), and `0` when everything is healthy.
- Like `mailboxes/`, `claims/`, and `audit/`, `hosts/` is committed automatically by relay.

## 17. Garbage Collection (`relay gc`)

```text
relay gc [--older-than 14] [--repo <repo> ...] [--apply]
```

Defaults to dry-run mode (listing what would be deleted); pass `--apply` to actually delete. Only processes dispatches that **replied more than N days ago**, determined by `phase == "replied"` and the `finished` timestamp in local `runs/<id>/state.json`. Never touches `running` / `queued` dispatches, `new` / `cur` messages, or worktrees without a matching local run record.

| Item | Condition |
| --- | --- |
| Worktree `<repo>/.relay-worktrees/<id>` | Replied more than N days ago. Repositories are discovered from `--repo` and `dispatch.repo_roots`. Worktrees with uncommitted files are kept unless their commit was pushed and accepted, or `--force` is passed |
| Local branch `relay/<id>` | Same age check; removes the worktree first, then the branch. Branches whose commit exists on `origin` are deleted directly; unpushed branches use `git branch -d` and are kept if unmerged unless `--force` is passed |
| Remote branch `relay/<id>` on `origin` | Same age check, must have been **pushed by this machine** (`pushed_ref` and `repo` recorded in `state.json`), and must have received an `accepted:` or `rejected:` note from the commander. Unaccepted remote branches are kept. Independent of whether the local branch still exists |
| `runs/<id>/` | Same age check |
| Messages in own mailbox `done/` | `created_at` older than N days. Deletions are committed and synced by relay (git history retains them). If a task's local run record is still kept, its `accepted:` / `rejected:` note is also kept so the remote branch can be cleaned up later |
| `accepts/<reply-id>/` (commander acceptance logs) | Last modified more than N days ago |

- `N` must be positive and cannot be shorter than `dispatch.max_age_hours` (default 7 days). If `max_age_hours` is non-positive, `gc` refuses to run because old signed messages would remain valid forever and only local run records prevent replay (Section 13).
- Kept items are reported as `kept: <git reason>` and do not count as failures.
- Git operations run with hooks disabled. A failure on one item is recorded on that item without aborting the rest; if any item fails, `gc` exits with code `9`.

## 18. Unattended Mail Handling

All three switches are opt-in and enforce every security check from the preceding sections:

```json
// Receiver config.json
"dispatch": {"default": {"runtime": "claude", "mode": "build", "timeout_minutes": 30}}
// Commander config.json
"accept": {"auto": true, "repo_roots": ["C:\\GitHub"], "push": false, "window": false}
// Either side (default true)
"auto_ack_notes": true
```

- **Auto-dispatch for plain tasks** (`dispatch.default`): a `kind=task` message without `--run` inherits the receiver's default runtime, mode, timeout, and TDD settings, executing just as if the sender had passed `--run`.
  - The target repository comes from the message's `project` field (`send --project <repo>`), falling back to `dispatch.default.repo`; if neither is set, relay replies `refused` and prompts the sender to pass `--project`.
  - Sender allowlist, signature verification, `repo_roots`, concurrency/daily limits, and verification gates all apply per Sections 7, 9, 11, 13, and 15.
  - When `dispatch.default` is not set on the receiver, plain tasks only ring doorbells and wait for human handling.
- **Commander auto-acceptance** (`accept.auto`): when a `status: ok` reply with a pushed branch arrives, `watch` launches `relay accept` in the background following Section 14, merging only if verification passes.
  - Local clone resolution: takes the repository folder name from the original task message and looks for a matching git repository under `accept.repo_roots`; if not found, skips and logs.
  - If local preflight fails (dirty working tree or wrong branch), `accept` leaves no audit record and keeps the reply unacknowledged in `cur/` (visible in `relay status`) so you can run `relay accept` manually.
  - `accept.push: true`: pushes the target branch to `origin` after merging (equivalent to `relay accept --push`).
    - Before pushing, validates every push URL of `origin` using the exact same rules as Section 14: only pushes to `SanHsien/<repo>` on GitHub or a remote listed in `accept.push_remotes`; if any URL fails validation, skips pushing while keeping the local merge and records the reason in `notes`.
    - Git hooks run normally (this is the commander's own clone). A push failure does not roll back the completed merge.
  - `accept.window: true`: runs in a Windows Terminal tab; defaults to a background process.
- **Auto-acknowledge notification notes** (`auto_ack_notes`): `accepted:` / `rejected:` notes sent by `relay accept` are informational only; `watch` automatically acknowledges them after ringing doorbells.

## 19. One-Command Local Setup and Recovery (`relay setup-local`)

```powershell
python -m agent_relay init --agent commander --clone <store repo> --branch mailbox   # Run once on a new machine
python -m agent_relay setup-local [--auto] [--repo-root C:\GitHub] [--runtime claude --runtime codex ...]
```

Creates or repairs the following items in order. Safe to run repeatedly—each step only fills in what is missing:

1. Commander signing keypair;
2. Worker home (default `~/.agent-relay-worker`, mailbox `claude-code`) and its `config.json`;
3. Worker signing keypair;
4. Mutual trust (`allowed_signers`) between commander and worker;
5. Commander `accept` defaults;
6. Both Windows Scheduled Tasks.

- **Non-destructive**: only populates missing config keys, preserving any values you customized by hand; reuses existing keys; avoids duplicate lines in `allowed_signers`; re-registers scheduled tasks under the same names.
- `--auto` also enables `dispatch.default` and `accept.auto` from Section 18.
- **Backup strategy**:
  - Config files and scheduled tasks: reconstructed by this command and your flags, so no separate backup is needed (record any hand-edited overrides in your own notes).
  - Private keys: **never backed up and never committed to git**. They do not encrypt data—they only prove which local installation sent a message. If lost or migrating to a new machine, re-running `setup-local` generates fresh keys and re-establishes trust. Because messages signed by an old key remain valid within `max_age_hours` (Section 13), if you ever suspect a private key was exposed, remove its line from `allowed_signers` first and then re-run `setup-local`.

## 20. Receiving Mail in an Existing Antigravity App Conversation

See [docs/antigravity-app-mailbox.en.md](docs/antigravity-app-mailbox.en.md) ([繁體中文](docs/antigravity-app-mailbox.md)) for full setup and troubleshooting instructions. This path uses the official App sidecar and `agentapi` rather than resuming an App conversation via the `agy` CLI.

- `python -m agent_relay.antigravity --home <receiver-home>`: syncs the store every 300 seconds and notifies at most one unread `task` / `reply` per cycle; `--once` runs a single cycle, and `--interval` has a 30-second minimum.
- The receiver home must configure mailbox `antigravity`, `antigravity.conversation_id`, `repos`, `senders`, and `agentapi_argv`, along with locally trusted public keys per SPEC §13. `repos` is a non-empty list of existing directories, allowing the bound conversation to accept tasks for any repository in the list; the legacy single-string `repo` key remains supported as a one-element list, while specifying both keys or any non-existent/non-directory path raises `ValueError`. Only messages with a valid signature, an allowed sender, within the age limit (default 24 hours), and matching the bound session ref or project (`project` must be in `repos`; for messages with a session ref and no `project`, scope is defined by the message) are accepted; messages carrying `dispatch` do not use this path. The fixed notification text cites the message's own `project` path (or states that scope is defined by the message when `project` is omitted) without listing all configured repos. When multiple repos share a conversation and checkout, signatures and notifications do not replace file-scope isolation—use dedicated worktrees for parallel work.
- An explicit `antigravity-session:<uuid>` accepts only two kinds of UUID: the `conversation_id` bound in the config, or a conversation this sidecar opened itself, proven by a local `app-delivery/*.json` receipt (`mode` is `new-conversation`, `status` is `notified`, `conversation_id` is that UUID); the latter is delivered with `agentapi send-message <uuid>`. Any other UUID, including one whose receipt is `failed` / `refused` / `uncertain` / `attempting`, is skipped and writes no receipt; the UUID must be in lowercase hyphenated form and a message may carry only one session ref. `kind` is limited to `task` / `reply`; signature, sender, age, `project` allowlist, `dispatch` exclusion, and receipt de-duplication are all unchanged. The receipt of such a follow-up records `mode: follow-up` and is never used as authorization. The sender does not derive the UUID from the original message (receipts are receiver-local state, not part of the shared store); it passes `--ref` explicitly.
- Notifications contain only the message ID and fixed read/reply instructions, never executing message text. The conversation's own model, hooks, permissions, and human confirmation rules remain in full effect.
- Exclusively creates an `attempting` receipt in `app-delivery/<id>.json` before calling the API, updating it to `notified` on exit 0, `failed` on non-zero exit, or `uncertain` on timeout. Messages that fail signature verification write a `refused` receipt (with reason) and print the refusal once, skipping silently on later cycles; messages skipped by policy (sender, session, project, or age mismatch) write no receipt and print nothing. No attempted message is ever auto-resent, preventing duplicate execution on uncertain delivery. Notification does not mark a message read: the bound conversation itself runs `read` / `result` / `ack`.
- At the end of each cycle (whether notifying or idle), the sidecar writes the `hosts/antigravity@<host>.json` heartbeat per §16 so `relay status` displays it; it writes and pushes only when due, and any heartbeat/push error is recorded in `heartbeat_error` without affecting a delivered `notified` status. At startup, the sidecar sets `AGENT_RELAY_HOME` to its own `--home` (which has no `runs/` directory), so `running` / `queued` are always empty and never impersonate dispatches from the main worker home.
- Requires no new Windows Scheduled Task; the App must be running. Disabling the sidecar leaves the mailbox and delivery receipts intact.
- Current official Antigravity hooks support `SessionStart` and `PreInvocation` with a different schema from Claude; this sidecar integration requires no changes to existing hooks.
