# Windows 11 原生：開新 Session／代理網關／終端復用 相容性報告

- 日期：2026-10-20
- 對應任務：T-20261018-15（調研部分）
- 測試環境：Windows 11 Pro（原生，非 WSL），Git Bash 與 PowerShell 7
- 範圍：只做實跑與記錄，沒有安裝任何新軟體、沒有改任何 repo 程式碼

## 一句話結論

| 工具 | 角色 | 結論 |
|------|------|------|
| herdr | 終端復用（多 pane、狀態感知） | Windows 原生可用，pane 分割／下指令／讀輸出實測通過；還沒用真 agent 驗證狀態感知 |
| opencodex（ocx） | 代理網關 | 正在本機跑，`/healthz` 與 `/v1/models` 實測 200；但代理版本落後 CLI，背景服務狀態異常 |
| magpie | 代理網關（另一選項） | 有 Windows 原生單檔 binary，`version`／`--help` 實跑通過；尚未實際轉發請求 |
| cortex（agent-cortex） | 控制平面／開新 Session | CLI 可執行，但 `doctor` 3 項 FAIL（環境變數沒設），尚未可直接派工 |
| worktree_tasks.py | 派工前範圍檢查 | `--help` 通過，純 Python 標準庫 + git，Windows 無障礙 |

---

## 1. herdr（終端復用）

**版本**：`herdr 0.9.1`（stable channel，protocol 22）。安裝於使用者目錄 `.herdr\packages\standalone\releases\0.9.0-preview...-x86_64-pc-windows-msvc\`，是 Windows 原生（msvc）build，已在 PATH。

**實跑指令與輸出摘要**

```
$ herdr --version
herdr 0.9.1

$ herdr status
client: version 0.9.1, channel stable, protocol 22
server: status: not running      # 沒開時的正常狀態
socket: %APPDATA%\herdr\herdr.sock

$ herdr server &                  # 啟動無介面 server
$ herdr workspace create          # 回 JSON，帶 workspace_id / root_pane
$ herdr pane split w9:p1 --direction right
  -> {"type":"pane_info", "pane":{"pane_id":"w9:p2", ...}}   # 成功分出第二個 pane
$ herdr pane run wA:p1 "echo herdr-win-test"
$ herdr pane read wA:p1
  PS C:\Users\...> echo herdr-win-test
  herdr-win-test                  # 指令真的在 pane 的 PowerShell 裡跑，輸出可讀回
$ herdr agent list
  {"agents":[]}                   # 沒跑 agent 時為空
$ herdr server stop               # 測完停掉，status 確認 not running
```

**能力（實測 + 官方 skill 說明）**

- 結構：workspace > tab > pane，所有操作都有 CLI（`workspace`、`tab`、`pane`、`agent`、`worktree`、`notification`、`session`）。回傳是 JSON，適合腳本與 agent 呼叫。
- 多 pane：分割、聚焦、縮放、搬移、關閉均有指令；實測分割與新建 workspace 成功。
- 遠端控制 pane：`pane run`、`send-text`、`send-keys`、`read`、`wait-output` 可由另一個程序驅動，實測 run + read 通過。
- 狀態感知：官方說明 agent 會被辨識為 `idle`／`working`／`blocked`／`done`／`unknown`，`herdr agent wait` 可等狀態。**本次沒有在 pane 裡真的跑 claude／codex，這部分只有文件依據，未實測。**
- 持久 session：`--session <name>`、`session attach`，server 獨立於 UI 存活（實測 server 可無介面啟動）。
- 附帶：SSH 遠端機器（`--machine`／`--remote`）、`worktree` 子指令、`integration install`（內建 agent 整合）。

**Windows 限制與觀察**

- server 不會自己常駐：`herdr status` 顯示 not running，要先啟動 `herdr`（TUI）或 `herdr server`。沒有看到 Windows 開機自啟機制，需自己用工作排程器處理。
- pane 預設 shell 是 PowerShell，不是 Git Bash；要 Bash 需自行指定。
- 路徑與 socket：Windows 下用 `%APPDATA%\herdr\herdr.sock`，有 IPC 通道，但跨 Git Bash／PowerShell 傳 pane id 要注意引號（本次一開始把 `--pane` 當選項用，錯了，正確是位置參數）。
- 目前裝的是 preview 目錄但回報 stable channel 0.9.1，升級路徑（`herdr update`）未測。
- 純 TUI 互動（快捷鍵、滑鼠）沒測，因為 agent 環境無法操作互動終端。

---

## 2. opencodex（ocx，代理網關）

**版本**：CLI `opencodex 2.88.0`（`C:\Program Files\OpenCodex\ocx.exe`，在 PATH）；**正在跑的代理是 2.82.0**。

**實跑指令與輸出摘要**（金鑰、帳號 email 已遮掉）

```
$ ocx --version
opencodex 2.88.0

