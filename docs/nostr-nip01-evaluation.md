[English](nostr-nip01-evaluation.en.md) | 繁體中文

# agent-relay 對接 Nostr NIP-01 評估

評估日期：2026-10-26。來源任務：ai-inbox `T-20261026-10`。結論先講：**現階段不把 Nostr 做進核心，也不改信件格式**；若要做，只做「選用的單向橋接器」，且必須先解決下面第 3 節的簽章落差。

## 1. 規格依據

- NIP-01 全文：<https://github.com/nostr-protocol/nips/blob/master/01.md>（2026-10-26 讀取）。
- agent-relay 信件格式：[SPEC.md 第 1 節](../SPEC.md)；簽章：[SPEC.md 第 13 節](../SPEC.md)。

NIP-01 的要點（2026-10-26 查證）：事件欄位為 `id`、`pubkey`、`created_at`、`kind`、`tags`、`content`、`sig`；`id` 是 `[0,pubkey,created_at,kind,tags,content]` 緊湊 JSON 的 SHA-256；簽章為 secp256k1 上的 BIP-340 Schnorr；`kind` 為 0～65535 的整數，1000～9999 為「一般」事件（relay 應儲存），20000～29999 為「短暫」事件（relay 不必儲存）；客戶端與 relay 以 WebSocket 傳 `EVENT`／`REQ`／`CLOSE` 與 `OK`／`EOSE`／`CLOSED`／`NOTICE`。

## 2. 欄位映射表

| agent-relay 信件欄位 | NIP-01 事件欄位 | 能否直接對應 | 做法與落差 |
| --- | --- | --- | --- |
| `schema` | 無 | 否 | 放進 `content` 內嵌的完整信件 JSON（見下方「建議形狀」） |
| `id`（`YYYYMMDDTHHMMSSmmmZ-6hex`，檔名） | `id`（64 字元 hex，內容雜湊） | **否，語意不同** | NIP-01 的 `id` 由內容算出，無法指定。信件 `id` 改放 tag `["relay-id", <id>]`；回信的 `reply_to`／`thread` 也得用 `relay-*` 自訂 tag，不能直接套 `e` tag（`e` 要 64 字元事件 id） |
| `from` / `to`（信箱名） | `pubkey` / `["p", <pubkey>]` | **否** | 信箱名是任意字串，Nostr 是公鑰。需要一份本機「信箱名 ↔ secp256k1 公鑰」對照表，與現有 `allowed_signers` 一樣只存本機 |
| `created_at`（ISO 8601 UTC，秒） | `created_at`（Unix 秒） | 是 | 無損轉換；範例 `2026-10-26T01:23:45Z` → `1792977825` |
| `kind`（`task`/`result`/`reply`/`note`） | `kind`（整數） | 需自訂 | 四種字串要對到整數。建議用一般事件區段 1000～9999 內自選未登記的數字，**實作前須查 NIPs 登記表避免撞號（未查證）**；`note` 也可考慮短暫事件 20000～29999 |
| `subject` | tag `["subject", ...]` | 是（自訂 tag） | NIP-01 只規定 tag 為字串陣列，名稱自訂；是否有標準 `subject` tag 未查證 |
| `body` | `content` | 是 | 純字串；NIP-01 對 `content` 的跳脫規則由 JSON 序列化處理 |
| `priority`、`host`、`project`、`refs`、`dispatch` | tags 或 `content` | 是（自訂） | 只有 `to`、`kind` 需要被 relay 過濾，其餘留在內嵌信件 JSON 即可 |
| `sig`（ssh-keygen ed25519，簽 canonical JSON） | `sig`（BIP-340 Schnorr，簽事件 id） | **否** | 見第 3 節 |
| 信箱目錄 `new/cur/done` 狀態 | 無 | 否 | Nostr 沒有「已讀／結案」。`relay ack` 要另寫一則事件，或維持本機狀態 |

建議形狀（外層事件由橋接器簽，內層信件原封不動，ed25519 簽章保留）：

```text
Nostr 事件
  pubkey     = 橋接器 secp256k1 公鑰
  created_at = 信件 created_at 轉 Unix 秒
  kind       = 自訂整數（task/result/reply/note 各一個）
  tags       = [["relay-id",<id>], ["relay-from",<from>], ["p",<收件端公鑰>],
                ["relay-kind",<kind>], ["subject",<subject>], ["relay-thread",<thread>]]
  content    = 完整信件 JSON（含 sig，與 git 版位元組相同）
  sig        = 橋接器對事件 id 的 Schnorr 簽章
```

