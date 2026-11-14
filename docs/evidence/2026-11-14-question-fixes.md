# T-20261114-05 決策問答修正驗收

本次僅在 `worker/relay-ask-fix` 修改獨立 clone；不 push，不操作真實 relay 信箱。
所有重現透過 `tests/test_questions.py` 的臨時 home/store 與 `--no-sync`；同步失敗測試使用 mock，沒有真實同步。
修前版本：`2044f9bdeed9d04291a80a04097959f5986667ec`。修前與 mutation 都在臨時程式副本執行，不覆寫工作樹。

## 必修重現

以下省略共同前綴 `python -m agent_relay`；信件 ID 由測試臨時建立，所有指令加 `--no-sync`。

| 項目 | 重現參數 | 修前結束碼 | 修後結束碼 |
| --- | --- | --- | --- |
| 推薦需為選項代號 | `ask --to commander --subject Q --option A=yes --option B=no --recommend C` | 0（錯誤接受） | 2 |
| 非題目不得回答 | `answer <note-id> --choice A` | 0（回信並結案） | 2，原信與回信均不變 |
| 選項需存在 | `answer <question-id> --choice C` | 0（錯誤接受） | 2，原信與回信均不變 |
| 不得重答 | 同題執行兩次 `answer <question-id> --choice A` | 第二次 0 | 第二次 2，沒有第二封回信 |
| 批次共同答案 | `answer <id1> <id2> --choice A` | 2（parser 不接受第二個 id） | 0，兩題各一封回信並結案 |

逐題負向與整批零寫入證據見 `tests.test_questions`：批次涵蓋非 question、無選項、已結案、不存在、重複 id、錯誤收件者與回覆驗證失敗；拒絕前後比對所有信件檔名與內容。

## Red / green

`python -m unittest tests.test_questions -q`：目前 25 個測試套在修前程式副本上為 `Ran 25 tests in 3.046s`、`FAILED (failures=37)`、exit 1；修正後為 `Ran 25 tests in 0.990s`、`OK`、exit 0。

指定的 `py -V:Astral/CPython3.11.15 -m unittest tests.test_questions -q` 在此環境回 `No suitable Python runtime found`，exit 1。改於暫存目錄安裝 CPython 3.11.15，直接以該 `python.exe -m unittest tests.test_questions -q` 執行：`Python 3.11.15`、`Ran 25 tests in 2.286s`、`OK`、exit 0。系統預設 Python 未更動。

## Mutation 抽查

指令：`python tools/check_question_mutations.py`，exit 0；`Mutation checks: 21/21 killed; temporary sources restored`。
每個 mutation 於臨時副本單獨停用檢查（退出碼案例退回舊的字串 SystemExit），要求對應測試發生 assertion failure；語法錯誤、import error、timeout 不算抓到。每次 finally 還原副本，工作樹從未套用 mutation。

| 停用或退回的檢查 | 對應測試 | 結果 |
| --- | --- | --- |
| known fields | `tests.test_questions.EnvelopeQuestionTests.test_unknown_question_fields` | KILLED |
| two options | `tests.test_questions.EnvelopeQuestionTests.test_options_require_two_entries` | KILLED |
| option format | `tests.test_questions.EnvelopeQuestionTests.test_option_format` | KILLED |
| unique codes | `tests.test_questions.EnvelopeQuestionTests.test_option_codes_must_be_unique` | KILLED |
| recommend membership | `tests.test_questions.EnvelopeQuestionTests.test_recommend_must_match_code` | KILLED |
| recommend needs options | `tests.test_questions.EnvelopeQuestionTests.test_recommend_requires_options` | KILLED |
| answer required | `tests.test_questions.CliQuestionWorkflowTests.test_answer_requires_choice_or_note` | KILLED |
| duplicate ids | `tests.test_questions.CliQuestionWorkflowTests.test_batch_invalid_member_sends_nothing` | KILLED |
| agent required | `tests.test_questions.CliQuestionWorkflowTests.test_answer_argument_errors_exit_two` | KILLED |
| message identity | `tests.test_questions.CliQuestionWorkflowTests.test_batch_invalid_member_sends_nothing` | KILLED |
| question kind | `tests.test_questions.CliQuestionWorkflowTests.test_answer_rejects_non_question` | KILLED |
| open question | `tests.test_questions.CliQuestionWorkflowTests.test_answer_rejects_closed_question` | KILLED |
| choice membership | `tests.test_questions.CliQuestionWorkflowTests.test_answer_rejects_invalid_choice` | KILLED |
| open question choice | `tests.test_questions.CliQuestionWorkflowTests.test_answer_rejects_choice_without_options` | KILLED |
| validate all replies | `tests.test_questions.CliQuestionWorkflowTests.test_batch_reply_validation_before_delivery` | KILLED |
| filter exclusion | `tests.test_questions.CliQuestionWorkflowTests.test_inbox_filters_mutually_exclusive` | KILLED |
| ask allowlist | `tests.test_questions.CliQuestionWorkflowTests.test_ask_allowlist_and_override` | KILLED |
| override reason length | `tests.test_questions.CliQuestionWorkflowTests.test_ask_allowlist_and_override` | KILLED |
| override audit | `tests.test_questions.CliQuestionWorkflowTests.test_ask_allowlist_and_override` | KILLED |
| deadline display | `tests.test_questions.CliQuestionWorkflowTests.test_ask_filter_read_and_answer_roundtrip` | KILLED |
| multiple ids | `tests.test_questions.CliQuestionWorkflowTests.test_answer_batch` | KILLED |

