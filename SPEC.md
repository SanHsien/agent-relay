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
| `id` | 是 | string | `YYYYMMDDTHHMMSSmmmZ-<6 hex>`（UTC）。字典序 = 時間序。見下方「名稱規則」 |
| `from` | 是 | string | 寄件信箱名，見下方「名稱規則」 |
| `to` | 是 | string | 收件信箱名，規則同上 |
| `created_at` | 是 | string | ISO 8601 UTC，例如 `2026-10-20T01:23:45Z` |
| `kind` | 是 | enum | `task` 派工、`result` 施工回報、`reply` 一般回覆、`note` 通知 |
| `subject` | 是 | string | 一行標題，不可空白，不可有換行 |
| `body` | 是 | string | 內文，Markdown。派工信要寫目標、驗收條件、回報格式 |
| `priority` | 否 | enum | `high` / `normal`（預設）/ `low` |
| `thread` | 否 | string | 對話串 id。新信等於自己的 `id`；回信沿用原信的 `thread`（不是原信的 `id`），所以回信的回信仍在同一串 |
| `reply_to` | 否 | string | 回覆哪封信的 `id` |
| `host` | 否 | string | 寄件機器名稱，查問題用，不可有換行 |
| `project` | 否 | string | 收件端機器上的專案資料夾，或 `asr:<會話名或 id>`（從 [agent-session-restore](https://github.com/SanHsien/agent-session-restore) 的登記檔查路徑）。`kind=task` 且有這欄時，`handoff` 門鈴會在該專案放交接卡 |
| `refs` | 否 | string[] | 相關路徑或 URL |

不認得的欄位要保留、不可報錯，方便日後加欄位。`refs` 必須是字串陣列；`subject`、`host`、`project` 不可含換行（它們會被寫進交接卡的 frontmatter）。

### 名稱規則

信箱名與 `id`（以及 `thread`、`reply_to`）會變成目錄名和檔名，所以整串比對（不要用會放過結尾換行的 `^...$` + match）：

- 信箱名：`[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9_-])?`
- id：`[0-9A-Za-z](?:[0-9A-Za-z._-]{0,126}[0-9A-Za-z_-])?`
- 不可以 `.` 結尾（Windows 會吃掉結尾的點，`codex.` 會變成 `codex` 的同一個資料夾）。
- 不可是 Windows 保留裝置名：`con` `prn` `aux` `nul` `com0-9` `lpt0-9`（不分大小寫，含 `nul.txt` 這種帶副檔名的）。

### 機密與公開 repo

- 信封內文不放 API key、token、密碼。store 是私人 repo，但信件會進 git 歷史，刪不乾淨。
- 會提交到公開 repo 的檔案裡，私人 repo 一律寫 `[Private Repo: <別名>]`（別名表見 [ai-inbox](https://github.com/SanHsien/ai-inbox) 的 `private-aliases.json`）。

## 2. 信箱目錄（Store）

```text
<store>/mailboxes/<agent>/new/<id>.json    已送達、未讀
<store>/mailboxes/<agent>/cur/<id>.json    已讀、還沒結案
<store>/mailboxes/<agent>/done/<id>.json   已結案（ack）
```

- **寫入要原子化**：先寫同目錄的暫存檔（`.tmp-` 開頭），再改名成 `<id>.json`。讀取端忽略 `.tmp-*`，git 也不提交它們。
- **檔案要名實相符**：檔名不等於 `<id>.json`、或 `to` 不等於所在信箱的檔案，讀取端一律略過（不然一個檔可以冒用別封信的 id，讓那封的門鈴不響）。
- **狀態轉換只用改名**：`new → cur → done`，內容不改。所以兩台機器的 git 合併不會衝突在同一行。
- **一個信箱只有一個收件人**：同一封信只由收件信箱的主人搬動。寄件人寫完 `new/` 就不再碰。
- 同一信箱內 `id` 重複要拒收。

## 3. 跨機器傳送（git）

- store 是一個 git repo 的工作目錄。參考部署用 `[Private Repo: 個人記憶庫]` 的 **`mailbox` 孤兒分支**，各機器另外 clone 這個分支，不碰主分支，也不受主分支每日自動同步影響。
- 寄送：寫檔 → `git add -A -- mailboxes` → commit（只含 `mailboxes/`）→ `pull --rebase` → `push`。push 被拒就 rebase 重試，最多 3 次。檔名唯一，rebase 不會衝突；萬一 rebase 失敗要 `rebase --abort`，不能讓 store 卡在 rebase 中。
- push 最後仍失敗時，信已經在本機 commit，下次同步會送出。參考 CLI 回 exit code 3 並註明「已排隊、未推送」，**不要重寄**（重寄會變成兩封）。
- 接收：`git pull --rebase`。
- 不需要公網 IP、不需要額外的 API key：每台機器本來就能 push 到自己的 GitHub repo。
- 延遲 = 收件端排程的輪詢間隔（預設 5 分鐘）。

## 4. 門鈴（Doorbell）

門鈴只是提醒，**信件檔才是真相**。門鈴失敗不影響信件送達。

- `relay watch --once`：pull 後，對 `new/` 裡這台機器還沒響過的每封信，依序按設定的每個門鈴。響過的 id 記在 `~/.agent-relay/rung-<agent>.json`（每台機器各自記，不進 git）。**每響完一封就存一次，而且要原子寫入**（暫存檔再改名）：中途被砍掉的那一輪，下次不會把已響過的全部重響。
- 一個門鈴壞掉要記錄錯誤、繼續按下一個。
- 內建三種：

| type | 做什麼 | 設定 |
| --- | --- | --- |
| `toast` | Windows 右下角通知（WinRT，免安裝模組） | 無 |
| `command` | 執行一個指令（不經 shell），參數可用 `{id}` `{from}` `{to}` `{subject}` `{kind}` `{summary}`。**拒絕 `.bat`/`.cmd`**：Windows 會交給 cmd.exe 重新解析參數，信件內文裡的 `&` 會變成指令。值可能以 `-` 開頭，程式若吃選項請在前面放 `--` | `argv`、`timeout`（秒） |
| `handoff` | `kind=task` 且有 `project` 時，在 `<project>/.claude/handoffs/` 放一張 dropoff/pickup 交接卡（檔名 `relay-<id>.md`，重響不會多一張），下一個 session `/pickup` 就接得到。專案必須在 `project_roots` 底下，沒設就一律拒絕：放卡的位置是寄件人決定的，收件端要先同意哪些資料夾可以放。交接卡裡的回信指令不放信件標題 | `project_roots`（資料夾陣列，必填） |

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

## 7. 派工契約（dispatch）

`kind=task` 的信可以多帶一個 `dispatch` 物件，要求收件端**自動開一個新的 agent Session** 施工，做完自動回信。

```text
指揮者 send --run claude --repo <收件端 repo>
   └─▶ 收件端 watch（排程）── dispatch 門鈴 ──▶ Windows Terminal 新分頁：relay dispatch <id>
                                                 ├─ git worktree：<repo>/.relay-worktrees/<id>，分支 relay/<id>
                                                 ├─ headless claude / codex / agy（逾時強制結束整棵行程）
                                                 └─ 回 kind=result（reply_to、同 thread）＋ ack 原信
```

| 欄位 | 必填 | 說明 |
| --- | --- | --- |
| `runtime` | 是 | `claude`、`codex`、`agy` |
| `repo` | 是 | 收件端機器上的 git repo 路徑，或 `asr:<會話名>` |
| `mode` | 否 | `build`（預設，工人改 worktree 裡的檔，relay 事後替它 commit）或 `plan`（唯讀） |
| `model`、`effort` | 否 | 交給該 runtime 的模型與推理強度。`model` 只能是模型 id 的字元（`[A-Za-z0-9._:/-]`，不可 `-` 開頭）；`effort` 只能是 `low`／`medium`／`high`／`xhigh` |
| `timeout_minutes` | 否 | 1～120，預設 30。超時就結束整棵行程，回信 `status: timeout` |

**收件端政策（config.json 的 `dispatch`，沒設就全部拒絕）**：

```json
"dispatch": {
  "runtimes": ["claude", "agy"],
  "repo_roots": ["C:/GitHub"],
  "senders": ["commander"],
  "cortex_path": "C:/GitHub/agent-cortex",
  "executables": {"claude": "%USERPROFILE%/.local/bin/claude.exe"}
}
```

- runtime 不在 `runtimes`、repo 不在 `repo_roots` 底下、或設了 `senders` 而寄件者不在名單 → 不執行，回一封 `status: refused` 說明原因。
- **只執行真正的 `.exe`**：先找 `executables` 指定的路徑，沒設就找 PATH 上的 `<runtime>.exe`。`.bat`／`.cmd` 外殼一律拒絕：cmd.exe 會重新解析參數，把 prompt 第一個換行之後的內容連同權限旗標一起丟掉，工人就會用使用者的預設權限跑（實際踩過）。
- claude、codex 的 prompt 從 **stdin** 傳，不放命令列（避開 32K 長度上限與換行問題）。
- **工人只改檔，不 commit**：claude 工人預設**沒有任何 shell 指令**（claude 在原生 Windows 沒有 shell 沙箱；只要放行「跑測試」，信件就能叫它先寫一支 conftest.py 再執行，等於沒有沙箱地跑任意程式；`git diff --output=` 也能寫到 worktree 外）。codex 工人在自己的 `workspace-write` 沙箱裡，可以跑驗證但寫不到 `.git`。兩者都拿不到 git 自己的目錄。
- **relay 代為 commit**：工人結束後，relay 在 worktree 跑 `git add -A` 與 `git commit -m "relay: <id>"`，並關掉 hooks（`core.hooksPath` 指向空裝置、`--no-verify`、`core.fsmonitor=false`）。若工人改過 worktree 的 `.git` 指標檔（可能指向自己寫的 hooks），relay 就不 commit，在回信裡註明。
- 收件端可以用 `claude_allowed_tools` 放行 claude 工人跑特定指令（例如 `["Bash(python -m unittest *)"]`），但**這等於允許信件在你的機器上沒有沙箱地執行程式**，只在信任所有寄件者時開。
- 驗證責任在寄件端：工人的回報寫出該跑的驗證指令，指揮者收信後在 worktree 分支上自己跑。
- 工人只在 worktree 的 `relay/<id>` 分支上變動，**不 push、不動主工作目錄**。要不要合併，由收信的指揮者看結果再決定。
- 指令由 [agent-cortex](https://github.com/SanHsien/agent-cortex) 的 launcher 組（設了 `cortex_path` 或能 import 時），沿用它加固過的權限模式：claude `acceptEdits`（plan 模式 `plan`）、codex `workspace-write` 沙箱（plan 模式 `read-only`）、agy 一律 `plan + sandbox`。cortex 載不到就用同樣保守的內建版本。**不會**使用 `bypassPermissions` 或 `--dangerously-*` 旗標。
- 回信內文：狀態與 exit code、runtime、worktree 與分支、耗時、工人最後回報、commit 清單與 `diff --stat`。常見的 token 形狀（`sk-`、`ghp_`、`AKIA`、Bearer 等）會遮成 `[REDACTED]`，因為回信會進 git 歷史。
- **每次派工都一定回一封信並 ack**：`ok`、`failed`、`timeout`、`refused`、`error`（未預期的例外）、`interrupted`。
- **中斷補救**：開跑時寫 `~/.agent-relay/runs/<id>/state.json`（含期限 = 逾時 + 10 分鐘）。分頁被關、當機或重開機時，信會停在 `cur/`；下一輪 `watch` 看到期限已過、還沒回信，就補一封 `status: interrupted` 並 ack，並立刻同步出去。門鈴響了卻一直沒啟動（沒有 state.json，超過 30 分鐘）的信，在有設 dispatch 門鈴時也一樣補回信。補救時某一封出錯只記 log、跳過，不會讓整個 watch 停掉。
- 已 `done` 的信不會再派；要重跑加 `relay dispatch <id> --force`。worktree 已存在時，只沿用 git 登記為 `relay/<id>` 的那個 worktree，diff 以該分支的 merge-base 為準；路徑上若是別的資料夾就拒絕。
- 已知範圍：worktree 之間共用同一個 repo 的物件庫，但工人拿不到 `.git` 目錄的寫入權，commit 一律由 relay 做。
- codex 經 OpenCodex 代理時注意：直接跑 `codex.exe` 會跳過 `codex.CMD` 外殼裡的 `ocx.exe ensure`。代理沒在跑，工人就會失敗（回信 `failed`），先確認代理常駐。

**門鈴**：在 `doorbells` 加 `{"type": "dispatch"}`。它在 Windows Terminal 開新分頁跑 `relay dispatch <id>`（用有主控台的 `python.exe`，relay 的 home 與 store 用參數傳，不靠環境變數），你看得到執行過程；沒有 `wt.exe`，或設了 `"window": false`，就改成背景獨立行程。之所以不在排程工作裡直接跑，是因為排程每輪最多 10 分鐘。
