# agent-relay

[![CI](https://github.com/SanHsien/agent-relay/actions/workflows/ci.yml/badge.svg)](https://github.com/SanHsien/agent-relay/actions/workflows/ci.yml)

讓 Claude Code、Codex、Antigravity、Hermes 互相寄信、派工、回報的**協定規格與參考 CLI**。Windows 11 原生、只用 Python 標準函式庫；跨機器靠 git push/pull，不需要公網 IP，也不需要額外的 API key。

本 repo 也是「多 Agent 派工與驗收閉環流水線」的**總目錄**：協定放這裡，各目標 repo 只加薄薄一層轉接，並在 README 連回這裡。任務來源與驗收標準在 [ai-inbox](https://github.com/SanHsien/ai-inbox) 的〈多 Agent 派工與驗收閉環流水線〉。

- 協定規格：[SPEC.md](SPEC.md)
- 給 Agent 的操作說明：[AGENTS.md](AGENTS.md)

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

每個指令加 `--json` 輸出機器可讀格式；加 `--no-sync` 只動本機、不碰 git。

## 運作方式

```text
指揮者 ──send──▶ store/mailboxes/<worker>/new/ ──git push──▶ GitHub（私人 store repo，mailbox 分支）
                                                                  │
                                    Worker 機器排程 watch --once ◀─┘ git pull
                                       ├─ 門鈴：Windows 通知 / Telegram / dropoff 交接卡
                                       └─ Worker read → 施工 → send --kind result → ack
```

## 生態系總目錄

流水線分三個階段，每階段復用下列現有 repo。「轉接層」欄是各 repo 要加的那一層；完成後在 ai-inbox 對應任務附證據。

### Phase 1：訊息通道

| Repo | 角色 | 轉接層 | 任務 |
| --- | --- | --- | --- |
| [agentdeck](https://github.com/SanHsien/agentdeck) | Windows 系統匣、排程輪詢 | 托盤每輪檢查信箱並跳通知；排程做法被本 repo 沿用 | T-20261019-01 |
| [Private Repo: 個人記憶庫] | 信件存放處（`mailbox` 分支） | 只放信件，不改主分支 | T-20261019-01 |
| [agent-session-restore](https://github.com/SanHsien/agent-session-restore) | 會話登記與喚醒 | 依登記的活躍會話決定交接卡送到哪個專案 | T-20261019-02 |
| [line-desktop-mcp](https://github.com/SanHsien/line-desktop-mcp) | LINE 桌面版橋接 | 手機門鈴候選；它的傳送要人確認，所以目前手機門鈴走 Telegram | T-20261019-02 |

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
