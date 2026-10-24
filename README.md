[English](README.en.md) | 繁體中文

# agent-relay

[![CI](https://github.com/SanHsien/agent-relay/actions/workflows/ci.yml/badge.svg)](https://github.com/SanHsien/agent-relay/actions/workflows/ci.yml)

讓 Claude Code、Codex、Antigravity、Hermes 互相寄信、派工、回報的**協定規格與參考 CLI**。Windows 11 原生、只用 Python 標準函式庫；跨機器靠 git push/pull，不需要公網 IP，也不需要額外的 API key。

本 repo 也是「多 Agent 派工與驗收閉環流水線」的**總目錄**：協定放這裡，各目標 repo 只加薄薄一層轉接，並在 README 連回這裡。任務來源與驗收標準在 [ai-inbox](https://github.com/SanHsien/ai-inbox) 的〈多 Agent 派工與驗收閉環流水線〉。

- 協定規格：[SPEC.md](SPEC.md)
- 給 Agent 的操作說明：[AGENTS.md](AGENTS.md)
- Antigravity App 既有會話收信：[sidecar 安裝、簽章與驗收](docs/antigravity-app-mailbox.md)（不同於 `--run agy` 新開唯讀 CLI 工人）

## 快速開始

```powershell
git clone https://github.com/SanHsien/agent-relay.git
cd agent-relay

# 每台機器做一次：指定信箱名，clone store 的 mailbox 分支
python -m agent_relay init --agent claude-code --clone https://github.com/<you>/<store-repo>.git --branch mailbox

# 寄信、看信箱、讀信、結案
python -m agent_relay send --to hermes --kind task --subject "跑一次測試" --body-file task.md
python -m agent_relay inbox
python -m agent_relay read <id>
python -m agent_relay ack <id>

# 每 5 分鐘自動收信並按門鈴（Windows 排程工作）
python -m agent_relay poller install --every 5
```

新 session 自動接手（不用貼上下文）：

```powershell
python -m agent_relay context --repo C:\GitHub\my-repo     # 看交接包
# Claude Code SessionStart hook：見 SPEC.md 第 8 節
```

每個指令加 `--json` 輸出機器可讀格式；加 `--no-sync` 只動本機、不碰 git。所有指令都接受 `--home <資料夾>`，可指定使用哪一個 relay home。

### 一台電腦：指揮者＋本機工人

同一台電腦可以同時當指揮者和收件端。兩邊各用一個 home、共用同一份 store，各自有一個排程，並互相信任對方的簽章金鑰。一行指令建好，之後也能用它復原（SPEC §19）：

```powershell
python -m pip install -e C:\GitHub\agent-relay            # 任何資料夾都能執行 python -m agent_relay
python -m agent_relay setup-local --auto                   # 指揮者＋本機工人＋兩個排程；可重複執行
```

工人的政策在 `$HOME\.agent-relay-worker\config.json` 的 `dispatch` 段落（SPEC §7、§11、§15、§18）：允許的 runtime 與 `repo_roots`、`senders: ["commander"]`、`verify: {"command": "auto"}`、`push_branch`、`max_concurrent`、`daily_limit`、一般 task 用的 `default`。

日常派工與驗收（在指揮者端）：

```powershell
# 派工：起點是 origin/main（在該 repo 資料夾裡執行，--base 才解析得到；或直接給 40 位 sha），要求 TDD，限定可改的檔案
python -m agent_relay send --to claude-code --kind task --run codex --repo C:\GitHub\<repo> `
  --base origin/main --tdd --path src/ --path tests/ --subject "..." --body-file task.md
python -m agent_relay status                                   # 信箱、心跳、執行中的派工
python -m agent_relay accept <回信 id> --repo C:\GitHub\<repo>  # 在本機重跑驗證，通過才合併
```

第一張真實工單的完整紀錄見 [docs/evidence/2026-10-21-real-e2e.md](docs/evidence/2026-10-21-real-e2e.md)。

## 運作方式

```text
指揮者 ──send──▶ store/mailboxes/<worker>/new/ ──git push──▶ GitHub（私人 store repo，mailbox 分支）
                                                                  │
                                    Worker 機器排程 watch --once ◀─┘ git pull
                                       ├─ 門鈴：Windows 通知 / Telegram / dropoff 交接卡
                                       └─ Worker read → 施工 → send --kind result → ack
```

## 功能地圖

| 功能 | 指令／設定 | SPEC | 實測紀錄 | ai-inbox 任務 |
| --- | --- | --- | --- | --- |
| 寄信、收信、跨機器同步 | `send`／`inbox`／`read`／`ack`／`sync` | 1–3 | [Phase 1](docs/evidence/2026-10-20-phase1.md) | T-20261019-01 |
| 門鈴（通知、Telegram、交接卡）、排程輪詢 | `watch`、`poller install` | 4–5 | [Phase 1](docs/evidence/2026-10-20-phase1.md) | T-20261019-02 |
| 信件自動開新 Session 施工（worktree） | `send --run claude\|codex\|agy\|hermes`、`dispatch` | 7 | [Phase 2](docs/evidence/2026-10-20-phase2.md) | T-20261018-15、T-20261017-31 |
| 新 Session 自動接手（交接包） | `context`、`hooks/session_start.py` | 8 | [T-08](docs/evidence/2026-10-20-t08-handover.md) | T-20261019-08 |
| 簽出鎖、範圍檢查、打卡紀錄 | `--path`、`claim`／`claims`／`release` | 9 | [T-07](docs/evidence/2026-10-20-t07-checkout.md) | T-20261019-07 |
| 額度感知分派、斷線續跑 | `--run auto`、`advise` | 10 | [T-16](docs/evidence/2026-10-20-t16-advisor.md) | T-20261018-16 |
| 驗收閘門（TDD、禁止空殼、真實 log） | `--tdd`、`dispatch.verify` | 11 | [T-18](docs/evidence/2026-10-20-t18-gates.md) | T-20261018-18 |
| 回流升級除錯 | `dispatch.escalation` | 12 | [基準報告](docs/reports/2026-10-20-space-bunny-benchmark.md) | T-20261017-32 |
| Hermes 工人（寫入限於 worktree、回信附用量） | `send --run hermes` | 7 | [T-03](docs/evidence/2026-10-21-t03-hermes.md) | T-20261021-03 |
| 寄件者驗證（信件 SSH 簽章、senders 預設拒絕） | `keygen`、`trust`、`dispatch.require_signature` | 13 | [T-07 簽章](docs/evidence/2026-10-21-t07-signing.md) | T-20261021-07 |
| 指定施工起點、結構化回信 | `send --base`、回信 `result` 欄位 | 7 | [T-08](docs/evidence/2026-10-21-t08-base-result.md) | T-20261021-08 |
| 結果分支回傳、指揮者驗收合併 | `dispatch.push_branch`、`accept` | 14 | [T-05](docs/evidence/2026-10-21-t05-accept.md) | T-20261021-05 |
| Codex 工人（workspace-write 釘住 worktree） | `send --run codex` | 7 | [T-04](docs/evidence/2026-10-21-t04-codex.md) | T-20261021-04 |
| 並發／每日上限、防重跑 | `dispatch.max_concurrent`、`daily_limit` | 15 | [T-09](docs/evidence/2026-10-21-t09-limits.md) | T-20261021-09 |
| 總覽與收件端心跳 | `status`、`hosts/` | 16 | [T-10](docs/evidence/2026-10-21-t10-status.md) | T-20261021-10 |
| 清理 worktree／分支／run 紀錄 | `gc` | 17 | [T-11](docs/evidence/2026-10-21-t11-gc.md) | T-20261021-11 |
| 不經人工的收信處理（預設工人、自動驗收、通知自動結案） | `dispatch.default`、`accept.auto` | 18 | — | — |
| 本機一鍵建立與復原 | `setup-local` | 19 | [真實端到端](docs/evidence/2026-10-21-real-e2e.md) | — |

Windows 相容性調研（herdr、magpie、opencodex、cortex）：[報告](docs/reports/2026-10-20-windows-session-control.md)。

## 生態系總目錄

流水線分三個階段，每階段復用下列現有 repo。「轉接層」欄是各 repo 要加的那一層；完成後在 ai-inbox 對應任務附證據。

### Phase 1：訊息通道

| Repo | 角色 | 轉接層 | 任務 |
| --- | --- | --- | --- |
| [agentdeck](https://github.com/SanHsien/agentdeck) | Windows 系統匣、排程輪詢 | 托盤每輪檢查信箱並跳通知；排程做法被本 repo 沿用 | T-20261019-01 |
| [Private Repo: 個人記憶庫] | 信件存放處（`mailbox` 分支） | 只放信件，不改主分支 | T-20261019-01 |
| [agent-session-restore](https://github.com/SanHsien/agent-session-restore) | 會話登記與喚醒 | 信件寫 `--project asr:<會話名>`，門鈴從它的會話登記檔查出專案路徑再放交接卡 | T-20261019-02 |
| [line-desktop-mcp](https://github.com/SanHsien/line-desktop-mcp) | LINE 桌面版橋接 | 手機門鈴候選。它每次傳送都要人確認，不適合自動門鈴，所以手機門鈴目前走 Telegram（`command` 門鈴） | T-20261019-02 |

### Phase 2：派工契約

| Repo | 角色 | 任務 |
| --- | --- | --- |
| [agent-cortex](https://github.com/SanHsien/agent-cortex) | Windows 原生控制平面、開新 Session | T-20261018-15 |
| [opencodex](https://github.com/SanHsien/opencodex) | 通用供應商代理 | T-20261018-15 |
| [claude-code-projects-workflow](https://github.com/SanHsien/claude-code-projects-workflow) | Worktree 派工前範圍檢查與衝突預測 | T-20261018-15、T-20261019-07 |
| [agent-advisor](https://github.com/SanHsien/agent-advisor) | 派工路由與合約 | T-20261017-31、T-20261018-16 |
| [agy-staff](https://github.com/SanHsien/agy-staff) | Antigravity CLI 當執行端 Worker | T-20261017-31 |
| [claude-obsidian](https://github.com/SanHsien/claude-obsidian) | Obsidian 交接單同步 | T-20261019-08 |
| [cc-session-reader](https://github.com/SanHsien/cc-session-reader) | 會話精簡讀取 | T-20261019-08 |
| [Private Repo: 個人指揮中心] | 指揮中心 | T-20261019-08 |
| [codebase-memory-mcp](https://github.com/SanHsien/codebase-memory-mcp) | 程式碼知識圖譜 | T-20261019-07 |
| [Private Repo: 環境審計工具] | 環境審計 | T-20261019-07 |
| [codex-reset-checker](https://github.com/SanHsien/codex-reset-checker) | 額度與重置時間查詢 | T-20261018-16 |
| [codex-auto-retry](https://github.com/SanHsien/codex-auto-retry) | 限流斷線原地續跑 | T-20261018-16 |
| [Antigravity-Manager](https://github.com/SanHsien/Antigravity-Manager) | Antigravity 帳號切換 | T-20261018-16 |

### Phase 3：驗收閉環

| Repo | 角色 | 任務 |
| --- | --- | --- |
| [harness-guard](https://github.com/SanHsien/harness-guard) | 阻擋 hook（無證據的完成宣稱、紅燈提交） | T-20261018-18、T-20261019-07 |
| [ai-quality-gates](https://github.com/SanHsien/ai-quality-gates) | 可執行規格、覆蓋率與 mutation 關卡 | T-20261018-18 |
| [agent-governance-toolkit](https://github.com/SanHsien/agent-governance-toolkit) | 執行期政策與沙箱審計 | T-20261018-18 |
| [SkillSpector](https://github.com/SanHsien/SkillSpector) | 技能供應鏈掃描 | T-20261018-18 |
| [agent-advisor](https://github.com/SanHsien/agent-advisor) | 獨立審查員軌道、報錯回流再派工 | T-20261017-32 |

`[Private Repo: …]` 是私人 repo 的公開別名，真實名稱不寫在任何公開檔案裡。

## 測試

```powershell
python -m unittest discover -s tests -v
```

## 授權

MIT