## 行為範圍

只支援每批共同 `--choice`／`--note`；不同答案分次執行。單題 JSON 維持原物件，多題用 `answers` 陣列。整批預檢保證驗證失敗零寄送／零結案；不提供磁碟、簽章或同步失敗的交易回滾。`read` 原本已有 deadline 顯示，本次新增對應 assertion 並以 mutation 證明它會偵測顯示遭移除。

## 完整交付檢查與阻礙

`python -m unittest discover -s tests -q`：`Ran 481 tests in 413.105s`、`FAILED (failures=1, errors=14)`，exit 1。原始完整輸出保存在本機暫存目錄的 `relay-ask-fix-full-tests.log`。

- 11 個簽章 errors：實際 `whoami` 是 `CodexSandboxOnline`，但程序的 `USERNAME=SanHsien`；原始版本代表測試 `tests.test_signing.SigningTests.test_valid_signature_survives_storage` 也得到相同 `SigningError: Bad permissions`（exit 1）。僅在測試程序以 `$env:USERNAME=whoami` 對齊身分後，該案例通過（1 test，exit 0）。
- 3 個 timeout cleanup errors 及 1 個 assertion failure：`test_dispatch.RunTests.test_timeout_kills_worker`、`test_gates.GateReviewTests.test_timeout_kills_grandchildren_quickly`、`test_robustness.RunStdinTests.test_worker_that_never_reads_stdin_still_times_out`。最後一個同時有 failure 與 cleanup error。對本輪新建的單一測試子程序執行 `taskkill /T /F /PID <pid>` 回 exit 1、`ERROR: Access denied`；測試子程序已由持有的 Popen 物件 terminate 並 wait 清理。
- `dispatch.py`、`gates.py`、`procs.py`、`signing.py` 與 HEAD 完全一致。沒有為通過這台機器而修改它們、跳過測試或放寬 timeout。

因全套未綠，本輪不 stage、不 commit、不 push。後續工作只登記在 ROADMAP；不能把本次聚焦驗證當成完整交付通過。

最終補驗：

- `$env:USERNAME=whoami; python -m unittest tests.test_dispatch.SenderTrustTests tests.test_sender_session.SignatureTests tests.test_setup.SetupLocalTests.test_builds_everything_and_both_sides_trust_each_other tests.test_signing.SigningTests -q`：`Ran 15 tests in 9.090s`、`OK`，exit 0。僅重跑受影響的簽章群組，不重跑已通過的全套其他案例。
- `python tools/check_doc_parity.py`：`PASS: bilingual document parity check passed (5 pairs)`，exit 0。
- `python tools/check_links.py`：`31 Markdown files checked, 0 with broken links.`，exit 0。
- `git diff --check`：無輸出，exit 0。
- `git status --porcelain`：8 個 modified、2 個 untracked（本證據檔與 mutation 工具）；沒有 staged 檔案。HEAD 仍為 `2044f9bdeed9d04291a80a04097959f5986667ec`，沒有新 commit。
