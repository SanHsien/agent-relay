# 路線圖

> 本 repo 後續工作的單一真相源。做完一項，就在同一個 commit 刪掉那一列；新工作只加在這裡。
> 現況段的數字是 2026-10-29 依 `main` `a92207cb5833` 查的快照，開工前先重查 GitHub。

## 現況
多 Agent 寄信、派工、回報的協定（`SPEC.md`）與參考 CLI（`python -m agent_relay`），file＋git mailbox，Windows 原生；功能與實測紀錄對照見 `README.md:77-99`。2026-10-29 查：open issue／PR 0，Dependabot 與 code scanning open 0，`main` 上 CI、CodeQL、Dependency freshness 最近一次皆 success，從未發過 release（2026-10-29 `gh release list -R SanHsien/agent-relay`、`gh run list -R SanHsien/agent-relay -b main`）。卡點是政策：`--run agy` 工人與 `advise` 的低複雜度路線仍以 `agy --print` 為首選（`agent_relay/advisor.py:47-48`、`agent_relay/dispatch.py:388`），與維護者 2026-10-27 的 Antigravity 個人訂閱政策衝突。

## 近期：可以直接動手
| 項目 | 為什麼 | 驗收方式 | 建議執行者 | 證據 |
| --- | --- | --- | --- | --- |
| 依政策停用 `agy` 工人：`send --run agy`／`dispatch.default=agy` 明確拒絕並說明政策；`advise` 的 `ROUTES` 移除 agy（low 路線改以 `claude haiku` 為首）；README 功能地圖與 SPEC 第 7 節同步，並寫明 Antigravity 的派法改為 `send --to antigravity --kind task --project <repo> --ref antigravity-session:new`（Desktop App 開新對話、用自己的登入執行） | 維護者 2026-10-27 政策：第三方 agent 不得經 `agy -p` 使用 Antigravity 個人訂閱；派工給 Desktop App 本身視同人下指令，是目前唯一被允許的 Antigravity 派法 | 新測試：`--run agy` 回 `DispatchError` 且訊息含政策理由；`advise` 對任何複雜度都不回 agy；`python -m unittest discover -s tests` 綠；`python tools/check_doc_parity.py` 綠（中英文件同步） | Claude Sonnet | `agent_relay/dispatch.py:46`、`:370`、`:388`；`agent_relay/advisor.py:47-51`；`README.md:83` |
| `gc` 解析 `created_at` 在 Python 3.10 也接受非 3／6 位小數秒；CI 加跑 3.10 | `requires-python = ">=3.10"`，但 3.10 的 `datetime.fromisoformat` 只吃 3／6 位小數，CI 只跑 3.12 所以測不到 | 新測試餵 `2026-10-21T13:39:57.1Z` 等格式；CI 矩陣含 3.10 且綠 | Codex | `agent_relay/gc.py:119`；`pyproject.toml:5`；`.github/workflows/ci.yml`；解析修正與回歸測試已進 main（2026-10-29）；CI 尚未加 3.10，因為 3.10 全套有三項既有失敗：`tests/test_gates.py` 兩項（`agent_relay/gates.py` 的 `tomllib` 是 3.11+）與 `test_dispatch.ArgvTests.test_cortex_builder_used_when_available`，要先修好這三項才能加 3.10 並刪本列 |

## 中期：等條件成立再做
| 項目 | 觸發條件 | 驗收方式 | 建議執行者 | 證據 |
| --- | --- | --- | --- | --- |
| 恢復 agy 工人，改走正式 API 計費 | 維護者改用 `GEMINI_API_KEY` 或 Vertex，且有不經個人訂閱 CLI 的呼叫方式 | 新 runtime 只讀環境變數中的 API 金鑰、不讀 App／CLI 登入狀態；測試涵蓋未設金鑰時拒絕；SPEC 與 README 同步 | Claude Sonnet | `agent_relay/dispatch.py:46` |
| `advise` 的路由表跟上 agent-advisor 的 lane 定義 | agent-advisor 改了 lane（模型分層或判準）；判準由 agent-advisor 擁有，本 repo 只是消費者 | `ROUTES`／`HIGH_*`／`LOW_*` 與 agent-advisor 新版一致，docstring 註明對照的 agent-advisor commit；`tests` 綠 | Claude Sonnet | `agent_relay/advisor.py:1-15` |
| `repo_id` 正規化 `ssh://git@host/x/y` 與 `git@host:x/y` 為同一 id | 實際出現兩台機器用不同 remote 寫法導致鎖或 claim 對不上 | 新測試：兩種寫法算出相同 id；舊 claim 的相容讀取有測試 | Claude Opus | `agent_relay/locks.py:120` |
| 收掉 gitsync 剩下的窄競態（第三位等待者在搬走與放回之間建鎖） | 實際出現雙寫入 | 有重現測試，修後通過；與 `locks._LocalLock` 採同一做法 | Claude Opus | `agent_relay/gitsync.py`；`agent_relay/locks.py` |

## 刻意不做
| 不做什麼 | 理由 | 什麼情況下重新考慮 |
| --- | --- | --- |
| 在本 repo 自訂風險分流判準（`solo`／`delegate`／`audit`／`full`） | 判準歸 agent-advisor；本 repo 的 `advise` 只負責「誰有額度」並沿用其 lane | agent-advisor 停止維護 |
| 在 `dispatch.verify` 內自寫完成宣稱攔截或品質門檻 | 攔截歸 harness-guard、量化門檻歸 ai-quality-gates、執行期政策歸 agent-governance-toolkit（目前經 `AGENT_GOVERNANCE_PATH` 載入）；本 repo 只負責跑驗收指令並把結果帶回信件 | 上述 repo 無法以 CLI 或 import 方式被呼叫 |
| 自寫工人指令組裝而不用 agent-cortex 的 launcher | cortex 的 launcher 已加固權限模式；本 repo 只保留同樣保守的內建備援 | cortex launcher 介面變到無法 import |
| 把 `--ref antigravity-session:new`（sidecar 經 `agentapi` 在 Desktop 開新對話）改為拒絕 | 維護者 2026-10-27 的界線：派工給 Antigravity Desktop、由 App 用自己的登入執行視同人下指令；這是被允許、也在用的派法（repo-fleet-ops `docs/PLAYBOOK.md` §8）。不做的只有規避偵測 | 維護者改變這條界線 |
| 使用 `bypassPermissions`、`--dangerously-*`、`--yolo` 類旗標派工 | 安全邊界（`SPEC.md` 第 7 節） | 不重新考慮 |

## 維護方式
自有 repo：依賴由 Dependabot（github-actions、pip 分組）與每週 `Dependency freshness` 追蹤，合併前 `python -m unittest discover -s tests`、`tools/check_doc_parity.py`、`tools/check_links.py` 全綠。中英文件（`README`／`SPEC` 的 `.en.md`）同一個 commit 改。尚未發版，版本號在 `pyproject.toml`。