$ ocx status
Proxy: running (PID 14208)
Health: http://127.0.0.1:10100/healthz ok (live)
Dashboard: http://localhost:10100/
CLI 2.88.0 does not match the running proxy 2.82.0 ...  (警告：代理比 CLI 舊)
Service: installed, running, not viable            (背景服務異常)
Restart safety: AT RISK after restart              (重開機後可能起不來)
OAuth logins: anthropic = logged in；其餘 provider = not logged in

$ ocx doctor
路徑檢查 ok；Codex runtime 0.163.0-alpha.10.2 ok；
另有 1 項 "AT RISK after restart" 警告

$ curl http://127.0.0.1:10100/healthz
{"status":"ok","service":"opencodex","version":"2.82.0","uptime":477581,"port":10100,...}   HTTP 200

$ curl http://127.0.0.1:10100/v1/models
HTTP 200，0.3 秒，回傳 185 個模型 id（gpt-6-astra、gpt-6.1-sol 與各 effort 變體等）
```

**代理轉發測試**

- 端點：`http://127.0.0.1:10100`（預設只綁迴環位址 127.0.0.1，本機端不需認證；設 `OPENCODEX_API_AUTH_TOKEN` 後才要 token）。
- 只送最小請求：`GET /healthz`、`GET /v1/models`，皆 200。沒有送任何計費的 completion 請求，沒有請求 key。
- 證明力說明：`/v1/models` 是代理依各 provider 設定彙整出來的清單，證明代理程序活著、設定載入成功、對外 API 面可用。**它不等於「上游模型呼叫成功」**——真正的轉發（聊天請求）因避免花費沒送。本機既有的 `ocx-*` 子 agent 也是走這個代理路由，可視為旁證，但本次沒有獨立重測。
- 支援的 API 型態（取自模型清單欄位）：`chat_completions`、`responses`、`anthropic_messages`，所以 Claude 與 Codex 兩邊都能接。

**能力**：多 provider 集中（`provider`、`account`、`models`、`alias`、`combo`、`route`）、儀表板、用量與費用（`ocx usage`）、日誌（`ocx logs`）、診斷（`ocx doctor`）、背景服務（`ocx service`）。

**Windows 限制與觀察**

- 代理版本 2.82.0 落後 CLI 2.88.0，需 `ocx service restart` 或依 Desktop 方式重啟才會換新版；本次不動，以免影響其他正在用它的 session。
- 背景服務「installed-but-unhealthy」、重開機安全性「AT RISK」：目前是靠手動／桌面 app 起來的程序在撐，重開機後不保證自動恢復。
- Windows 上 package handoff 被停用，ocx 命令由使用者 PATH 第一順位決定（目前指到 Program Files 版）。
- 7 個 OAuth provider 中只有 1 個登入，其餘模型多半靠 API key 設定。

---

### 補測：真實轉發（2026-10-20 07:09）

```text
$ curl -X POST http://127.0.0.1:10100/v1/messages     -d '{"model":"anthropic/claude-haiku-5-5","max_tokens":8,"messages":[{"role":"user","content":"Reply with exactly: relay-ok"}]}'
HTTP 200 1.19s
model: anthropic/claude-haiku-5-5  stop_reason: end_turn  usage: input 49 / output 7 tokens
content: ["relay-ok"]
```

一次極小的聊天請求經 opencodex 轉到上游並拿回正確回答，**轉發實測通過**（上面「沒送聊天請求」的限制以此補上）。


## 3. magpie（代理網關，另一選項）

**版本**：最新 release `v0.1.1294`（2026-10-19），專案語言 Go，MIT。本機沒裝，只下載到暫存資料夾試跑。

