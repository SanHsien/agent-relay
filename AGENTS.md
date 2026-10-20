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
- 要收件端**自動開新 Session 施工**：加 `--run claude|codex|agy|hermes --repo <收件端 repo>`（見 [SPEC.md](SPEC.md) 第 7 節）。收件端要先在 config 的 `dispatch` 允許該 runtime 與 repo。不確定派給誰就寫 `--run auto`，收件端依複雜度與它的額度挑（SPEC 第 10 節）；`--path` 寫出工人可改的檔案範圍（SPEC 第 9 節）。

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
- 改協定先改 [SPEC.md](SPEC.md)，再改程式和測試；`python -m unittest discover -s tests` 全綠才提交。
- 本 repo 公開：私人 repo 一律寫 `[Private Repo: <別名>]`，不寫真名。
