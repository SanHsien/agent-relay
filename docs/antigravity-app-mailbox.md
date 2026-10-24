# Antigravity App：既有會話自動收信

這是 **Antigravity App 的既有會話** 接法。它用官方 sidecar 與 `agentapi send-message`，不把 App 的會話 ID 交給 `agy --conversation`：CLI 與 App 使用不同的資料目錄，CLI 可能回 `trajectory not found`。

```text
commander send -> 私人 store / antigravity / new
                                      |
App sidecar（每 5 分鐘 pull）<----------+
    |
    +-> 檢查寄件者、簽章、期限、目的會話／專案
    +-> agentapi send-message -> 指定既有 App 會話
                                 read -> 施工／回報阻擋
                                 result -> commander
                                 ack -> done
```

## 官方依據與適用邊界

- [Sidecars](https://antigravity.google/docs/sidecars)：App 管理背景程式生命週期，並提供 `agentapi send-message <conversation_id> <prompt>`。sidecar 定義在 `~/.gemini/config/sidecars/<id>/sidecar.json`，由 `~/.gemini/config/config.json` 的 `sidecars.<id>.enabled` 啟用。
- [Hooks](https://antigravity.google/docs/hooks)：現行 App／CLI／IDE 支援 `SessionStart` 與 `PreInvocation`，使用 `conversationId`、`workspacePaths` 與 `injectSteps`，不是 Claude 的 `cwd`／`hookSpecificOutput`。不要直接套用 Claude hook，也不要把所有 hooks 改成另一種格式。
- 本接法不需要增加或修改 hooks。原有 guard、停用設定及工人權限維持原值。
- App 必須開著，sidecar 才會執行。這不是關機後也能喚醒 App 的 Windows 服務。
- `relay send --run agy` 是另一條路：開**新**的 CLI 工人，仍依 SPEC §7 使用 `plan + sandbox`。不適用本頁的既有會話收信。
- 若已經在 Antigravity App 會話內執行，且希望在 **Antigravity Desktop 左側對話列表直接建立一個使用者看得見的頂層新對話**（`nestingDepth: 0`）派工，則不走 `invoke_subagent`（那只會建立 `nestingDepth: 1` 的子面板對話），也不走 `agentapi.bat`（避免 `cmd.exe` 截斷換行），而是直接呼叫原生 `<App 的 language_server.exe> agentapi new-conversation --title="<標題>" "<prompt>"`，由回傳 JSON 取得 `conversationId` 附上 `[<標題>](conversation://<id>)` 連結，並讓新對話完工後以 `send_message` 工具回報主對話。
- 既有會話沿用自己的工作目錄，不自動建立獨立 worktree。多個 Agent 共用 checkout 時，可能有人把別人的未提交檔案一起提交；需要並行施工時，先為收件會話建立專用 worktree，並將 `repos` 綁定到該目錄。通知與簽章不能代替檔案範圍隔離。

## 1. 建立獨立收件端 home

以下以 `C:\GitHub\agent-relay` 安裝路徑為例；所有 JSON 路徑請換成實機路徑。不要把私人 store URL 或私鑰寫進公開文件。

建立 `~/.agent-relay-antigravity/config.json`；`store` 指向本機既有 mailbox clone：

```json
{
  "agent": "antigravity",
  "store": "C:/path/to/local/mailbox-store",
  "antigravity": {
    "conversation_id": "00000000-0000-0000-0000-000000000000",
    "repos": ["C:/GitHub/target-repo", "C:/GitHub/other-repo"],
    "senders": ["commander"],
    "max_age_hours": 24,
    "agentapi_argv": ["C:/path/to/native/agentapi.exe"]
  }
}
```

`conversation_id` 由收件端明確指定，不讓信件任意選會話。`repos` 是非空清單，每個路徑都必須是既有目錄，同一個會話接受清單內任一 repo 的工單；信件的 `--project` 必須在清單內，通知文字會寫該信自己的 project 路徑（只有 session ref、沒有 project 的信，則寫範圍以信件所定義為準）。舊設定的單一字串 `repo` 仍可用，等同只有一個元素的 `repos`；`repos` 與 `repo` 同時出現會報錯。多個 repo 共用同一會話時，上方的檔案範圍隔離提醒照樣適用。寄件者空清單就不通知。金鑰與信任設定沿用 SPEC §13：

```powershell
python -m agent_relay keygen --home "$HOME/.agent-relay-antigravity"
python -m agent_relay trust commander C:/path/to/commander_ed25519.pub --home "$HOME/.agent-relay-antigravity"
python -m agent_relay trust antigravity "$HOME/.agent-relay-antigravity/keys/antigravity_ed25519.pub"
```

`trust` 第二個參數接受公鑰檔案路徑。確認指揮者與收件端互相信任；不要放寬成免簽章。

Windows 的 `agentapi` 可能是 `.bat` 包裝。本機若包裝內容是 `<App 的 language_server.exe> agentapi %*`，把 `agentapi_argv` 設成 `["完整 language_server.exe 路徑", "agentapi"]`，直接執行原生程式。**先讀實機包裝確認，不猜路徑**；不把信件文字交給 cmd.exe。

## 2. 註冊 sidecar

先執行 `python -m pip install -e C:\GitHub\agent-relay`。建立 `~/.gemini/config/sidecars/agent-relay/sidecar.json`：

```json
{
  "display_name": "Agent Relay mailbox",
  "description": "Receive signed relay tasks for an explicitly bound App conversation",
  "command": "C:/path/to/python.exe",
  "args": ["-u", "-m", "agent_relay.antigravity", "--home", "C:/path/to/.agent-relay-antigravity", "--interval", "300"],
  "restart_policy": "on-failure"
}
```

在 `~/.gemini/config/config.json` **合併**以下欄位，保留現有設定與其他 sidecars；修改前先備份：

```json
{"sidecars": {"agent-relay": {"enabled": true}}}
```

App 提供 sidecar 的 `ANTIGRAVITY_LS_ADDRESS` 與 `agentapi` 環境。一般 shell 直接執行可能回 `ANTIGRAVITY_LS_ADDRESS is not set`，不表示會話不存在；不要自行猜端點或複製驗證 token。若 App 未熱載入，於沒有其他工作的時候重開 App，再看 sidecar logs，不能只憑設定檔存在宣稱接通。

不用另裝 Windows 收信排程：sidecar 已負責每五分鐘同步與通知，避免同信箱兩套排程重複輪詢。

## 3. 寄一張工單

```powershell
python -m agent_relay send --to antigravity --kind task --project C:/GitHub/target-repo --subject "單項試驗" --body-file task.md
```

也可用 `--ref antigravity-session:<UUID>` 指定已在收件端綁定的會話；若同時有 `--project`，專案也必須相符。不加 `--run`。既有信件已有正確 session ref 時直接沿用，不重寄、不修改簽過的信封。

同一工單的修正意見可用 `--kind reply --reply-to <原信 ID>`，同樣加上 session ref 或 project；沿用相同的簽章、寄件者與目的會話檢查。這是後續訊息，不是重開一張相同工單。`note`／`result` 不會喚醒這個收件端。

通知只含信件 ID 與固定收信指令，不把標題、內文或外部指令放進程序參數。通知不會替 Agent 把信搬成已讀或結案；這兩步由收件會話執行，回信須附實際證據。簽章證明來源，不保證工作內容安全；會話仍須遵守既有權限、範圍與人工確認要求。

## 4. 最小驗收與故障處理

只沿用一張工單，驗收清冊如下，達標就停止：

1. `~/.gemini/antigravity/sidecar_data/agent-relay/logs/` 出現本次 ID 與 `status: notified`。這只代表 API 接受通知。
2. **指定會話的實際 transcript** 出現收信操作，且 mailbox 從 `new` 變 `cur`。這才是接手證據。
3. 同一 thread 收到 `kind=result` 並有驗證輸出；原信 `done`。施工失敗時也須回報原因，不把「通知成功」當「工作完成」。

本機 `~/.agent-relay-antigravity/app-delivery/<信件 ID>.json` 保存通知收據：

- `notified`：通知命令 exit 0；同信不再通知。
- `failed`：命令非零；不自動重試，先讀 App sidecar 的錯誤紀錄。
- `refused`：簽章驗證失敗，收據記錄原因；拒絕訊息只輸出一次，之後的輪次靜默略過。
- `uncertain`／`attempting`：逾時或程序中斷，可能已送達。先看指定會話，**不要盲目重送**。

先記錄 `attempting` 再呼叫 API，並利用檔案的排他建立避免兩個程序同時發同信。這是偏向避免重複施工的策略，不是 exactly-once 保證。確定未送達且排除錯誤後，才由操作者備份並移走該張收據，允許下一輪補通知；不要重寄工單。排程正常時不反覆跑手動 smoke，不另建第二張試驗資料。

單次診斷指令（須在 App sidecar 環境，會真的通知）：

```powershell
python -m agent_relay.antigravity --home "$HOME/.agent-relay-antigravity" --once
```

sidecar 每輪也會寫 `hosts/antigravity@<主機>.json` 心跳並推送（頻率見 SPEC §16），指揮者的 `relay status` 因此看得到 `antigravity@<主機>`；App 關閉、sidecar 停止後，該心跳會逾時。心跳推送失敗只在輸出的 `heartbeat_error` 記錄，不影響通知。

停用只把 `config.json` 的 `sidecars.agent-relay.enabled` 改成 `false`，保留信箱、收據與金鑰供追查。
