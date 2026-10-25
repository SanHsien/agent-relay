# Space Bunny 實測基準報告（2026-10-20）

對應 [ai-inbox](https://github.com/SanHsien/ai-inbox) 任務 T-20261017-32。DoD：「產出 Space Bunny 實測基準報告與回流除錯 Prompt 範本文檔」。回流除錯範本見 [docs/templates/escalation-prompt.md](../templates/escalation-prompt.md)。

## 結論先講

- **維護者決定（2026-10-21）**：不使用 MiniMax 這類中國服務。本報告的 MiniMax 段落只留作查證紀錄，不再補測，也不放進任何路由。
- **Space Bunny 已經不能照原計畫用**：它是 OpenRouter 的匿名測試模型 `stealth/space-bunny-alpha`，後來公布就是 **MiniMax M3.1 Flash**；免費的匿名期已結束，`space-bunny-alpha` 目前沒有可用端點，OpenCode 上的 `space-bunny-free` 也隨之失效。
- **2026-10-25 實測複查（免費版下架確定）**：2026-10-20 查證時存在的免費版 `minimax/minimax-m3.1-flash:free` 目前**已完全下架**。經 OpenRouter API 實測檢索，目前僅存付費端點 `minimax/minimax-m3.1-flash`（`pricing.prompt = 0.00000015`），無任何免費端點。依任務退回規範，確認免費版下架即關閉待辦。
- 「便宜工人先做、失敗再升級」的流程已在本機用同等角色的模型實測打通（下面第 3 節）。

## 1. 查證來源與時間

| 項目 | 結果 | 來源 |
| --- | --- | --- |
| Space Bunny 身分 | MiniMax M3.1 Flash | OpenRouter 模型頁 `openrouter.ai/stealth/space-bunny-alpha`（頁面寫明免費匿名期已結束） |
| `stealth/space-bunny-alpha` 端點 | 0 個 | `GET https://openrouter.ai/api/v1/models/stealth/space-bunny-alpha/endpoints`（2026-10-20） |
| 現行免費版（2026-10-20） | `minimax/minimax-m3.1-flash:free`（當時在線） | `GET https://openrouter.ai/api/v1/models`（公開清單，2026-10-20） |
| **現行狀態（2026-10-25 實測）** | **完全下架（無端點）**，僅存付費端點 `minimax/minimax-m3.1-flash` | `GET https://openrouter.ai/api/v1/models`（2026-10-25 實跑查驗） |
| 本機 opencodex 代理 | 185 個模型，沒有 MiniMax／Space Bunny | `GET http://127.0.0.1:10100/v1/models` |
| 本機金鑰 | `OPENROUTER_API_KEY`、`OPENCODE_API_KEY`、`MINIMAX_API_KEY` 都未設定 | 只檢查有無，未讀內容 |

### 1.1 2026-10-25 OpenRouter API 實測查驗命令與回傳 JSON 證據

- **查驗時間**：`2026-10-25 21:56:23 (UTC+8)`
- **實測查詢命令**：
```powershell
pwsh -NoProfile -Command '$res = Invoke-RestMethod -Uri "https://openrouter.ai/api/v1/models"; $matches = $res.data | Where-Object { $_.id -like "*space-bunny*" -or $_.id -like "*minimax*" }; $matches | Select-Object id, name, @{n="pricing_prompt";e={$_.pricing.prompt}}, @{n="pricing_completion";e={$_.pricing.completion}} | ConvertTo-Json -Depth 3'
```
- **回傳 JSON 輸出**：
```json
[
  {
    "id": "minimax/minimax-m3.1-flash",
    "name": "MiniMax: MiniMax M3.1 Flash",
    "pricing_prompt": "0.00000015",
    "pricing_completion": "0.0000006"
  },
  {
    "id": "minimax/minimax-m3",
    "name": "MiniMax: MiniMax M3",
    "pricing_prompt": "0.0000003",
    "pricing_completion": "0.0000012"
  },
  {
    "id": "minimax/minimax-m2.7",
    "name": "MiniMax: MiniMax M2.7",
    "pricing_prompt": "0.0000003",
    "pricing_completion": "0.0000012"
  },
  {
    "id": "minimax/minimax-m2.5",
    "name": "MiniMax: MiniMax M2.5",
    "pricing_prompt": "0.0000003",
    "pricing_completion": "0.0000012"
  },
  {
    "id": "minimax/minimax-m2-her",
    "name": "MiniMax: MiniMax M2-her",
    "pricing_prompt": "0.0000003",
    "pricing_completion": "0.0000012"
  },
  {
    "id": "minimax/minimax-m2.1",
    "name": "MiniMax: MiniMax M2.1",
    "pricing_prompt": "0.0000003",
    "pricing_completion": "0.0000012"
  },
  {
    "id": "minimax/minimax-m2",
    "name": "MiniMax: MiniMax M2",
    "pricing_prompt": "0.000000255",
    "pricing_completion": "0.00000102"
  },
  {
    "id": "minimax/minimax-m1",
    "name": "MiniMax: MiniMax M1",
    "pricing_prompt": "0.00000055",
    "pricing_completion": "0.0000022"
  },
  {
    "id": "minimax/minimax-01",
    "name": "MiniMax: MiniMax-01",
    "pricing_prompt": "0.0000002",
    "pricing_completion": "0.0000011"
  }
]
```
- **查驗結論**：
  1. `stealth/space-bunny-alpha` 完全下架（未返回任何包含 space-bunny 之模型）。
  2. 2026-10-20 原存之免費端點 `minimax/minimax-m3.1-flash:free` 亦已徹底下架（OpenRouter 模型清單中無任何 `:free` 後綴之 MiniMax 端點）。
  3. 目前僅保留付費端點 `minimax/minimax-m3.1-flash`（定價：輸入 `$0.00000015` / token 即 `$0.15` / 1M tokens，輸出 `$0.0000006` / token 即 `$0.60` / 1M tokens）。
  4. 依退回規範，確認免費版下架即改以 `close` 關閉任務。回流升級除錯機制已完全由 commit `66e8473` 與 Claude Haiku/Opus 實測落地。
## 2. 拿到金鑰後怎麼補測

把金鑰設在環境變數（不要寫進任何檔案或信件），跑這幾個最小探測：

```powershell
# 連線延遲與首字時間（16 token）
curl.exe -s -o NUL -w "connect %{time_connect}s ttfb %{time_starttransfer}s total %{time_total}s http %{http_code}`n" `
  -X POST https://openrouter.ai/api/v1/chat/completions `
  -H "Authorization: Bearer $env:OPENROUTER_API_KEY" -H "Content-Type: application/json" `
  -d '{"model":"minimax/minimax-m3.1-flash:free","messages":[{"role":"user","content":"Reply with: ok"}],"max_tokens":16}'
```

- **正確率**：用本報告第 3 節同一個 `parse_duration` 題目，比對它是否像 haiku 一樣只修表面。
- **1M 承載力**：送一個約 800K token 的檔案串接（例如某大 repo 的 `git ls-files | xargs cat`），在文中埋一個獨特字串再問位置，量完成時間與是否答對。免費版會限流，失敗要重試。
- **接進流水線**：relay 的 runtime 目前是 claude／codex／agy。要讓 MiniMax 當工人，最省事的路是在 opencodex 加 OpenRouter provider，再經 codex runtime 指定 model（SPEC 第 7 節）。

## 3. 本機實測：便宜工人 → 驗收失敗 → 升級強模型（同一題）

題目：`parse_duration` 有 bug（`=` 應為 `+=`），repo 已有完整測試（含「無效輸入要 raise ValueError」）。工單只說「修 `1h30m` 回傳錯誤，只修這個、改動越小越好」——典型的外包最小修補。收件端設定 `verify = python -m unittest`、`escalation = claude/opus`。

| 輪次 | 模型 | 耗時 | 回合 | 成本（Claude Code 回報） | 結果 |
| --- | --- | --- | --- | --- | --- |
| 1 | claude-haiku-5-5 | 25.6 秒 | 8 | $0.019 | 只改成 `+=`，`test_invalid_input_raises` 失敗 → 閘門 verification-failed |
| 2（升級） | claude-opus-5-5 | 34.5 秒 | 7 | $0.685 | 引用失敗證據說明根因，補上輸入驗證 → 閘門 exit 0 → commit `66e8473` |

整張派工 85 秒（含兩次驗收）。派工 id `20261020T151929609Z-7e6d40`，回信「回流升級」段：`claude/haiku：ok → 閘門 verification-failed；升級到 claude/opus`。

同一天其他便宜工人的紀錄（都有回信或 commit 紀錄；只有標「閘門」的那幾次跑過驗收閘門）：

| 工人 | 任務 | 耗時 | 結果 |
| --- | --- | --- | --- |
| claude-haiku-5-5 | calc 加 add＋測試 | 60 秒 | ok（工人自報 unittest 通過，當時未開閘門） |
| claude-haiku-5-5（acceptEdits、無 shell） | calc 加 div／neg | 30 秒 | ok（指揮者在 worktree 自跑 unittest 通過） |
| agy gemini-3.8-flash-low（agy-staff implement） | calc 加 mul＋測試 | 43 秒 | ok（主 session 自跑 unittest 通過；非 relay 派工，無回信） |
| agy（plan＋sandbox，唯讀） | 審查缺少的邊界測試 | 44 秒 | ok（唯讀，無閘門） |

## 4. 怎麼解讀

- 便宜工人在**機械、範圍清楚**的任務上一次就過，成本約 $0.02；這才是省錢的來源。
- 便宜工人在**規格比工單寫的更多**時，會只做表面（本例），這時靠驗收閘門抓出來、再交給強模型，總成本是 $0.019＋$0.685≈$0.70。這次沒有讓 opus 從頭單獨做同一題，所以「是否比直接用強模型貴」只能推估：第 2 輪是在 haiku 已修一半的基礎上做的，單獨跑 opus 的成本應該相近或略高。換到的是一份「為什麼第一輪不夠」的紀錄。
- 所以順序應該是：先由 `runtime auto`（SPEC 第 10 節）把明顯困難的工單直接派給強模型，機械工單派便宜工人，再用回流升級（SPEC 第 12 節）接住判斷失誤的那一部分。
- ~~MiniMax M3.1 Flash 放進 low 層~~：維護者不使用中國服務，不採用。low 層維持 haiku／codex low／agy。
