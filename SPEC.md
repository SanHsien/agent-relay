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

`relay poller install --agent <name> --every 5` 註冊 Windows 排程工作 `agent-relay-watch-<name>`，每 5 分鐘跑一次 `watch --once`。同一台機器可以有好幾個 relay home，例如指揮者用 `~/.agent-relay`，收件工人用 `~/.agent-relay-worker`。所有指令都接受 `--home`；用非預設 home 註冊的排程會自動帶上 `--home`，因為排程和新分頁都不會繼承環境變數。用 XML 註冊才能設 `StartWhenAvailable`：睡眠或關機錯過的那一輪，開機後會補跑（做法沿用 [agentdeck](https://github.com/SanHsien/agentdeck) 的自動續跑排程）。

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
| `runtime` | 是 | `claude`、`claude-3p`、`codex`、`agy`、`hermes` |
| `repo` | 是 | 收件端機器上的 git repo 路徑，或 `asr:<會話名>` |
| `mode` | 否 | `build`（預設，工人改 worktree 裡的檔，relay 事後替它 commit）或 `plan`（唯讀） |
| `model`、`effort` | 否 | 交給該 runtime 的模型與推理強度。`model` 只能是模型 id 的字元（`[A-Za-z0-9._:/-]`，不可 `-` 開頭）；`effort` 只能是 `low`／`medium`／`high`／`xhigh` |
| `timeout_minutes` | 否 | 1～120，預設 30。超時就結束整棵行程，回信 `status: timeout` |
| `base` | 否 | 施工起點的完整 commit sha（40 個小寫 hex；短前綴可能被能推送 origin 的人暴力碰撞，所以不收）。寄件端 `--base` 可寫任何 ref，CLI 會在目前的 repo 先解析成完整 sha。收件端本機沒有就 `git fetch origin` 一次，仍找不到就 `refused`。沒寫就用收件端當下的 HEAD；那可能在別的分支上，或帶著還沒推送的 commit，所以要把成果帶回去（第 14 節）的派工都應該寫 `base` |

**回信的結構化欄位**：派工回信（`kind=result`）多一個 `result` 物件，給程式讀，不用解析內文。它在簽章範圍內：

```json
"result": {"status": "ok", "runtime": "hermes", "branch": "relay/<id>", "commit": "<40 位 sha>",
           "base": "<40 位 sha>", "gate": {"status": "ok", "logs": [{"phase": "green", "exit": 0, "sha256": "…"}]},
           "usage": [{"attempt": 1, "model": "…", "total_tokens": 0}]}
```

`commit` 只在 relay 真的 commit 時才有。refused／error 的回信只有 `{"status": …}`。

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

- 下列任一情況都不執行，回一封 `status: refused` 說明原因：
  - runtime 不在 `runtimes`；
  - repo 不在 `repo_roots` 底下；
  - 寄件者不在 `senders`。`senders` **沒設或空清單就一律拒絕**；
  - 信件沒有本機信任的簽章（第 13 節；`require_signature` 預設 true）。
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
- **claude-3p**：同一個 Claude Code CLI，改經 Claude Desktop 3P 的 gateway（例如本機 opencodex，`127.0.0.1:10100`）呼叫模型。權限、prompt 走 stdin、不給 shell 等規則都和 `claude` 相同。
  - gateway 的網址與金鑰在**執行當下**從 3P 設定檔讀取：預設是 `%LOCALAPPDATA%\Claude-3p\configLibrary\_meta.json` 裡名為 opencodex 的設定，或目前套用中的設定。也可以用 `dispatch.claude_3p.profile` 指定設定檔。
  - 讀到的值放進工人的 `ANTHROPIC_BASE_URL`／`ANTHROPIC_API_KEY`，並移除 `ANTHROPIC_AUTH_TOKEN`，避免改用官方登入。金鑰不會寫進 relay 設定、信件、argv 或 log。
  - 必須指定模型：信件的 `model`，或收件端的 `dispatch.claude_3p.model`。模型 id 以 gateway 解析的結果為準，不能只看顯示名稱。
  - 讀不到設定檔時回 `refused`。`runtime auto`、改派、升級都不會選它，因為它的額度查不到，必須明寫 `--run claude-3p`。
- **hermes**（Hermes Windows desktop 版 CLI）：`hermes.exe -z <短指令> --in <worktree> -t file --usage-file runs/<id>/hermes-usage.json`，可加 `-m`、`--reasoning`。
  - 單次模式 `-z` **會自動略過核可**，所以安全只靠兩道限制：工具組只給 `file`（`read_file`／`write_file`／`patch`／`search_files`，沒有 terminal 和程式執行）；環境變數 `HERMES_WRITE_SAFE_ROOT` 設為 worktree，寫到 worktree 外會被 Hermes 拒絕。plan 模式把寫入根目錄設成 run 目錄下的空資料夾，worktree 對它是唯讀。
  - **讀取不受限**：寫入根目錄只管寫，`read_file`／`search_files` 只擋 Hermes 自己的憑證與 `.env`。工人讀得到 `~/.ssh`、其他 repo 和 relay store；被 prompt injection 時，它能把讀到的東西寫進 worktree，經 relay commit 後進入回信的 git 歷史。codex 的 `workspace-write` 也有同類風險。只對信任的寄件者放行 hermes。
  - exit 0 但用量報告 `completed: false`（迭代額度用完或被中斷）視為 `failed`。每次嘗試前先刪掉舊的用量檔；回信列出每次 Hermes 嘗試的用量，升級到其他 runtime 後也保留。
  - hermes 不會被 `runtime auto`、改派或 `dispatch.escalation` 選到：這三者只用能施工的 claude／codex，而且 hermes 的額度查不到。要用 hermes 就明寫 `--run hermes`。
  - 不使用 `--yolo`，也不經過 agent-cortex。
  - prompt 寫進 `runs/<id>/prompt.md`，命令列只放「讀這個檔」的短指令。
  - 回信附 `--usage-file` 的用量（模型、tokens、api_calls、估計費用）。
  - Hermes 沒有 shell，跟 claude 工人一樣不能自己跑測試，驗證交給驗收閘門。
- **codex 沙箱釘住 worktree**：使用者的 `~/.codex/config.toml` 可能用 `sandbox_workspace_write.writable_roots`（或 `%TEMP%`）把 workspace-write 撐大；實測曾因此讓工人寫進主 repo 的 `.git/hooks`。relay 每次都加 `-c sandbox_workspace_write.writable_roots=[]`、`exclude_tmpdir_env_var=true`、`exclude_slash_tmp=true`。每次嘗試的實際指令（不含 prompt）寫在 `runs/<id>/argv.json`（重試時前一次留成 `argv.N.json`）與回信的「指令」行。另加 `network_access=false`。前提：Windows 上的檔案寫入隔離由 codex 的 Windows 沙箱執行，實測環境是 `~/.codex/config.toml` 的 `[windows] sandbox = "elevated"`；其他收件機請先用一張含反例的工單確認它真的擋得住。
- codex 經 OpenCodex 代理時注意：直接跑 `codex.exe` 會跳過 `codex.CMD` 外殼裡的 `ocx.exe ensure`。代理沒在跑，工人就會失敗（回信 `failed`），先確認代理常駐。

**門鈴**：在 `doorbells` 加 `{"type": "dispatch"}`。它在 Windows Terminal 開新分頁跑 `relay dispatch <id>`（用有主控台的 `python.exe`，relay 的 home 與 store 用參數傳，不靠環境變數），你看得到執行過程；沒有 `wt.exe`，或設了 `"window": false`，就改成背景獨立行程。之所以不在排程工作裡直接跑，是因為排程每輪最多 10 分鐘。

## 8. 接手資料（hand-over bundle）

新開的 session 不用人工貼上下文：`relay context --repo <repo>` 把下列來源整理成一份有上限的 Markdown，自動帶給新 session。

```text
<repo>/HANDOFF.md                        交接單（cc-session-reader／agent-handoff 的階段一產物）
<vault>/handoffs/<repo 資料夾名>.md       Obsidian 交接筆記（claude-obsidian 的 vault；要設 context.obsidian_vault）
<vault>/wiki/hot.md                       claude-obsidian 的 hot cache（整個 vault 共用，要另設 context.hot_cache: true）
<repo>/.claude/handoffs/*.md              status: pending 的 dropoff/pickup 交接卡（只列標題）
上一個 session                            cc-session list -p <完整專案 key> 找「剛好是這個 repo」的最近一次，取最後 60 行
git 狀態                                  分支、已追蹤但未提交的檔數、最近 5 筆 commit
```

兩個注入點：

1. **派工**：`relay dispatch` 把收件端 repo 的交接包接在工人 prompt 後面（`dispatch.context: false` 可關）。**上一個 session 的內容預設不帶**（裡面可能有工具輸出，而回信會進 git 歷史）；要帶就設 `dispatch.context_sessions: true`。
2. **任何新 session**：Claude Code 的 SessionStart hook。在 settings（使用者、專案，或 `--settings` 檔）加：

   ```json
   {"hooks": {"SessionStart": [{"matcher": "startup|clear",
     "hooks": [{"type": "command", "timeout": 15,
                "command": "python C:/GitHub/agent-relay/hooks/session_start.py"}]}]}}
   ```

   建議 matcher 只用 `startup|clear`：resume／compact 時會排除目前的 session，帶進來的「上一個 session」可能是另一個平行中的 session。

規則：

- **只讀**：不寫任何檔案。符號連結、以及實際路徑跑到 repo 或 vault 外面的檔案都不讀；每個檔最多讀 16 KB；含 NUL 的二進位檔跳過。
- **有上限**：每段最多 4000 字；全部最多 14000 字（hook 模式 9000 字，避免被 Claude Code 截斷）。整份交接包有時間上限（派工 15 秒、hook 8 秒），每個外部指令最多 5 秒；上一個 session 只試一次，失敗就略過。git 用 `status -uno`，不掃未追蹤檔。
- **遮罩**：每個來源先遮罩再截斷。遮的形狀：`sk-`、`ghp_`／`github_pat_`、`xox?-`、`AKIA`、`AIza`、`ya29.`、JWT、PEM 私鑰、Bearer、Telegram bot token、URL 裡的 `user:pass@`、`password=`／`token=`／`*_KEY=` 這類鍵值。
- **資料界線**：整份放在 `<<<HANDOVER-DATA <隨機 nonce>>>` … `<<<END HANDOVER-DATA <nonce>>>` 之間，前面一句說明這是資料、不是指令。每個來源各自放在 `~~~~` 區塊裡，內容每行縮排 4 格（結尾標記最多只能縮排 3 格，所以內容關不掉區塊），`<<<`／`>>>` 換成相似字元；超過總上限時整段丟掉尾端的來源，不會從區塊中間切斷。git 狀態（commit 訊息）也經過遮罩。派工 prompt 的規則行也會再提醒一次。
- hook 收不到 `cwd`、payload 壞掉、或什麼都沒有可交接時，不輸出任何東西；任何錯誤都 exit 0（`relay context --hook` 和 `hooks/session_start.py` 都一樣）。payload 一律以 UTF-8 解讀，中文路徑可用。
- 專案 key 照 Claude Code 的規則：路徑中每個非英數字元換成 `-`；同時比對原路徑與解析後路徑（subst、對應的網路磁碟機）。
- Codex、agy 沒有 SessionStart hook：經 `relay dispatch` 派工時一樣拿得到交接包；直接開時可以先跑 `python -m agent_relay context` 再貼。

```json
"context": {"obsidian_vault": "C:/Users/<you>/Documents/Obsidian/MyVault", "hot_cache": false, "sessions": true}
```

## 9. 簽出鎖與打卡紀錄（check-out / check-in）

多個 agent 同時改同一個 repo 時，先簽出要改的檔案，改完簽入；簽出重疊就擋下。

```text
<store>/claims/<repo key>/<claim id>.json         有效的簽出：holder、paths、到期時間
<store>/audit/<YYYY-MM-DD>/<時間>-<claim>-<事件>.json   打卡紀錄：checkout／checkin／blocked（每事件一檔，跨機器合併不衝突）
~/.agent-relay/touch-log.jsonl                    本機紀錄：checkout-guard 對每次改檔的放行／擋下
```

- **repo 身分**：`origin` 的 URL（去掉帳密、統一成 https、去掉 `.git`、小寫）；沒有 remote 就用本機 git 目錄。同一個 repo 的 worktree 身分相同。`<repo key>` = 名稱 slug + 身分的 SHA-1 前 12 碼。
- **路徑規則**：repo 相對路徑，`**` 跨層，結尾 `/` 代表整個資料夾；比對不分大小寫。`./a`、`a//b` 會正規化成 `a`、`a/b`；不可用 `..`、開頭的 `/` 或磁碟機代號跳出 repo。兩個 claim 是否重疊，沿用 [claude-code-projects-workflow](https://github.com/SanHsien/claude-code-projects-workflow) 的 `may_overlap` 保守判斷：可能碰到同一個（即使還不存在的）檔案就算重疊。
- **簽出**：本機以 `claims/<key>/.lock`（O_EXCL）把「檢查＋寫入」包成一步，同機器上不會兩個都成功。跨機器靠 git 同步，對方 pull 後才看得到；兩台機器在同一輪同時簽出時，可能都成功，這時衝突預測（下面）會在回信裡點出來。
- **派工**：task 信的 `dispatch.paths` 就是工人可以改的檔案範圍。relay 在開 worktree 前簽出（holder = `relay:<信件 id>`，期限 = 逾時 + 10 分鐘），重疊就回 `status: refused`，原因寫明誰持有哪些檔案、到何時。收件端可設 `dispatch.require_paths: true` 要求每封派工都要寫範圍。
- **施工後**：
  - 工人改到範圍外的檔案 → relay 不 commit，回 `status: out-of-scope` 並列出那些檔案。變更清單用 `--no-renames -z` 與 `core.quotePath=false` 取得：中文檔名照原樣比對；搬檔算「刪舊檔＋加新檔」，把範圍外的檔搬進範圍內也會被抓到。讀不到變更清單、或工人改過 worktree 的 `.git` 指標時，一律當成範圍外，不 commit。worktree 內的 git 一律關掉 hooks 與 fsmonitor。
  - **衝突預測**：列出「主線自 base 起也改過」的檔案，以及「目前被別人簽出」的檔案，合併前先處理。
  - 不論結果都簽入（刪除 claim），打卡紀錄寫下實際改了哪些檔案。
- **互動 session**：[harness-guard](https://github.com/SanHsien/harness-guard) 的 `checkout-guard`（PreToolUse：Edit、Write、MultiEdit、NotebookEdit）讀本機 store 的 claim；要改的檔案被別人簽出就擋下（exit 2，說明持有者）。「我」= `AGENT_RELAY_HOLDER`（relay 會替它開的工人設好），否則 `manual:<config 的 agent>@<主機名>`，與 `relay claim` 的預設一致。hook 找 store 的規則與 CLI 相同（config.json，再被 `AGENT_RELAY_STORE`／`AGENT_RELAY_AGENT` 覆蓋，預設 `<home>/store`）。壞掉或缺 schema 的 claim 檔會被略過，不會讓 hook 整個失效。
- **已知限制**：同一台機器上的兩個互動 session 身分相同（都是 `manual:<agent>@<主機>`），彼此不會互擋；要互擋就各自設不同的 `AGENT_RELAY_HOLDER`。checkout-guard 不攔 Bash 寫檔。
- **手動簽出**：`relay claim --repo . --path calc.py --ttl 60`、`relay claims --repo .`、`relay release <claim id> --changed calc.py`（只能釋放自己的 claim，別人的要加 `--force`，打卡紀錄會記下是誰釋放的）。

## 10. 額度感知分派與斷線續跑（`runtime: auto`）

派工信寫 `--run auto`，由**收件端**依工單複雜度與**它自己機器上的**額度決定 runtime、模型與推理強度（實際花的是收件端的帳號）。`relay advise` 可以先看決定與交接卡。

```text
工單（標題、內文、paths、mode）
   │ 複雜度：high（除錯、根因、競態、架構、重構、安全…）／low（錯字、格式、註解、文件…）／medium
   ▼
額度 ── claude：~/.claude/agentdeck-status.json（agentdeck statusline 寫的；重置時間已過視為 0%）
   │    codex：codex-reset-checker --json（SanHsien/codex-reset-checker；設 advisor.codex_quota_command）
   │    agy：無本機來源，視為未知（可用）
   ▼
路由表（照 agent-advisor 的分工）            額度門檻：5 小時 ≥ 90% 或週 ≥ 95% 就跳過
  high   claude/opus → codex(effort high) → claude/sonnet
  medium claude/sonnet → codex(effort medium) → agy
  low    agy → claude/haiku → codex(effort low)
  build 模式不選 agy（relay 讓 agy 只讀）；只在 dispatch.runtimes 允許的 runtime 裡挑
```

- `auto` 不可再寫 `model`／`effort`（那是某個 runtime 專用的 id，收件端會一起決定）。
- 先做寄件者、repo、範圍的政策檢查，再查額度：沒授權的寄件者拿到 `refused`，不會看到本機額度。`dispatch.runtimes` 裡沒有能跑這種任務的 runtime（例如 build 模式只允許 agy）也是 `refused`。
- 全部被額度擋下 → 不執行，回 `status: deferred`，寫明最早的重置時間。額度檔或 checker 輸出格式不對時當成「未知（可用）」，並在分派理由註明「讀取失敗」。
- 關鍵字比對：英文用詞邊界，路徑、檔名、`code` 片段不算；有難度關鍵字時，機械性關鍵字不會把它降級。
- 回信附「分派（runtime auto）」段：複雜度依據、被跳過的 runtime 與原因、選中的路由。
- **斷線續跑**：工人失敗時看 log 結尾——
  - 像斷線或暫時性錯誤（逾時、ECONNRESET、502／503／504、overloaded…）→ 稍等後在同一個 worktree 原地重跑（沿用 [codex-auto-retry](https://github.com/SanHsien/codex-auto-retry) 的做法）。
  - 像限流（429、rate limit、usage limit、quota exceeded…）→ 改派路由表的下一個 runtime。
  - 其他失敗不重試。重試次數由 `dispatch.retries`（0～3，預設 1）控制；每次嘗試都列在回信的「嘗試」段，前幾次的 log 保留為 `output.<n>.log`。
  - 判斷只看 runtime 自己回報的錯誤：JSON 事件裡的 `is_error` 結果、`api_error_status`、codex 的 `error`／`turn.failed`，加上最後幾行非 JSON 的 stderr。工人自己的輸出（工具結果、報告裡提到 429 或 `rate_limiter.py`）不算。
  - 已被限流的 runtime 不會再被改派回去。
  - **每次嘗試前、跑驗收閘門前都重寫租約並延長簽出期限**；reaper 補 `interrupted` 前也會先確認那個 dispatch 行程是否還活著。
- **交接卡**：`relay advise --card` 印出 Obsidian／Markdown 卡片：frontmatter（status、complexity、runtime、model、effort、tags）＋派給誰、範圍、要做什麼、驗收條件、備案、派工當下的額度。

```json
"advisor": {"codex_quota_command": ["node", "C:/GitHub/codex-reset-checker/bin/codex-reset-checker.js"],
            "claude_status_file": "C:/Users/<you>/.claude/agentdeck-status.json"}
```

未涵蓋：Antigravity-Manager 的帳號切換（那是帳號設定的變更，不由 relay 自動做）；agy 的額度。

## 11. 驗收閘門（強制 TDD、禁止空殼、真實執行紀錄）

工人做完、範圍檢查通過後，relay 在收件端跑驗收閘門；沒過就不 commit，回信狀態寫明哪一關。

```text
防護     工人刪了測試檔、或改了測試設定檔（conftest.py、pyproject.toml、setup.cfg、tox.ini、pytest.ini、
         loop-policy.toml、package.json…）→ guarded（要允許就設 verify.allow_test_deletion／allow_config_changes）
空殼檢查 工人**新增或改動**的非測試 .py 函式，本體只有 pass／...／return None／raise NotImplementedError → stub-rejected
         （base 裡原本就有的 no-op 不算；@abstractmethod、@overload、Protocol 成員不算；繼承 ABC 但沒標 abstract 的照樣算）
TDD      信件 dispatch.tdd: true 或設定 verify.tdd: true 時，在 base commit 開暫時 worktree：
           base 單獨跑驗證指令 → 必須通過（起點是綠的，否則 tdd-not-proven）
           放進工人新增或修改的測試檔再跑 → 必須失敗（證明重現了問題，否則 tdd-not-proven）
綠燈     在工人的 worktree 跑驗證指令 → 失敗就 verification-failed
紀錄     每次執行的完整終端輸出直接寫進 runs/<id>/verify-base.log、verify-red.log、verify-green.log；
         逾時就結束整棵行程（含孫行程）；回信附指令、exit code、最後 15 行與 log 的 sha256；
         store/audit 記一筆 verify 事件（不含輸出內容）
```

收件端設定（信件**只能要求** TDD，不能提供要跑的指令）：

```json
"dispatch": {
  "verify": {"command": ["python", "-m", "unittest"], "tdd": false, "no_stubs": true, "timeout_minutes": 10},
  "require_verification": true
}
```

- `command` 可以寫 `"loop-policy"`：沿用 repo 自己的 `loop-policy.toml` `[verification].quick_gate`（[ai-quality-gates](https://github.com/SanHsien/ai-quality-gates) 的格式）。
- `command` 也可以寫 `"auto"`，依序採用：
  1. `loop-policy.toml`；
  2. `tests/` 底下有 `test_*.py`：`python -m unittest discover -s tests`；
  3. `package.json` 有真正的 `test` script（不是 npm 預設的佔位指令）：`npm test --silent`；
  4. 有 `go.mod`：`go test ./...`。

  都不符合就沒有閘門。一台機器管很多 repo 時用這個。
- `by_repo`：例如 `{"by_repo": {"harness-guard": ["python", "tests/run-tests.py"]}}`，用 repo 資料夾名稱指定那個 repo 的指令，優先於 `command`。
- 上面三種判斷都只讀**主工作目錄**的檔案，不讀工人的分支，所以工人改不了驗證方式。
- `require_verification: true` 時，沒有設定閘門就回 `unverified`（不 commit）；信件要求 TDD 但收件端沒設閘門也一樣。沒設這些時閘門是關的，行為和以前一樣。
- 閘門會執行工人寫的測試程式，等同你自己手動跑測試；只對信任的寄件者與 repo 開啟。
- 開了閘門（或要求 TDD）而工人沒有正常結束（failed／timeout）時，relay 不 commit 半成品。
- **已知限制**：紅燈只看 exit code，看不出失敗原因（測試 import 了工人新加的模組，在 base 上 ImportError 也算紅燈）；暫時 worktree 沒有被 gitignore 的東西（.venv、node_modules、建置產物），依賴它們的指令在 base 就會失敗，結果是 tdd-not-proven（偏向擋下）。空殼檢查只看 Python，抓不到 lambda 或其他語言。被 gitignore 的新檔案不在變更清單裡，防護檢查看不到；空殼比對以「函式名稱＋本體」判斷，不分所在類別（base 某類別已有的 `__init__: pass`，會讓另一個類別新加的同樣空殼漏網）。工人仍可在受保護清單以外的地方動手腳讓測試空過（例如改測試 helper），閘門擋的是「沒重現就修」和「沒跑測試就宣稱完成」，不是蓄意欺騙；合併前仍要看 diff。
- 互動 session 的同類防線在 [harness-guard](https://github.com/SanHsien/harness-guard)：`claim-guard`（沒有驗證紀錄就宣稱完成會被擋）、`test-gate-guard`（測試沒綠不准 commit／push）。[agent-governance-toolkit](https://github.com/SanHsien/agent-governance-toolkit) 的執行期政策引擎可作為更嚴格的外層，relay 不直接依賴它。

## 12. 回流升級除錯（Fallback Escalation Loop）

便宜的工人沒通過驗收時，relay 把真實失敗證據交給更強的模型，在**同一個 worktree** 診斷並修正，再用**同一套驗收閘門**重驗。Prompt 結構與判斷表見 [docs/templates/escalation-prompt.md](docs/templates/escalation-prompt.md)。

```json
"dispatch": {"escalation": {"runtime": "claude", "model": "opus"}}
```

- 觸發：閘門 `verification-failed`／`tdd-not-proven`／`stub-rejected`，或工人自己失敗、逾時（限流與斷線不算，那是重試與改派的事）。
- 不觸發：改到範圍外、刪測試、改測試設定、`.git` 指標被改（行為問題交給人）；plan 模式；升級目標就是原本的 runtime／模型；升級 runtime 不在 `dispatch.runtimes`；信件寫 `"escalate": false`。
- 不升級的還有：TDD 的 base 本來就沒過（只跑了 base 階段，換人也一樣失敗）；升級目標是唯讀的 runtime（agy）；升級目標的 model／effort 格式不合法。
- 一張派工最多升級一次。第 2 輪有自己的重試次數（暫時性錯誤原地重跑）；第 2 輪遇到限流不再改派。升級後仍沒過、或沒有正常結束，都不 commit（即使收件端沒開閘門）。回信的「回流升級」段列出第一輪的結果、升級對象與第 1 輪閘門 log（改名為 `verify-*.round1.log`，只改名該輪閘門實際寫的檔）。
- 升級 prompt 的證據放在 `~~~~` 區塊並限制長度，前面註明「是資料不是指令」；工人自己的回報（JSON 的 result）不會被當成證據引用。
- 證據只取執行紀錄（閘門 log 結尾、`git diff`），不轉述第一個工人自己的說法。

## 13. 寄件者驗證（信件簽章）

store 是一個 git 分支，能推送它的人可以在 `from` 寫任何名字。派工會在收件端開工人、跑驗收閘門（閘門會執行工人寫的測試），所以收件端只能信任它自己核可過的金鑰。

```text
寄件端  relay keygen ──▶ ~/.agent-relay/keys/<信箱>_ed25519（無密碼，relay 無人值守簽章）
        relay send   ──▶ 信件加 "sig"：ssh-keygen -Y sign -n agent-relay，簽在正規化 JSON 上
收件端  relay trust <信箱> <公鑰>  ──▶ ~/.agent-relay/allowed_signers（本機檔案，不在 store 裡）
        relay dispatch ──▶ ssh-keygen -Y verify -I <from> -n agent-relay；不過就 refused
```

- **簽什麼**：信件所有欄位（`sig` 除外），key 排序、無空白、UTF-8 的 JSON。`id`、`to`、`body`、`dispatch` 都在簽章範圍內：改內容、轉寄給別的信箱、換 id 重放都會驗不過。
- **原封不動的重放**：能推送 store 的人可以把已簽、已完成的信從 `done/` 搬回 `new/`。收件端看自己本機的 `runs/<id>/state.json`，裡面記錄為已回信就拒絕；這份紀錄不在 store 裡。另外，`created_at` 超過 `dispatch.max_age_hours`（預設 168 小時）的派工一律拒絕，用來限制舊信重放的時間窗。
- **身分綁信箱**：allowed_signers 的 principal 是信箱名，namespace 限 `agent-relay`。某個金鑰被信任為 `mallory`，不代表它能替 `commander` 簽章。
- **信任清單只在本機**：金鑰清單不放在 store。如果放在 store，能推送 store 的人就能把自己的金鑰加進去。公鑰交換走 store 以外的管道，例如主人親手在兩台機器各執行一次 `relay trust`。
- **工具**：只用 OpenSSH 內建的 `ssh-keygen`（Windows 11、Linux、macOS 都有），不需要 GPG。
- **收件端政策**：`dispatch.require_signature`（預設 `true`）與 `dispatch.senders`（必填）。只在測試環境才關簽章。
- **設定**：`signing.key` 與 `signing.allowed_signers` 可以改路徑，預設是 `~/.agent-relay/keys/<agent>_ed25519` 與 `~/.agent-relay/allowed_signers`。`trust` 只接受合法的信箱名稱：principal 欄位支援萬用字元與逗號清單，`*` 會讓一把金鑰替所有信箱簽章。
- **從舊版升級**（2026-10-21 之前的 config）：舊收件端升上來後，所有派工都會變成 `refused`。要做三件事：
  1. 收件端在 `dispatch` 補上 `"senders": [...]`；
  2. 寄件端執行 `relay keygen`；
  3. 收件端執行 `relay trust <寄件信箱> <公鑰>`。

  收件端也執行 `keygen`，並讓寄件端 `trust` 它，回信就會帶簽章。
- **回信也簽**：收件端有金鑰時，派工回信（含 `interrupted`）也會簽章，指揮者 `relay accept`（第 14 節）可以據此確認回信真的來自收件端。簽章失敗時照樣送出未簽的回信，並記入 log：回信一定要送到。
- **已知限制**：私鑰沒有密碼，保護靠檔案權限（ssh-keygen 建立時只給目前使用者）。拿得到收件端使用者帳號的人本來就能直接執行程式，不在這個威脅模型內。

## 14. 結果回傳與指揮者驗收合併（`push_branch`＋`relay accept`）

```text
收件端  dispatch ok、閘門過、relay commit ──▶ push relay/<id> 到 repo 的 origin（dispatch.push_branch）
                                              回信 result.pushed = {remote, ref, commit}（有簽章）
指揮者  relay accept <回信 id> --repo <自己的 clone> [--into <分支>]
          ├─ 本機前置檢查（失敗只報錯、不記錄、不通知收件端）：有 accept.verify.command、
          │  不是 detached HEAD、目前分支就是 --into、工作目錄乾淨、這個 commit 還沒合併過
          ├─ 驗回信簽章、status ok、commit／base 是完整 sha、pushed 指向 relay/<回覆的那張派工 id>
          ├─ fetch origin relay/<id>：FETCH_HEAD 必須等於回信的 commit（回信後又被推的分支不收）
          ├─ base 必須是 commit 的祖先
          ├─ 暫時 worktree：從目前 HEAD 做 merge --no-ff <commit>，在「合併結果」上跑指揮者自己的
          │  accept.verify.command（不讀分支裡的設定）
          └─ 通過且 HEAD 沒動過，才把主 repo merge --ff-only 到那個驗證過的合併 commit。
             其他失敗 = rejected；結果寫 audit，並回一封 note 給收件端
```

**收件端（推送）**：

- `dispatch.push_branch: true` 才推送。條件是工人正常結束（status ok）、驗收閘門通過或沒設閘門，而且 relay 真的 commit 了。推送前會關掉 hooks。
- 只推到收件端自己 clone 的 `origin`，信件指定不了遠端。檢查的是 `git remote get-url --push --all origin`，也就是 git 真正會推的每一個網址（含 `pushurl`、`pushInsteadOf`）。每個網址都必須是下列其中一種，否則一律不推送，回信寫明原因：
  - 主人自己的 GitHub repo，網址要完全符合：`https://github.com/SanHsien/<repo>`、`git@github.com:SanHsien/<repo>` 或 `ssh://git@github.com/SanHsien/<repo>`，可接 `.git`。只比對前綴不夠：`SanHsien/../other/x.git` 會被 libcurl 折疊成別人的 repo。
  - `dispatch.push_remotes` 明列的遠端（本機路徑要完全相同）。
- 推送失敗不影響派工的 status；回信的「推送」段落與 `push_error` 會寫明原因，`result` 不會有 `pushed`。

**指揮者（驗收）**：

```json
"accept": {
  "verify": {"command": ["python", "-m", "unittest"], "timeout_minutes": 10},
  "require_signature": true
}
```

- 沒設 `accept.verify.command` 就拒絕合併，不合併沒驗證過的成果。`accept.verify` 與收件端一樣接受 `"auto"`、`"loop-policy"`、`by_repo`（讀的是指揮者 clone 的主工作目錄）。`"loop-policy"` 讀的是指揮者 clone 主工作目錄的 `loop-policy.toml`，不是分支裡的那份。
- 驗證的是「目前分支＋成果」的合併結果，不只是分支本身：派工送出後，目標分支可能已經往前走。驗證在暫時 worktree 跑，log 存在 `~/.agent-relay/accepts/<回信 id>/verify-*.log`，連同 sha256 寫進 audit 和給收件端的 note。
- 合併訊息是 `relay accept: relay/<id> (reply <回信 id>)`。合併有衝突時回報 rejected，主 repo 不受影響。
- 主 repo 只做 fast-forward，不會產生新的合併 commit，所以合進來的就是驗證過的那個 commit。暫時 worktree 裡的合併會關掉 hooks。
- 執行中遇到 git 逾時或 I/O 錯誤，記為 rejected 並附原因；記錄或通知失敗時，不會掩蓋已完成的合併（寫在 notes）。
- 結束後回信會被 ack，收件端收到 `accepted:`／`rejected:` 開頭的 note。
- 已知限制：驗證指令會執行分支裡的程式碼（驗證本來就要執行它）；派工前就要只信任簽章過的收件端。
- **測試拓撲限制**：第 7～17 節的跨機器行為，都只在同一台主機的兩份 store／repo clone 上實測過，沒有在兩台實體機器上跑過。下列差異沒有涵蓋：
  - 兩台機器的時鐘不同步。會影響的機制：`max_age_hours`、心跳逾時、lease 期限，這些都拿 `created_at` 或檔案時間和本機時間比。
  - 兩台機器各自使用不同的 git 認證與 SSH 金鑰。
  - 真實網路的延遲與斷線（push、fetch 的重試）。

  第一次在兩台機器上使用前，先把指揮者和收件端的時鐘都設成自動對時，再跑一張含反例的工單確認。

## 15. 收件端上限與防重跑

```json
"dispatch": {"max_concurrent": 2, "queue_wait_minutes": 240, "daily_limit": 20}
```

- **並發**：`max_concurrent` 設定這台機器同時最多跑幾個工人。每個名額是 `runs/.slots/slot-<id>` 檔案，內容是 pid 與到期時間。下列三種情況下名額會被收回：
  - 擁有者行程已經不在；
  - 該工單已回信（pid 被重用時，靠這點認出名額已無主）；
  - 超過到期時間。

  「清掉失效名額、數名額、判斷每日上限、佔名額、記帳」這幾步，全部在同一把 `runs/.limits.lock`（O_EXCL，只持有幾毫秒）裡完成。同時開的分頁不可能都看到空位或都看到還有額度。沒有空位時，派工會在自己的分頁裡排隊：lease 的 phase 是 `queued`，每 15 秒檢查一次，超過 `queue_wait_minutes`（預設 240）就回 `deferred`。名額在派工結束時釋放，不論成功或失敗。
- **每日上限**：`daily_limit` 計算的是今天（本機日期）真正拿到工人的派工張數，紀錄在 `runs/.ledger/<日期>.txt`。達到上限就回 `deferred`，不會開工人。排隊中的派工每次重試都會重新檢查。
- **排隊中當掉**：排隊中的分頁如果當掉，下一輪 `watch` 一樣會補 `interrupted`（第 7 節）。排隊期間每 15 秒會刷新一次期限，所以還活著的排隊不會被誤補。上限檢查放在寄件者與簽章驗證之後，所以冒名的信不會占掉額度。
- **一張工單只跑一個工人**：開跑前，在同一把鎖裡建立 `runs/<id>/.owner` 持有紀錄；回信後才刪掉。持有者還活著時，第二次觸發一律拒絕，`--force` 也一樣；舊版 relay 留下、phase 為 `running`／`queued` 且 pid 還活著的 lease 也算。被拒的那次不回信、不 ack，原本那次照常回信。
- 費用：回信附上每次嘗試的用量。目前只有 hermes 會提供用量；claude／codex 的花費看各自的額度工具（SPEC §10）。

## 16. 總覽與收件端心跳（`relay status`）

```text
收件端 watch（每輪）──▶ store/hosts/<信箱>@<主機>.json：{at, version, running:[…], queued:[…]}
                        只有在超過 status.heartbeat_minutes（預設 30）或 running／queued 改變時才重寫並推送
指揮者 relay status ──▶ 各信箱 new／cur 封數、各收件端心跳距今幾分鐘、本機還沒回信的派工
```

- 心跳超過 `--stale-minutes`（預設 90 分鐘）沒更新，就標示「心跳逾時」。可能原因：收件端的排程停了，例如使用者登出，因為排程用的是 InteractiveToken；或是機器關了。
- 本機派工：`runs/*/state.json` 裡 phase 為 `running`／`queued` 的工單，連同期限與 pid 是否存活。已過期的會列為「已逾期」；下一輪 `watch` 會補一封 `interrupted`（第 7 節）。
- 退出碼：有心跳逾時或逾期工單時回傳 8，方便排程或腳本判斷；一切正常回傳 0。
- `hosts/` 跟 `mailboxes/`、`claims/`、`audit/` 一樣，由 relay 自己 commit。

## 17. 清理（`relay gc`）

```text
relay gc [--older-than 14] [--repo <repo> ...] [--apply]
```

預設只列出會刪掉什麼（dry run），加 `--apply` 才真的刪除。只處理「已回信超過 N 天」的派工；判斷依據是本機 `runs/<id>/state.json` 的 phase 為 `replied`，以 `finished` 時間計。下列項目不會碰：還在 `running`／`queued` 的派工、`new`／`cur` 的信，以及沒有本機紀錄的 worktree。

| 項目 | 條件 |
| --- | --- |
| worktree `<repo>/.relay-worktrees/<id>` | 已回信超過 N 天。repo 來源是 `--repo` 與 `dispatch.repo_roots` 底下的各個 repo。有未 commit 檔案的 worktree 會保留，除非成果已推送且已驗收，或加了 `--force` |
| 本機分支 `relay/<id>` | 同上，先刪 worktree 再刪分支。推送過的分支（commit 在 origin 上也有）直接刪；沒推送過的只用 `git branch -d`，未合併就保留，除非加了 `--force` |
| origin 上的 `relay/<id>` | 同上，而且必須是**這台機器推上去的**（state 記錄的 `pushed_ref` 與 `repo`），也必須已收到指揮者的 `accepted:`／`rejected:` note。指揮者還沒驗收的分支保留。是否刪除不受本機分支是否還在影響 |
| `runs/<id>/` | 同上 |
| 自己信箱 `done/` 的信 | `created_at` 超過 N 天。刪除後由 relay commit 並同步，git 歷史仍保留。若某張工單的 run 紀錄還留著，它的 `accepted:`／`rejected:` note 也保留，因為那是日後刪遠端分支的依據 |
| `accepts/<回信 id>/`（指揮者端驗收 log） | 最後修改超過 N 天 |

- N 必須是正數，而且不能短於 `dispatch.max_age_hours`（預設 7 天）。`max_age_hours` 不是正數時，gc 拒絕執行：這時舊的簽章信永遠會被接受，只剩本機 run 紀錄能擋重放（第 13 節）。
- 被保留的項目會標示 `kept: <git 的原因>`，不算失敗。
- git 操作會關掉 hooks。某一項失敗時只記錄在該項，不影響其他項；有失敗時退出碼為 9。

## 18. 不經人工的收信處理

三個開關都要明確設定才會生效，而且都沿用前面各節的安全檢查：

```json
// 收件端 config.json
"dispatch": {"default": {"runtime": "claude", "mode": "build", "timeout_minutes": 30}}
// 指揮者 config.json
"accept": {"auto": true, "repo_roots": ["C:\GitHub"], "push": false, "window": false}
// 任一端（預設 true）
"auto_ack_notes": true
```

- **一般 task 自動施工**（`dispatch.default`）：沒有 `--run` 的 `kind=task` 信件，會套用收件端的預設工人、模式、逾時與 TDD 設定開工，等同寄件者寫了 `--run`。
  - repo 取自信件的 `project`（`send --project <repo>`），沒有就用 `dispatch.default.repo`；兩者都沒有就回 `refused`，並提示寄件者加 `--project`。
  - 寄件者名單、簽章、`repo_roots`、上限、驗收閘門全部照第 7、9、11、13、15 節執行。
  - 收件端沒設 `dispatch.default` 時，一般 task 只會響門鈴，等人處理。
- **指揮者自動驗收**（`accept.auto`）：收到 `status: ok` 而且已推送的結果信時，`watch` 會在背景啟動 `relay accept`，流程照第 14 節，驗證通過才合併。
  - 本機 clone 的找法：原派工信的 repo 資料夾名稱，到 `accept.repo_roots` 底下找同名的 git repo；找不到就略過，記在 log。
  - 本機前置檢查不通過時（工作目錄不乾淨、分支不對），accept 不會留下任何紀錄，回信維持未結案，`relay status` 看得到，可以手動 `relay accept`。
  - `accept.push: true`：合併後把該分支推送到 origin（`relay accept --push` 同效）。
  - `accept.window: true`：在 Windows Terminal 分頁執行；預設在背景執行。
- **純通知自動結案**（`auto_ack_notes`）：relay accept 寄給收件端的 `accepted:`／`rejected:` 開頭 note 只是通知，`watch` 響完門鈴就自動 ack。

## 19. 本機一鍵建立與復原（`relay setup-local`）

```powershell
python -m agent_relay init --agent commander --clone <store repo> --branch mailbox   # 新電腦先做一次
python -m agent_relay setup-local [--auto] [--repo-root C:\GitHub] [--runtime claude --runtime codex ...]
```

依序建立或補齊下列項目。可以重複執行，每一步只補缺少的部分：

1. 指揮者的簽章金鑰；
2. 工人 home（預設 `~/.agent-relay-worker`，信箱 `claude-code`）與它的 `config.json`；
3. 工人的簽章金鑰；
4. 雙方互相信任；
5. 指揮者的 `accept` 預設值；
6. 兩個 Windows 排程。

- **不覆蓋**：設定只補缺少的鍵，你自己改過的值會保留；已有的金鑰沿用；信任清單不重複寫入；排程用同名重新註冊。
- `--auto` 會一併打開第 18 節的 `dispatch.default` 與 `accept.auto`。
- **備份策略**：
  - 設定檔與排程：由這個指令和你的參數重建，不用另外備份。不過，自己手動改過的設定值不在指令參數裡，可以抄進你自己的筆記。
  - 私鑰：**不備份，也不進 git**。它們沒有加密任何資料，只用來證明信件是誰寄的；遺失或換電腦時，重跑這個指令會產生新金鑰並重新互信。舊金鑰簽的信，在第 13 節的 `max_age_hours` 內仍會被當成有效，所以懷疑私鑰外洩時，先刪掉 `allowed_signers` 裡對應的那一行，再重跑。
