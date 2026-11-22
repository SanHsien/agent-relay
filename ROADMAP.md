# 路線圖

> 本 repo 後續工作的單一真相源。做完一項，就在同一個 commit 刪掉那一列；新工作只加在這裡。
> 現況段的數字是 2026-10-29 依 `main` `a92207cb5833` 查的快照，開工前先重查 GitHub。

## 現況
決策問答 T-20261114-05 的選項代號驗證、整批回答預檢與已結案保護已提交為 `88addb6`；依整合者沙盒外驗收回報，Python 3.14 與 3.11 各 481 tests OK、doc parity PASS、links 0 broken、mutation 21/21 killed。第二輪小修補上單行 note、Unicode `Cf` 代號拒絕與兩項 mutation 回歸；驗證範圍見 [修正證據](docs/evidence/2026-11-14-question-fixes.md)。協定與操作流程見 `SPEC.md` 第 1 節。
2026-11-20 store 同步鎖卡 5 分鐘已修：Windows 上等待者 `read_text` 鎖檔的瞬間，持有者的 `unlink` 會 WinError 32，`gitsync._release` 吞掉例外後鎖檔留著；`read` 的 pull 留下鎖，接著自己的 sync 等自己 `LOCK_WAIT_S`（300 s）。現在 `_release` 遇 `PermissionError` 重試最多 5 秒，`_acquire` 把寫著自己 pid 的鎖當殘留處理；回歸測試在 `tests/test_locks.py` `SyncLockTests`。
2026-11-21 門鈴與閘門補強（依 ai-inbox `docs/always-on-relay-gap-check.md` §4 與 `docs/ccc-mcp-architecture-review.md` §5 逐項對照程式後實作）：門鈴 `min_priority` 門檻（R1）、同一輪多封合併摘要（R2，toast 預設合併、command 要明寫 `batch`）、失敗門鈴補響有上限與退避（R4，要明寫 `retries` 才開，狀態檔壞掉時整輪不響）、驗證指令跑 0 個測試回 `unverified`（不是 `ok`）；規則見 `SPEC.md` 第 4、11 節。R3、R5–R9 列在中期。
2026-11-22 Antigravity 投遞監看補強（兩封 `--project <裸 repo 名>` 的信在 new/ 停了 3 小時以上，sidecar 回 idle、status 全綠）：寄件端拒絕非絕對路徑的 `--project`（`861c413`）；sidecar 因政策略過時輸出一次 `skipped` 並在 `app-delivery/skipped/` 留原因；`relay status` 列出超過 `--undelivered-minutes`（預設 30）沒有通知收據的 Antigravity 信、退出碼 8；同一 home 只留最新的 sidecar（`sidecar.pid`，舊的自行退場）。規則見 `SPEC.md` 第 16、20 節。
多 Agent 寄信、派工、回報的協定（`SPEC.md`）與參考 CLI（`python -m agent_relay`），file＋git mailbox，Windows 原生；功能與實測紀錄對照見 `README.md:77-99`。2026-10-29 查：open issue／PR 0，Dependabot 與 code scanning open 0，`main` 上 CI、CodeQL、Dependency freshness 最近一次皆 success，從未發過 release（2026-10-29 `gh release list -R SanHsien/agent-relay`、`gh run list -R SanHsien/agent-relay -b main`）。維護者 2026-10-27 Antigravity 個人訂閱政策已落實：`--run agy` 工人與 `dispatch.default=agy` 已明確依政策拒絕，`advise` 路由已移除 agy，Antigravity 派法統一走 Desktop App 會話（`python -m agent_relay send --to antigravity --kind task --subject <主旨> --project <repo> --ref antigravity-session:new`）。

## 近期：可以直接動手
| 項目 | 為什麼 | 驗收方式 | 建議執行者 | 證據 |
| --- | --- | --- | --- | --- |

