English | [繁體中文](antigravity-app-mailbox.md)

# Antigravity App: Automatic Mail Reception for Existing Conversations

This guide describes how to connect an **existing Antigravity App conversation** to `agent-relay`. It uses the official App sidecar and `agentapi send-message` rather than passing an App conversation ID to `agy --conversation`: the CLI and the App use separate data directories, so the CLI may return `trajectory not found`.

```text
commander send -> private store / antigravity / new
                                      |
App sidecar (pulls every 5 min) <-----+
    |
    +-> verify sender, signature, expiry, and target conversation/project
    +-> agentapi send-message -> bound existing App conversation
                                 read -> build / report blocker
                                 result -> commander
                                 ack -> done
```

## Official References and Scope Boundaries

- [Sidecars](https://antigravity.google/docs/sidecars): The App manages background process lifecycles and provides `agentapi send-message <conversation_id> <prompt>`. A sidecar is defined in `~/.gemini/config/sidecars/<id>/sidecar.json` and enabled via `sidecars.<id>.enabled` in `~/.gemini/config/config.json`.
- [Hooks](https://antigravity.google/docs/hooks): Current App / CLI / IDE builds support `SessionStart` and `PreInvocation`, using `conversationId`, `workspacePaths`, and `injectSteps` rather than Claude's `cwd` / `hookSpecificOutput`. Do not copy Claude hooks verbatim, and do not rewrite existing hooks into another format.
- This integration does not add or modify any hooks. Existing guards, disable flags, and worker permissions remain unchanged.
- The App must be running for the sidecar to execute. It is not a Windows service that wakes the App from a cold shutdown.
- `relay send --run agy` is a separate path: it spawns a **new** CLI worker using `plan + sandbox` per SPEC §7. It does not apply to receiving mail in an existing App conversation.
- When already running inside an Antigravity App session and you want to **create a user-visible top-level conversation (`nestingDepth: 0`) in the Antigravity Desktop left sidebar** for dispatch, do not use `invoke_subagent` (which creates a `nestingDepth: 1` sub-panel conversation) and do not call `agentapi.bat` (to avoid `cmd.exe` truncating newlines); instead, invoke `<App language_server.exe> agentapi new-conversation [--model=<flash_lite|flash|pro>] --title="<Title>" "<prompt>"` directly (use `--model=flash` for Gemini 3.8 Flash which maps to `gemini-3.8-flash-tiered`; do not append `-high`), extract `conversationId` from the returned JSON to include a `[<Title>](conversation://<id>)` link, and have the new conversation report back to the parent conversation via `send_message` upon completion.
- An existing conversation reuses its own working directory and does not automatically create an isolated worktree. When multiple agents share a checkout, one agent could inadvertently commit another's uncommitted files; for parallel work, create a dedicated worktree for the receiving conversation first and bind `repos` to that directory. Notifications and signatures are not substitutes for file-scope isolation.

## 1. Create an Independent Receiver Home

The examples below assume `C:\GitHub\agent-relay` as the installation path; replace all JSON paths with the actual paths on your machine. Never commit private store URLs or private keys to public files.

Create `~/.agent-relay-antigravity/config.json` with `store` pointing to your existing local mailbox clone:

```json
{
  "agent": "antigravity",
  "store": "C:/path/to/local/mailbox-store",
  "antigravity": {
    "conversation_id": "00000000-0000-0000-0000-000000000000",
    "repos": ["C:/GitHub/target-repo", "C:/GitHub/other-repo"],
    "senders": ["commander"],
    "max_age_hours": 24,
    "agentapi_argv": ["C:/path/to/native/agentapi.exe"]
  }
}
```

`conversation_id` is pinned by the receiver; messages cannot arbitrarily pick a conversation (the one exception is a conversation the sidecar itself opened via `antigravity-session:new`, see Section 3). `repos` is a non-empty list where every path must be an existing directory, allowing the bound conversation to accept tasks for any repo in the list; the message's `--project` must be in the list, and the notification text cites that message's own project path (for messages that carry only a session ref and no project, the notification states that the scope is defined by the message). The legacy single-string `repo` key remains supported as a single-element `repos` list, while specifying both `repos` and `repo` raises an error. When multiple repos share a single conversation, the file-scope isolation note above still applies. An empty `senders` list disables notifications altogether. Key generation and trust setup follow SPEC §13:

```powershell
python -m agent_relay keygen --home "$HOME/.agent-relay-antigravity"
python -m agent_relay trust commander C:/path/to/commander_ed25519.pub --home "$HOME/.agent-relay-antigravity"
python -m agent_relay trust antigravity "$HOME/.agent-relay-antigravity/keys/antigravity_ed25519.pub"
```

The second argument to `trust` accepts a public-key file path. Confirm mutual trust between commander and receiver; never weaken the policy to allow unsigned messages.

On Windows, `agentapi` may be a `.bat` wrapper. If the wrapper on your machine invokes `<App language_server.exe> agentapi %*`, set `agentapi_argv` to `["full path to language_server.exe", "agentapi"]` so the native binary runs directly. **Inspect the actual wrapper on disk first rather than guessing paths**; never pass message text through `cmd.exe`.

## 2. Register the Sidecar

First run `python -m pip install -e C:\GitHub\agent-relay`. Then create `~/.gemini/config/sidecars/agent-relay/sidecar.json`:

```json
{
  "display_name": "Agent Relay mailbox",
  "description": "Receive signed relay tasks for an explicitly bound App conversation",
  "command": "C:/path/to/python.exe",
  "args": ["-u", "-m", "agent_relay.antigravity", "--home", "C:/path/to/.agent-relay-antigravity", "--interval", "300"],
  "restart_policy": "on-failure"
}
```

**Merge** the following field into `~/.gemini/config/config.json`, preserving existing settings and other sidecars (back up the file first):

```json
{"sidecars": {"agent-relay": {"enabled": true}}}
```

The App populates `ANTIGRAVITY_LS_ADDRESS` and the `agentapi` environment for the sidecar. Running it directly from an ordinary shell may return `ANTIGRAVITY_LS_ADDRESS is not set`, which does not mean the conversation is missing; do not guess endpoints or copy auth tokens by hand. If the App does not hot-reload the sidecar, restart the App when no other work is active and inspect the sidecar logs—never claim the integration is live merely because the config file exists.

Do not install a separate Windows Task Scheduler poller for this mailbox: the sidecar already syncs and notifies every five minutes, avoiding duplicate polling on the same mailbox.

## 3. Send a Task Message

```powershell
python -m agent_relay send --to antigravity --kind task --project C:/GitHub/target-repo --subject "Single verification task" --body-file task.md
```

You can also pass `--ref antigravity-session:<UUID>` to target the conversation bound on the receiver, or a conversation the sidecar itself opened (see "Sending follow-ups to a relay-opened conversation" below); if `--project` is also present, the project must match as well. Do not pass `--run`. When an existing message already carries the right session ref, reuse it directly—do not resend or mutate a signed envelope.
If an external agent (such as Claude Code, Codex, or Hermes) wants to **open a brand-new top-level visible conversation (`nestingDepth: 0`) in the Antigravity Desktop left sidebar** to execute the task, pass `--ref antigravity-session:new --project C:/GitHub/target-repo` (must be `kind=task` with `--project` in the `repos` allowlist): the sidecar invokes `agentapi new-conversation --title="[Relay] <message-id>"` to create a top-level conversation and records the returned `conversationId` in `app-delivery/<message-id>.json`.

Follow-up feedback on the same task can use `--kind reply --reply-to <original-id>` with the same session ref or project (for a conversation opened via `new`, pass that conversation's UUID, see the next section); it goes through the same signature, sender, and target-conversation checks. This is a follow-up message, not a duplicate task. `note` and `result` messages do not wake this receiver.

### Sending follow-ups to a relay-opened conversation

To stop, question, or correct a conversation that was opened with `--ref antigravity-session:new`, you do not need to open yet another conversation to relay the message: send a `reply` (or `task`) that names that conversation's UUID. Take the UUID from the `conversation_id` field of the receiver's `app-delivery/<original-id>.json`.

```powershell
python -m agent_relay send --to antigravity --kind reply --reply-to <original-id> --ref antigravity-session:<conversation-UUID> --subject "Stop: worktree under review, do not edit" --body-file stop.md
```

The sidecar then delivers with `agentapi send-message <UUID>`, provided that:

- The UUID equals the conversation bound in the config, **or** it appears in a local `app-delivery/*.json` receipt whose `mode` is `new-conversation` and whose `status` is `notified`. A UUID the relay never opened, or one whose receipt is `failed` / `refused` / `uncertain` / `attempting`, is skipped and writes no receipt.
- Every other check is unchanged: valid signature, `senders` allowlist, `max_age_hours` window, `--project` (when given) in `repos`, no `dispatch` section, and one notification per message. `kind` is limited to `task` and `reply`.
- The UUID must be in lowercase hyphenated form, and a message may carry only one session ref. The receipt of a follow-up records `mode: follow-up` and never authorizes any other UUID.

The sender does not derive the UUID from the original message automatically: receipts live only on the receiver's machine, not in the shared store, so the sender passes `--ref` explicitly.

The notification contains only the message ID and fixed inbox instructions—never putting the subject, body, or external commands into process arguments. Sending the notification does not mark the message read or acknowledged; the receiving conversation performs those steps itself and must attach concrete evidence in its reply. A valid signature proves sender identity, not that the requested actions are safe; the conversation must still obey its existing permissions, scope, and human confirmation rules.

## 4. Minimal Acceptance Checklist and Troubleshooting

Use a single test task and stop once the checklist below is satisfied:

1. `~/.gemini/antigravity/sidecar_data/agent-relay/logs/` shows the message ID with `status: notified`. This proves only that the API accepted the notification.
2. **The actual transcript of the bound conversation** shows the inbox read operation, and the message moves from `new` to `cur`. That is the evidence of hand-over.
3. The same thread receives a `kind=result` reply with verification output, and the original message moves to `done`. If the work fails, the reply must still report the cause—never treat "notification succeeded" as "work completed".

Local delivery receipts are stored in `~/.agent-relay-antigravity/app-delivery/<message-id>.json`:

- `notified`: the notification command exited 0; the same message will not be notified again. A follow-up to a relay-opened conversation additionally records `mode: follow-up`.
- `failed`: the command exited non-zero; no automatic retry—inspect the App sidecar error logs first.
- `refused`: signature verification failed, with the reason recorded in the receipt; the refusal is printed once and silently skipped on subsequent cycles.
- `uncertain` / `attempting`: timed out or interrupted mid-flight; delivery may already have succeeded. Check the target conversation first and **do not blindly resend**.

The sidecar creates an exclusive `attempting` receipt before calling the API so two processes cannot notify the same message concurrently. This errs on the side of preventing duplicate execution rather than guaranteeing exactly-once delivery. Only after confirming that the notification never arrived and fixing the underlying cause should the operator back up and remove that receipt so the next cycle can re-notify; do not resend the task message. When scheduled polling is healthy, do not repeatedly run manual smoke tests or create extra test messages.

Single-cycle diagnostic command (must run inside the App sidecar environment and will send a real notification):

```powershell
python -m agent_relay.antigravity --home "$HOME/.agent-relay-antigravity" --once
```

Each cycle also writes and pushes the `hosts/antigravity@<host>.json` heartbeat (at the interval defined in SPEC §16), so `relay status` on the commander shows `antigravity@<host>`; once the App closes and the sidecar stops, that heartbeat ages out. Any heartbeat push error is recorded in `heartbeat_error` in the output without affecting notifications.

To disable the sidecar, set `sidecars.agent-relay.enabled` to `false` in `config.json`, keeping the mailbox, receipts, and keys intact for auditing.
