# Antigravity App 既有會話收信實測（2026-10-23）

## 範圍

在維護者 Windows 本機的既有 Antigravity App 會話，接收一張已存在的 `ai-inbox` 工單。只測同一原工單，不另開工人，不改原信，不變更既有 hooks 或執行權限。安裝方式見 [操作手冊](../antigravity-app-mailbox.md)。本紀錄不包含私人 store 名稱、位址、憑證或完整會話內容。

## 方法與結果

```text
原工單 new
   -> App 熱載入 sidecar
   -> agentapi send-message（exit 0）
   -> 原會話 relay read（exit 0，new -> cur）
   -> 原會話回覆有簽章的 result
   -> 原工單 done
```

- 原工單：`20261023T050025636Z-869a1f`，T-20261017-04。
- 13:09:47（UTC+8）：App 自動啟動 `agent-relay` sidecar，沒有重開 App。
- 同一次啟動的 sidecar log：原工單 `status: notified`、`exit: 0`。同 ID 的本機收據保存為 `notified`。
- 13:10:12：原會話 transcript 記錄 `relay read` 指令完成，exit 0；本機信箱確認原信為 `cur`。
- 13:13:23：回信 `20261023T051323313Z-5df9ae`，`reply_to` 指向原工單；指揮者以本機受信任公鑰檢查簽章，結果 `valid`。
- 工作內容另作驗收：首次報告有論文數字、引用與無來源推論問題，不能因為 CLI／CI 成功就視為內容驗收通過。已在同 thread 寄出集中修正意見，覆核後再限定三個表格／文字位置收尾，沿用同一會話，未重派原工單。這份紀錄確認通道成功，並不代替該報告的內容驗收。
- 13:15:18：sidecar 載入新版（支援同 thread 的 `reply`），修正訊息 `20261023T051435800Z-f58753` 的通知 exit 0；原會話隨後開始修正。

## 接線陷阱

- `agy --conversation <App 的會話 ID>` 回 `trajectory not found`，沒有施工；CLI 與 App 的儲存空間不同。
- 一般 shell 執行原生 `agentapi get-conversation-metadata` 回 `ANTIGRAVITY_LS_ADDRESS is not set`。由 App 管理的 sidecar 繼承官方環境後，`send-message` 才成功，不需猜端點或複製 token。
- 本機 `agentapi.bat` 只是 `language_server.exe agentapi %*` 包裝；實際設定直接呼叫原生 `.exe`，不經批次檔解析工作訊息。
- 收件端另建 home 與金鑰，雙方透過公開金鑰互信。原 App 設定與備份比較，除了新增 `sidecars.agent-relay`，其他欄位相等；不改既有 hook。

## 自動測試邊界

新增測試涵蓋正確會話／專案、拒絕未授權寄件者與無效簽章、過期信、異常日期、防止重複通知、逾時保留不確定收據、拒絕 batch 包裝，以及同 thread 後續訊息。單元測試的 API／簽章驗證使用替身；上面的真人會話紀錄才是實際接線證據。

- 本機全套 `python -m unittest discover -s tests`：299 tests、`OK (skipped=1)`，506.390 秒。
- 隨後只修改新模組的錯誤處理與後續訊息支援；最終聚焦 `python -m unittest discover -s tests -p test_antigravity.py`：11 tests、`OK`，0.740 秒。既有模組沒有變更；發布 CI 會在最終提交重新跑完整套件。

通知成功證明 API 接受訊息；`read`、回信與簽章證據才證明接手及回報。工作產出品質須另外驗收，不與通道成功混為一談。