**實跑**

- 官方 release 有 Windows 原生檔：`magpie-cli-windows-amd64.exe`（約 50 MB）、`magpie-windows-amd64.exe`（桌面版）、也有 arm64。
- 下載 `magpie-cli-windows-amd64.exe` 與 `SHA256SUMS` 到 scratchpad 的 `magpie\` 資料夾，雜湊比對一致（`6bc5e916...a93b37`）。
- `magpie-cli-windows-amd64.exe version` 輸出：`magpie 0.1.1294`，exit 0。
- `--help` 正常列出指令。重點指令：
  - `magpie ls`：列出本機偵測到的 agent
  - `magpie claude <provider/model>`、`magpie codex <provider/model>`：切換 agent 用的模型
  - `magpie web`／`magpie tui`：瀏覽器介面／終端介面，並同時提供 gateway
  - `magpie provider add <preset> <key>`、`magpie quota`、`magpie group add ... routing=smart`（額度用完自動換帳號／模型）
- 本機有 Go 1.27.0，所以 `go install github.com/yetone/magpie@latest` 理論上可行；但已有現成 Windows binary，且規定不安裝東西，**沒有執行 go install**。
- 依 README，閘道位址預設 `http://127.0.0.1:3425`（OpenAI 相容 `/v1`、Anthropic、Gemini 三種入口）。

**未測項目**：沒有加 provider、沒有啟動 gateway、沒有轉發請求。原因：這些會寫入設定檔並讀取本機 Claude／Codex 登入檔（`magpie accounts discovery` 預設會掃描登入檔），不符合「只讀、不裝、不碰金鑰」的範圍。試跑前後確認 `%USERPROFILE%\.magpie` 與 `%APPDATA%\magpie` 都沒有被建立。

**Windows 限制與觀察**

- README 標示支援 Windows，但 Windows 的主要使用方式偏向桌面 app／選單列常駐；CLI 版可用，但 `magpie` 預設行為是開 App。
- 專案很新、更新極快（單日多個 release），版本穩定度需觀察。
- 預設會讀 Claude Code／Codex 的登入檔來發現訂閱帳號，需要時可用 `MAGPIE_NO_ACCOUNTS=1` 關閉。
- 與 opencodex 功能高度重疊（多 provider、換模型、gateway）；多出的賣點是「額度用完自動換下一個帳號」。

---

## 4. agent-cortex（`cortex`，控制平面）

**版本**：`cortex 0.1.8`（`%USERPROFILE%\.local\bin\cortex`）。

**實跑**

- `cortex --version`：`cortex 0.1.8`。
- `cortex --help`：列出 `status`、`ready`、`fanout`、`tick`、`complete`、`dispatch`、`doctor`、`inspect`、`run`、`service` 等指令。（用 Git Bash 預設編碼會顯示亂碼，設 `PYTHONUTF8=1` 才正常。）
- `cortex doctor` 摘要：

```
FAIL preflight: PSC_PREFLIGHT_CMD is required ...
PASS model-identities: runtime-validated schema v3 with canonical agy identity
WARN claude-executable: Claude executor is not selected
WARN review-sandbox: missing required executable(s): bwrap,socat,srt
FAIL service-paths: managed bootstrap environment is invalid
WARN repo-identity: PSC_REPO_ROOT not set
PASS monitor-state: durable state root is writable/creatable
FAIL monitor-socket: production Monitor config did not resolve a socket path
WARN gh-auth / gh-permissions / auto-label / agy: live probe skipped
```

**能力**：`coordinator/launcher.py` 會依 claude／codex／copilot／agy 組 headless 參數，並用 `--worktree` 把每個工作隔離到獨立 git worktree；Windows 原生可跑。

**限制**：3 項 FAIL 都是「環境變數／服務路徑沒設定」，不是程式壞掉；要用於派工，得先設定 `PSC_PREFLIGHT_CMD`、`PSC_REPO_ROOT` 與 monitor socket，並跑過 `cortex install service`／`bootstrap`。review sandbox 需要的 `bwrap`／`socat`／`srt` 是 Linux 工具，Windows 原生沒有，所以含 sandbox 的審查流程在 Windows 上會受限。`install service` 指令說明提到 systemd units，在 Windows 要確認是否有對應實作（本次未測）。

