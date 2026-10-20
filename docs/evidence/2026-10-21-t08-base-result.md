# T-20261021-08 實測：指定施工起點與結構化回信（2026-10-21）

拓撲與 T-03／T-07 相同：指揮者加 machine-b，信件都有簽章。這次 calc-demo 多了一個本機 bare remote 當 `origin`，指揮者另有自己的 clone。

## 準備

```text
指揮者 clone   commit c1bad3f（commander: add add()）→ push origin/main
machine-b      main 多一個本機 commit 15b6c83（LOCAL_ONLY.txt），而且還沒 fetch c1bad3f
```

## 結果

| 信件 | dispatch.base | machine-b 結果 |
| --- | --- | --- |
| `20261020T165935483Z-a1e147`（hermes，加 mul） | `c1bad3f22c39…`（本機沒有） | 先 `fetch origin`；worktree 的起點是 `c1bad3f`，log 為 `6c24684 relay` → `c1bad3f commander: add add()` → `db8c0e8 init`；沒有 `LOCAL_ONLY.txt`；閘門 green，relay commit `6c24684` |
| `20261020T165941217Z-8abeba` | `0123…4567`（不存在） | `refused`：`dispatch.base … is not a commit in … (fetched origin, still missing)`，沒有建立 worktree |

指揮者讀回信的 `result` 欄位（兩封的簽章都驗證通過）：

```json
{"status": "ok", "runtime": "hermes", "branch": "relay/20261020T165935483Z-a1e147",
 "commit": "6c246848a83256f5bbbf323cf2d59e6a4edb581e", "base": "c1bad3f22c39eb79552b5ff9069b7093e6ae2fbb",
 "gate": {"status": "ok", "logs": [{"phase": "green", "exit": 0, "sha256": "7a00ca76…991381"}]},
 "usage": [{"attempt": 1, "model": "google-antigravity/gemini-3.8-flash", "total_tokens": 190367, "completed": true}]}
```

不存在 base 的那封回信只有 `{"status": "refused"}`。

## 審查後的調整

fresh Opus 審查建議 `dispatch.base` 只收 40 位完整 sha，現已改成這樣。原因是短前綴約 28 bit，能推送 origin 的人可以暴力造出同前綴的 commit，而簽章只綁到前綴。寄件端的 `--base` 仍然可以寫任何 ref，CLI 會在目前的 repo 先 `rev-parse` 成完整 sha。實測時用的 12 位前綴信件是在這個修正之前寄出的。

單元測試：`tests/test_dispatch.py::BaseAndResultTests`，涵蓋格式、本機 HEAD 不同時的起點、從 origin fetch、未知 base 被拒、`result` 只能出現在 result 信、CLI 解析 ref。
