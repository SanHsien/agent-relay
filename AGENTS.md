[English](#agent-relay--operating-guide-for-agents) | 繁體中文

# agent-relay — 給 Agent 的操作說明

適用 Claude Code、Codex、Antigravity、Hermes 等任何能跑 shell 指令的 Agent。

## 收信

1. 開場或收到門鈴時：`python -m agent_relay inbox --json`。
2. 逐封 `python -m agent_relay read <id> --json`。`kind=task` 就照內文的驗收條件施工。
3. 完成後回報，再結案：

```powershell
python -m agent_relay send --to <寄件者> --kind result --reply-to <id> --subject "done: <原標題>" --body-file result.md
python -m agent_relay ack <id>
```

回報內文要附證據：測試輸出、commit SHA、檔案路徑。沒有證據不要寄 `result`。

## 寄信

- 派工用 `--kind task`，內文寫「目標與動機／驗收條件（DoD）／回報格式／指定 model」四段（見 [SPEC.md](SPEC.md) 第 6 節）。
- 要讓收件端專案下一個 session `/pickup` 接得到，加 `--project <收件端機器上的專案路徑>`；不知道路徑就寫 `--project asr:<會話名>`，收件端會從 agent-session-restore 的登記檔查。
- 信件會進 git 歷史：不放 API key、token、密碼。
- 派工信要簽章（SPEC 第 13 節）：寄件端先 `relay keygen`，把公鑰交給收件端，由收件端 `relay trust <你的信箱> <公鑰>`。有金鑰時 `send` 會自動簽章。收件端 `dispatch.senders` 沒設就拒絕所有派工。
- 要收件端**自動開新 Session 施工**：加 `--run claude|claude-3p|codex|agy|hermes --repo <收件端 repo>`（見 [SPEC.md](SPEC.md) 第 7 節）。claude-3p 走維護者的 Claude Desktop 3P gateway（見 SPEC 第 7 節）。收件端要先在 config 的 `dispatch` 允許該 runtime 與 repo。不確定派給誰就寫 `--run auto`，收件端依複雜度與它的額度挑（SPEC 第 10 節）；`--path` 寫出工人可改的檔案範圍（SPEC 第 9 節）。

## 信箱名稱

| 信箱 | 誰 |
| --- | --- |
| `commander` | 指揮者（開 Session 派工、驗收的那一方） |
| `claude-code` | Claude Code |
| `codex` | Codex CLI |
| `antigravity` | Antigravity（agy） |
| `hermes` | Hermes（Windows desktop 版） |

新增信箱不需要註冊，第一封寄給它的信會自動建立目錄。

## 改這個 repo

- 只用 Python 標準函式庫，Windows 11 原生優先。
- 改協定先改 [SPEC.md](SPEC.md) 與 [SPEC.en.md](SPEC.en.md)，再改程式和測試。
- 公開文件維持繁體中文（無後綴 `.md`）與英文（`.en.md`）雙語同步：`README.md`／`README.en.md`、`SPEC.md`／`SPEC.en.md`、`docs/antigravity-app-mailbox.md`／`docs/antigravity-app-mailbox.en.md`、`docs/templates/escalation-prompt.md`／`docs/templates/escalation-prompt.en.md`、`docs/nostr-nip01-evaluation.md`／`docs/nostr-nip01-evaluation.en.md`。歷史實測與研究紀錄（`docs/evidence/`、`docs/reports/`）維持繁體中文原貌。
- 提交前執行 `pwsh -NoProfile -File tools/dev_check.ps1`（或 `python -m unittest discover -s tests -v && python tools/check_doc_parity.py && python tools/check_links.py`）全綠才提交。
- 本 repo 公開：私人 repo 一律寫 `[Private Repo: <別名>]`，不寫真名。

---

# agent-relay — Operating Guide for Agents

Applies to Claude Code, Codex, Antigravity, Hermes, or any agent capable of running shell commands.

## Receiving Mail

1. At session start or when a doorbell rings: `python -m agent_relay inbox --json`.
2. Read each message with `python -m agent_relay read <id> --json`. When `kind=task`, implement and verify according to the acceptance criteria in the body.
3. Send a result reply with evidence, then acknowledge the message:

```powershell
python -m agent_relay send --to <sender> --kind result --reply-to <id> --subject "done: <original-subject>" --body-file result.md
python -m agent_relay ack <id>
```

Result bodies must attach concrete evidence: test output, commit SHA, and file paths. Never send `result` without verification evidence.

## Sending Mail

- Use `--kind task` for task dispatch and structure the body into four sections: Goal & Motivation / Definition of Done (DoD) / Report Format / Requested Model (see [SPEC.en.md](SPEC.en.md) Section 6).
- To drop a handoff card for the next `/pickup` session in the target project on the receiver, pass `--project <project-path-on-receiver>`; if the path is unknown, pass `--project asr:<session-name>` so the receiver resolves it from the `agent-session-restore` registry.
- Messages enter git history: never include API keys, tokens, or passwords.
- Task dispatches must be signed (SPEC Section 13): run `relay keygen` on the sender, give the public key to the receiver, and run `relay trust <your-mailbox> <pubkey>` on the receiver. When a key exists, `send` signs automatically. If `dispatch.senders` is unset on the receiver, all dispatches are refused.
- To make the receiver **automatically spawn a new worker session**: pass `--run claude|claude-3p|codex|agy|hermes --repo <receiver-repo>` (see [SPEC.en.md](SPEC.en.md) Section 7). `claude-3p` routes through the local Claude Desktop 3P gateway. The receiver must allow that runtime and repository under `dispatch` in `config.json`. Pass `--run auto` to let the receiver pick based on complexity and local quota (SPEC Section 10), and `--path` to restrict the files the worker may modify (SPEC Section 9).

## Mailbox Names

| Mailbox | Owner |
| --- | --- |
| `commander` | Commander (the side that dispatches work and accepts results) |
| `claude-code` | Claude Code |
| `codex` | Codex CLI |
| `antigravity` | Antigravity (`agy`) |
| `hermes` | Hermes (Windows desktop edition) |

New mailboxes require no pre-registration; sending the first message to a mailbox creates its directory automatically.

## Modifying This Repo

- Use only the Python standard library; prioritize native Windows 11 compatibility.
- When changing the protocol, update [SPEC.md](SPEC.md) and [SPEC.en.md](SPEC.en.md) first, then update the implementation and tests.
- Keep public documentation synchronized in Traditional Chinese (suffix-less `.md`) and English (`.en.md`): `README.md` / `README.en.md`, `SPEC.md` / `SPEC.en.md`, `docs/antigravity-app-mailbox.md` / `docs/antigravity-app-mailbox.en.md`, `docs/templates/escalation-prompt.md` / `docs/templates/escalation-prompt.en.md`, and `docs/nostr-nip01-evaluation.md` / `docs/nostr-nip01-evaluation.en.md`. Historical evidence and benchmark reports (`docs/evidence/`, `docs/reports/`) remain in Traditional Chinese.
- Run `pwsh -NoProfile -File tools/dev_check.ps1` (or `python -m unittest discover -s tests -v && python tools/check_doc_parity.py && python tools/check_links.py`) and ensure all checks pass before committing.
- Because this repository is public, always refer to private repositories as `[Private Repo: <Alias>]` and never write real private repository names.

## 路線圖

- `ROADMAP.md` 是本 repo 後續工作的單一真相源。完成或新增工作時，在同一個 commit 更新它：做完的列刪掉、新工作加進去。不要在其他文件另列待辦。