實算驗證（stdlib `hashlib`，範例信件、kind 9000 為示意值，公鑰為佔位）：序列化 614 位元組，事件 `id` = `ee6b639e794c061d2fe7fdcdbe4b4806d1a12027ea2b7d096867b6020414e9c8`。同一份信件的 canonical JSON SHA-256 是 `95bbd2902b21cdd7a9129f5bd977e8d81519424726af20b5c0cba0f7b01fea17`，兩者本來就不同，所以信件 `id` 不能沿用為事件 `id`。（`sig` 本身未實算：本機 Python 沒有 secp256k1 套件，見第 3 節。）

## 3. 簽章差異

| 項目 | agent-relay（SPEC 第 13 節） | Nostr NIP-01 |
| --- | --- | --- |
| 演算法 | ed25519（OpenSSH） | secp256k1 上的 BIP-340 Schnorr |
| 簽什麼 | 去掉 `sig` 的信件 canonical JSON（鍵排序、無空白） | 事件 `id`（SHA-256 序列化） |
| 公鑰格式 | OpenSSH 公鑰，`allowed_signers` 以信箱名當 principal | 32 位元組 hex |
| 工具 | `ssh-keygen`（Windows 11 內建） | 需要 secp256k1 函式庫；Python 標準庫沒有 |
| 信任模型 | 收件端本機 `allowed_signers`，不進 store | 無內建，由客戶端自行決定信任哪些公鑰 |

兩邊的密鑰與演算法都不同，**無法用同一把金鑰同時滿足兩者**。可行做法只有兩種：

1. **雙層簽章**（建議的形狀）：內層維持 ed25519 簽的信件，外層由橋接器用 secp256k1 簽事件。端對端信任仍靠內層，Nostr 層只當傳輸；代價是要多管一把橋接器金鑰。
2. **改用 Nostr 金鑰簽信件**：等於換掉 SPEC 第 13 節，破壞既有 `allowed_signers` 與 `relay accept`，且需要引入非標準庫的曲線實作（自寫純 Python BIP-340 可行但慢，也有旁路攻擊風險）。不建議。

## 4. 原型評估結論

**結論：不做核心整合；暫不寫原型。** 本次只完成映射分析與 `id` 計算驗證，**沒有**實作適配器、沒有連到任何 relay、沒有驗證 Schnorr 簽章，以下為依規格推論，不是實測。

不做的理由：

1. **解的是不存在的問題**：目前傳輸是 `git push/pull`，不需公網 IP、不需額外 API key、不需常駐行程（SPEC 第 3 節）。Nostr relay 要一個常駐的 WebSocket 服務，等於新增一個必須維持運行的單點；「無中心依賴」只是把中心從 git 倉庫換成那台 relay。
2. **簽章要雙軌**：第 3 節的落差使整合成本集中在金鑰管理，而 SPEC 第 13 節的威脅模型（收件端明確信任名單、防重放）在 Nostr 端沒有對應機制。
3. **狀態語意不合**：`new→cur→done` 的 rename 狀態機與 `claim`／`release` 鎖（SPEC 第 9 節）依賴檔案系統；Nostr 事件不可變，需另設計狀態事件。
4. **延遲優勢有限**：Nostr 可即時推送（對照目前輪詢最長 5 分鐘），但 agent 派工是分鐘級任務，延遲不是現行瓶頸；若需要，縮短 `poller --every` 即可。

什麼條件下才值得做：已有一個常駐的 Nostr relay（例如另案評估中的 Buzz 部署，見 ai-inbox 的 Buzz 筆記）且要讓人在聊天室旁觀 agent 派工。屆時只做**單向橋接器**：把已寫入 store 的信件以第 2 節形狀轉發到 relay 供閱讀，不接收、不派工。最小驗收：用兩個本機行程（橋接器與一個只讀訂閱者）跑 `EVENT`／`REQ`，確認 `OK` 回應與事件 `id` 與第 2 節算法一致。

## 5. 未查證與限制

- 自訂 `kind` 數字是否與 NIPs 已登記數字衝突：未查證。
- NIP-10（`e` tag 的 thread 標記）、NIP-14（`subject` tag）：本次未讀，上表只用自訂 tag，不依賴它們。
- Schnorr 簽署與驗證、relay 實連：未實測，原因見第 4 節。
- 事件 `id` 實算只用 Python 標準庫 `hashlib`／`json`，可由任何人用第 1 節的序列化規則重算。
