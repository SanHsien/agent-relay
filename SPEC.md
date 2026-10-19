# agent-relay 協定規格 v1

這份規格定義各 Agent（Claude Code、Codex、Antigravity、Hermes、指揮者）之間怎麼寄信、收信、按門鈴。參考實作是本 repo 的 `agent_relay/`。任何語言照這份規格讀寫檔案都能互通，不一定要用參考 CLI。

```text
寄件端                         共用 store（git repo 的 mailbox 分支）              收件端
relay send ──寫入 new/──▶ mailboxes/<收件者>/new/<id>.json ──git push──▶ 遠端
                                                                          │ git pull（排程每 N 分鐘）
                                                    relay watch --once ◀──┘
                                                       ├─ 門鈴：toast / command / handoff 卡
                                                       ▼
                                   relay read（new→cur） ─▶ 施工 ─▶ relay send --kind result --reply-to <id>
                                                                     relay ack（cur→done）
```

## 1. 信封（Envelope）

一封信 = 一個 UTF-8 JSON 檔，檔名 `<id>.json`。

| 欄位 | 必填 | 型別 | 說明 |
| --- | --- | --- | --- |
| `schema` | 是 | string | 固定 `agent-relay/envelope@1`。看不懂的版本要拒收，不要猜 |
| `id` | 是 | string | `YYYYMMDDTHHMMSSmmmZ-<6 hex>`（UTC）。字典序 = 時間序；只能有 `[0-9A-Za-z._-]` |
| `from` | 是 | string | 寄件信箱名，`^[a-z0-9][a-z0-9._-]{0,63}$` |
| `to` | 是 | string | 收件信箱名，規則同上 |
| `created_at` | 是 | string | ISO 8601 UTC，例如 `2026-10-20T01:23:45Z` |
| `kind` | 是 | enum | `task` 派工、`result` 施工回報、`reply` 一般回覆、`note` 通知 |
| `subject` | 是 | string | 一行標題，不可空白 |
| `body` | 是 | string | 內文，Markdown。派工信要寫目標、驗收條件、回報格式 |
| `priority` | 否 | enum | `high` / `normal`（預設）/ `low` |
| `thread` | 否 | string | 對話串 id。新信等於自己的 `id`，回信沿用原信的 `thread` |
| `reply_to` | 否 | string | 回覆哪封信的 `id` |
| `host` | 否 | string | 寄件機器名稱，查問題用 |
| `project` | 否 | string | 收件端機器上的專案資料夾，或 `asr:<會話名或 id>`（從 [agent-session-restore](https://github.com/SanHsien/agent-session-restore) 的登記檔查路徑）。`kind=task` 且有這欄時，`handoff` 門鈴會在該專案放交接卡 |
| `refs` | 否 | string[] | 相關路徑或 URL |

不認得的欄位要保留、不可報錯，方便日後加欄位。

### 機密與公開 repo

- 信封內文不放 API key、token、密碼。store 是私人 repo，但信件會進 git 歷史，刪不乾淨。
- 會提交到公開 repo 的檔案裡，私人 repo 一律寫 `[Private Repo: <別名>]`（別名表見 [ai-inbox](https://github.com/SanHsien/ai-inbox) 的 `private-aliases.json`）。

## 2. 信箱目錄（Store）

```text
<store>/mailboxes/<agent>/new/<id>.json    已送達、未讀
<store>/mailboxes/<agent>/cur/<id>.json    已讀、還沒結案
<store>/mailboxes/<agent>/done/<id>.json   已結案（ack）
```

- **寫入要原子化**：先寫同目錄的暫存檔（`.tmp-` 開頭），再改名成 `<id>.json`。讀取端忽略 `.tmp-*`。
- **狀態轉換只用改名**：`new → cur → done`，內容不改。所以兩台機器的 git 合併不會衝突在同一行。
- **一個信箱只有一個收件人**：同一封信只由收件信箱的主人搬動。寄件人寫完 `new/` 就不再碰。
- 同一信箱內 `id` 重複要拒收。

## 3. 跨機器傳送（git）

- store 是一個 git repo 的工作目錄。參考部署用 `[Private Repo: 個人記憶庫]` 的 **`mailbox` 孤兒分支**，各機器另外 clone 這個分支，不碰主分支，也不受主分支每日自動同步影響。
- 寄送：寫檔 → `git add -A -- mailboxes` → commit（只含 `mailboxes/`）→ `pull --rebase` → `push`。push 被拒就 rebase 重試，最多 3 次。檔名唯一，rebase 不會衝突。
- 接收：`git pull --rebase`。
- 不需要公網 IP、不需要額外的 API key：每台機器本來就能 push 到自己的 GitHub repo。
- 延遲 = 收件端排程的輪詢間隔（預設 5 分鐘）。

## 4. 門鈴（Doorbell）

門鈴只是提醒，**信件檔才是真相**。門鈴失敗不影響信件送達。

- `relay watch --once`：pull 後，對 `new/` 裡這台機器還沒響過的每封信，依序按設定的每個門鈴。響過的 id 記在 `~/.agent-relay/rung-<agent>.json`（每台機器各自記，不進 git）。
- 一個門鈴壞掉要記錄錯誤、繼續按下一個。
- 內建三種：

| type | 做什麼 | 設定 |
| --- | --- | --- |
| `toast` | Windows 右下角通知（WinRT，免安裝模組） | 無 |
| `command` | 執行一個指令（不經 shell），參數可用 `{id}` `{from}` `{to}` `{subject}` `{kind}` `{summary}` | `argv`、`timeout`（秒） |
| `handoff` | `kind=task` 且有 `project` 時，在 `<project>/.claude/handoffs/` 放一張 dropoff/pickup 交接卡，下一個 session `/pickup` 就接得到 | 無 |

每個門鈴都可加 `"kinds": ["task", "result"]` 只對某些信響。手機通知用 `command` 接既有的 Telegram 腳本，例如：

```json
{"type": "command", "kinds": ["task", "result"], "argv": ["bash", "C:/Users/<you>/.claude/scripts/telegram-send.sh", "{summary}"]}
```

## 5. 輪詢排程（Windows）

`relay poller install --agent <name> --every 5` 註冊 Windows 排程工作 `agent-relay-watch-<name>`，每 5 分鐘跑一次 `watch --once`。用 XML 註冊才能設 `StartWhenAvailable`：睡眠或關機錯過的那一輪，開機後會補跑（做法沿用 [agentdeck](https://github.com/SanHsien/agentdeck) 的自動續跑排程）。

## 6. 派工信的內文慣例

`kind=task` 的 `body` 至少要有這四段，Phase 2 的派工契約會在這之上加欄位：

```markdown
## 目標與動機
## 驗收條件（DoD）
## 回報格式
## 指定 model / runtime（選填）
```

收件端完成後寄 `kind=result`、`reply_to=<原信 id>`，內文附證據（測試輸出、commit、路徑），再 `ack` 原信。