## 中期：等條件成立再做
| 項目 | 觸發條件 | 驗收方式 | 建議執行者 | 證據 |
| --- | --- | --- | --- | --- |
| `agent_relay/dispatch.py:210` 的 `fromisoformat` 沒有小數正規化也沒 `try`，Python 3.10 下 `.1+00:00` 會丟 `ValueError` | 呼叫路徑確認會碰到（`max_age_hours` 檢查收到非 3／6 位小數秒的 `created_at`） | 新測試餵 `2026-10-21T13:39:57.1Z` 等格式，3.10 與 3.12 都通過；壞值走 `DispatchError` 而非未處理例外 | Claude Sonnet | `agent_relay/dispatch.py:210`；`agent_relay/dispatch.py:1373`（同樣寫法） |
| 恢復 agy 工人，改走正式 API 計費 | 維護者改用 `GEMINI_API_KEY` 或 Vertex，且有不經個人訂閱 CLI 的呼叫方式 | 新 runtime 只讀環境變數中的 API 金鑰、不讀 App／CLI 登入狀態；測試涵蓋未設金鑰時拒絕；SPEC 與 README 同步 | Claude Sonnet | `agent_relay/dispatch.py:46` |
| `advise` 的路由表跟上 agent-advisor 的 lane 定義 | agent-advisor 改了 lane（模型分層或判準）；判準由 agent-advisor 擁有，本 repo 只是消費者 | `ROUTES`／`HIGH_*`／`LOW_*` 與 agent-advisor 新版一致，docstring 註明對照的 agent-advisor commit；`tests` 綠 | Claude Sonnet | `agent_relay/advisor.py:1-15` |
| `repo_id` 正規化 `ssh://git@host/x/y` 與 `git@host:x/y` 為同一 id | 實際出現兩台機器用不同 remote 寫法導致鎖或 claim 對不上 | 新測試：兩種寫法算出相同 id；舊 claim 的相容讀取有測試 | Claude Opus | `agent_relay/locks.py:120` |
| 查明 2026-11-20 約 19:10 sidecar（`agent_relay.antigravity`）持有同步鎖、擋住寄信的原因 | 修好 `_release` 後，仍再次出現「存活、沒有 git 子行程、不是自己」的行程持鎖超過 `LOCK_WAIT_S` | 當下記錄鎖檔內容與 mtime、持鎖行程命令列與 Python stack；定位後補重現測試再修 | Claude Opus | `agent_relay/gitsync.py`（`_release`、`_acquire`）；`agent_relay/antigravity.py:42`、`:126` |
| 收掉 gitsync 剩下的窄競態（第三位等待者在搬走與放回之間建鎖） | 實際出現雙寫入 | 有重現測試，修後通過；與 `locks._LocalLock` 採同一做法 | Claude Opus | `agent_relay/gitsync.py`；`agent_relay/locks.py` |
| R3 自動結案的通知信（`accepted:`／`rejected:` note）不響門鈴：把 `automate()` 的判斷移到按門鈴之前，或讓這類信只寫 log | 指揮者 home 加了手機 `command` 門鈴，或 toast 被這類信洗版到維護者抱怨 | 新測試：`auto_ack_notes` 開著時這類信不呼叫任何對人門鈴、仍被 ack；關掉時照舊響 | Claude Sonnet | `agent_relay/cli.py` `watch_once()`、`automate()`；ai-inbox `docs/always-on-relay-gap-check.md` §4 |
| R5 選填來源事件鍵 `send --dedupe-key`：同寄件者同鍵的信已存在（含 `done`）就不投遞、回傳原 id | 排程、hook 或失敗重試的自動寄信實際出現重複投遞 | 新測試：同鍵寄兩次只有一封；舊版用戶端讀得到新信封；簽章範圍含新欄位；SPEC 第 1、2 節中英同步 | Claude Opus | `agent_relay/envelope.py`、`agent_relay/store.py` `deliver()`、`agent_relay/cli.py` `send` |
| R6 提問期限改機器可讀；`relay status` 列逾期未答、`watch` 對逾期題目補響一次 | 有提問因為沒人注意期限而逾期造成延誤 | 新測試：逾期題目出現在 status、只補響一次；舊的自由文字期限仍讀得到 | Claude Sonnet | `agent_relay/envelope.py`（question 欄位）、`agent_relay/status.py`、`agent_relay/cli.py` `watch_once()` |
| R7 同一討論串只跑一個工人的選項（預設維持並行） | 工單之間實際出現先後相依、並行導致衝突 | 新測試：開啟時同 thread 第二封等第一封結束；關閉時照舊並行 | Claude Opus | `agent_relay/dispatch.py` `take_task()`（比照 `.owner`） |
| R8 中斷續跑：`interrupted` 回信附保留的 worktree 與分支，重寄可指定從該分支接著做 | 中斷後從頭重做的成本實際造成困擾，且先定好半成品可信度的判準 | 新測試：重寄指定分支時工人從該分支開工；未指定時照舊從 base | Claude Opus | `agent_relay/dispatch.py` `reap_interrupted()`、`make_worktree()` |
| R9 「工人需要人決定」的書面慣例：`needs-input` 結果加一封 `question`，指揮者 `answer` 後同討論串重派 | 工人實際卡在需要人決定、又沒有既定回法 | 只改 SPEC（中英）與派工信範本；不改執行流程 | Claude Sonnet | `SPEC.md` 第 6 節、`docs/templates/` |

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