---

## 5. worktree_tasks.py（派工前範圍檢查）

**實跑**：`python worktree_tasks.py --help` exit 0，子指令 `lint`、`up`、`check`、`down`，各自 `--help` 皆正常。

**能力**：`lint` 檢查計畫檔格式與各任務檔案範圍是否重疊；`up` 為每個任務建立 worktree 與分支；`check` 檢查各分支是否只動自己範圍內檔案、依序合併是否衝突；`down` 收尾。結束碼 0 通過／1 有問題／2 用法錯。

**限制**：只用 Python 標準庫與 git（需 2.38 以上，因為用 `git merge-tree --write-tree`），Windows 原生沒有障礙。本次只驗 `--help`，沒有拿真計畫檔跑 `lint`／`up`（會建立 worktree，屬於改動）。

---

## 6. 比較表

| 項目 | herdr | opencodex | magpie | cortex | worktree_tasks |
|------|-------|-----------|--------|--------|----------------|
| 角色 | 終端復用 | 代理網關 | 代理網關 | 控制平面 | 範圍檢查 |
| Windows 原生 | 是（msvc build） | 是 | 是（單檔 exe） | 是（Python） | 是（Python） |
| 本機狀態 | 已裝，server 平時不跑 | 已裝，代理執行中 | 未裝（暫存試跑） | 已裝，doctor 3 FAIL | 可用 |
| 本次實測 | 分割 pane、下指令、讀輸出 | healthz、模型清單 200 | version、help | version、help、doctor | help |
| 未實測 | 真 agent 狀態感知 | （已補測，見第 2 節） | gateway 啟動與轉發 | 實際派工 | lint／up／check |
| 主要風險 | server 需手動起 | 代理版本舊、服務不健康 | 新專案、會掃描登入檔 | 設定未完成、sandbox 工具缺 | 無 |
| 設定量 | 低 | 已完成 | 中 | 高 | 低 |

## 7. 建議

**開新 Session**

1. 想要「在一個畫面開多個 agent、可由腳本控制」：用 **herdr**。實測 pane 分割、下指令、讀輸出都通，JSON 輸出對 agent 友善。先用 `herdr agent start` 跑一個真 claude／codex pane，確認狀態感知，再決定要不要定案。
2. 想要「自動派工 + 每個任務獨立 worktree」：**cortex** 的設計最完整，但 doctor 的 3 項 FAIL 要先補環境設定；在那之前，用 **worktree_tasks.py** 做範圍檢查加手動開 pane（herdr）是最快可落地的組合。

**代理網關**

- 先用 **opencodex**：它已經在跑、Claude 與 Codex 都支援、既有 `ocx-*` 子 agent 已依賴它。但要先處理兩件事：重啟成與 CLI 同版（2.88.0），並修好背景服務，否則重開機後會掉線。
- **magpie** 當備案：Windows binary 可用，額外有「額度用完自動換帳號」。但與 opencodex 重疊，建議先不並用；若要評估，另開時間在隔離的 profile 下實測 gateway 轉發，並先關掉帳號掃描。

## 8. 沒做成或未驗證的項目

| 項目 | 狀況 | 原因 |
|------|------|------|
| opencodex 實際聊天轉發 | 已補測 | 一次 8 token 請求，HTTP 200，回 `relay-ok` |
| magpie gateway 轉發 | 沒做 | 會寫設定並掃描登入檔，超出範圍 |
| magpie `go install` | 沒做 | 已有現成 Windows binary，且不安裝東西 |
| herdr 真 agent 狀態感知 | 沒做 | 需要在 pane 內啟動 claude／codex，屬互動式 |
| cortex 實際派工 | 沒做 | `doctor` 回報 `PSC_PREFLIGHT_CMD is required` 與 `production Monitor config did not resolve a socket path` |
| worktree_tasks `lint`／`up` | 沒做 | `up` 會建立 worktree，屬改動；只驗 `--help` |
| 第一次 `herdr pane run --pane ...` | 失敗，已改正 | 原文 `pane --pane not found`、`unknown option: --pane`；正確用法為 `herdr pane run <PANE_ID> <COMMAND>` |
