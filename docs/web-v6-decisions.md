# web-v6 裁決紀錄

沿用 web-v4／v5 的體例：`docs/web-v6-plan.md` 寫**打算**做什麼，`docs/web-v6-design.md` 是設計本體，
本文件寫實作過程中計畫沒寫到的判斷，以及 review 的處置。逐階段接續，從 D42 起編號。

## D42（P1）：`config.env`、列出 key 的 `.env` 閘、送出時就檢查、`not_published`

範圍是設計 §4、§5、§8.3 的 `not_published` 結局與 §9 的「設定」區。下面是實作時才定下的細節。

### `.env` 閘的訊息

- key 用「」框起，最多 5 個，其餘寫「等 N 個」，每個截到 64 字。每個 key 先過
  `redact_known_secrets` 再截斷（`_env_policy_message`），沿用全模組 redact→cap 的順序。
- 兩道閘各自**收集所有**不合格的 key 才回答，不再遇到第一個就停：一次拒絕就說完要搬的所有東西。
  通過的情況本來就要走完每個 key 與每一行，所以成本沒有增加。
- 過短的訊息多了一句「.env 的每個值都會被當成秘密遮蔽，而太短的值遮不掉」。起因就在這裡：
  操作者看到舊訊息「無法遮蔽」，以為哪裡有秘密，卻找不到——他的 `.env` 裡只有 TTL。
- 拼法閘的訊息也補上「如果它只是設定，也可以把那一行移到同目錄的 config.env」。
- 被後面同名行蓋掉的舊行（R8-1）也要能被點名，所以 `_env_assignment_rhs_all` 改成回傳
  `(key, rhs)`；被判斷的仍然只有 RHS。

### 一道閘、兩個呼叫點

`_revise_env_gate` 把 `.env` 的讀取與解析、遮蔽政策、`config.env` 的讀取放在**同一次** worker hop
（沿用 R1-3／R4-3：解析與掃描都不上 event loop）。它被呼叫兩次：

1. **送出路徑**：`start_revise_job` 在 `reserve_sync_operation()` 之前。擋下就回
   `ReviseJobStart(refusal="env_policy", message=…)`，路由回 `409 env_policy`。這次檢查只讀檔案、
   不涉及版本，所以不影響不變量 K：`expected_vid` 仍然在取得名額之後比對。
2. **`run_revise`**：照舊在 LLM 呼叫之前。從送出到 job 真正開跑可能隔分鐘級，而算數的是開工
   當下的檔案。

既有測試 `test_run_revise_never_redacts_or_builds_its_prompt_on_the_event_loop` 釘住「`.env` 的解析
與讀取在同一個 worker」，合併成一次 hop 之後照樣成立。

### `config.env`

- **讀不到時**：執行時當作空的，與 `.env` 相同；修訂時拒絕（`_ERROR_REVISE_CONFIG_UNREADABLE`、
  `_ERROR_REVISE_CONFIG_TOO_LARGE`）。理由是 builder 測試的環境必須等於執行時的環境，而操作者
  以為設定了、其實讀不到的情況，拒絕比默默少一份設定更容易被發現。
- **builder 寫進工作區的 `config.env` 不剝除**：執行時只讀套件層那一份，版本內容裡的同名檔不會被
  載入，所以不像 `.env` 那樣有「從後門復活」的問題。builder 提示明說不要建立。
- **修訂提示**：`config.env` 的**原文**（含註解）經遮蔽器後放入；`.env` 只放 key 名；兩邊都有的
  key 另列一行，說明以 `.env` 為準。三段各自受與其他段相同的 per-part budget 截斷。
- **run_shell 的環境**：`{**config, **env}`，與 `tools._build_tool_env` 的順序一致。
  `_build_meta_tools` 的參數名仍叫 `secret_env`（不擴大這次的改動），docstring 寫明修訂時它也帶
  `config.env`。

### 結局欄位

- `InstallOutcome.not_ready` 只在 builder 自己回 `ready=false` 時為真；`InstallJob.outcome` 是
  `published`／`not_published`／`failed`，進行中為 null。
- `state` 維持原本兩個終局值，只靠 `outcome` 細分失敗，舊的輪詢讀者不受影響。
- install 的 `ready=false` 一樣是 `not_published`，卡片寫「AI 未安裝工具」。

### 設定 API（`GET /api/tools/{name}/settings`）

- 只要求套件存在，不要求 `current` 可解析：兩個檔案都在套件層，`current` 壞掉的套件正是需要看
  設定的時候。內部別名一樣回 404。
- `contains_secret` 的判斷是「這個值是否**逐字包含**某個已登記的秘密」（長度達遮蔽下限者），
  涵蓋所有套件的 `.env` 與進行中登記的秘密；含有秘密的值整個以遮蔽標記回傳。最初的寫法是
  「遮蔽器有沒有改動這個值」，review C1 指出那會誤報，見下方附錄。
- `built_before_v5` 看**任一**已提交版本的 `origin.json` 的 `source` 是否來自遷移
  （`legacy-ai-meta`、`pre-existing/unknown`）。不看遷移專用的 `migration-owner.json`：那是遷移
  內部的所有權證明，不是產品語意。
- `secret_like_name` 只看 key 名是否含 `KEY`、`TOKEN`、`SECRET`、`PASSWORD`；`KEYWORD_LIMIT`
  也會被提醒，接受這種誤報，因為它只是提醒。

### 前端

- 「設定」是每一列獨立的開關（`ToolSettingsSection`，獨立元件檔），`current` 壞掉的套件也有；
  「AI 總結」則和以前一樣只在有可用版本時出現。刪除工具時一併移除這個查詢的快取。
- `env_policy` 的通知是橘色、不自動關閉：下一步是去改檔案，幾秒就消失的通知會把 key 名一起帶走。
- `not_published` 卡片以 AI 自己的 summary 為主文，下方註明「工具維持原本的版本，沒有任何變更」。
- Mantine 的 Badge 預設會轉大寫，把 `.env` 印成 `.ENV`，所以設定區的 Badge 加 `tt="none"`。
- 錯誤訊息模板裡 `{keys}` 前後不留空格：key 已經用「」框起，留空格會變成「的 「CACHE_TTL」 值」。

### 驗收

設計 §11 的 A、B、C、D、E、O、W，各自對應的測試：

| 不變量 | 測試 |
|---|---|
| A | `test_config_env_values_are_never_registered_as_secrets`、`test_package_settings_shows_key_names_and_redacted_config` |
| B | 既有 `.env` 閘與遮蔽測試全數通過；只改了訊息字串的比對（原本斷言「不含 key」的幾行改成比對列出 key 的完整訊息） |
| C | `test_unmaskable_env_error_names_every_short_key_and_never_a_value`、`test_env_policy_message_redacts_a_key_before_cutting_it`、`test_router_revise_refuses_an_env_policy_failure_before_any_job`、`test_router_revise_refuses_an_unreadable_config_env` |
| D | `test_config_env_layers_between_tls_no_verify_and_dotenv`、`test_run_revise_gives_config_env_to_the_shell_and_the_prompt` |
| E | `test_config_env_is_not_interpolated` |
| O | `test_run_revise_not_ready_publishes_nothing_and_says_so`、`test_job_outcome_separates_not_published_from_failed`、`test_job_outcome_of_an_unexpected_exception_is_failed` |
| W | `test_router_settings_serves_a_tool_named_jobs_and_404s_the_unknown` |

前端的判斷邏輯在 `toolSettings.js`、`toolInstall.js` 的 `toolJobOutcomeView`、`toolSummary.js` 的
`reviseRefusalNotification`，各有 vitest。另外用實際的瀏覽器跑過一次：遷移來的套件顯示提醒、
貼錯檔的秘密被遮蔽並標示、送出修訂被 `CACHE_TTL` 擋下時出現列出 key 的橘色通知。

### D42 附錄（P1 review r1，Fable，8 條）

| # | 嚴重度 | finding | 處置 |
|---|---|---|---|
| C1 | P2 | `contains_secret = redact(value) != value` 會誤報：遮蔽器的「結尾是秘密前 6 字就遮」守衛是給被截斷的文字用的，完整的設定值也被套上了。實例：token 以 `myproj-` 開頭，`BASE_URL=https://api.example.com/myproj` 被標成紅色秘密 | **採納**。改成逐字包含判斷；含有秘密的值整個以標記回傳。測試 `test_package_settings_flags_exact_containment_only` 在舊寫法下會失敗 |
| C2 | P3 | `package_settings` 對每個值與 key 都呼叫 `redact_known_secrets`，每次都重掃整個 tools 目錄；500 行的 `config.env` 掃 1,002 次 | **採納**，與 C1 一起：已知秘密集合只算一次，key 名用同一份集合遮 |
| C3 | P3 | 拼法閘把含 `=` 的註解行當 key 點名，訊息出現「「# Usage: KEY」的寫法」 | **採納（只改措辭）**：以 `#` 開頭的列為「註解行「…」」。閘的語意不變（R8-1 本來就檢查註解行，`cat` 會印出註解） |
| C4 | P3 | 送出路徑的閘現在會呼叫 `redact_known_secrets`，而它在已知秘密掃描失敗時會拋例外 → 500 | **接受並記錄**：拋出時還沒有名額、工作或秘密登記，也沒有文字外流；把它映成 `env_policy` 反而會告訴操作者「你的檔案有問題」，其實壞的是掃描。寫進 `_revise_env_gate` 的 docstring |
| C5 | P3 | 測試缺口：拼法閘多個 key、`config.env` 超過上限、`.env` 不可讀的 `env_readable`、`versions/` 裡的怪項目 | **採納**：補 `test_spelling_gate_names_each_failing_line_once_in_order`、`test_router_revise_refuses_an_oversized_config_env`、`test_package_settings_reports_an_unreadable_dotenv`、`test_built_before_v5_skips_entries_that_are_not_committed_versions` |
| C6 | P3 | 設定區用遮蔽後的 key 名當 React key，兩個 key 都等於秘密時會重複 | **採納**：改用 `occurrenceKeys`（`TagList` 的「名稱-出現次數」做法；Biome 禁止用陣列索引） |
| C7 | P3 | 文件不一致：`frontend/README.md` 的檔案分工規則沒提第三個檔；兩份文件說 `env_policy` 一律列出 key（讀不到／過大其實只說明狀況）；根 README 對 `config.env` 給了錯的理由 | **採納**：三處都改 |
| C8 | P3 | 行為變更應記錄；修訂提示把空值的 `.env` key 也叫「credentials」 | **採納**：記錄如下；提示改稱「the package's .env sets in your run_shell environment (secrets; values hidden…)」 |

C8 記錄的兩個行為變更：

- **送出時的拒絕順序**：`.env`／`config.env` 的閘只要套件目錄存在就跑，所以 `current` 壞掉、同時
  `.env` 不合格的套件，現在回 `409 env_policy`，以前是 `404`；`job_busy` 也排在 `env_policy` 之後。
  前端不會對沒有可用版本的列提供修訂，所以目前沒有可見影響；P3 的一鍵修正會走同一條路徑，屆時
  沿用這個順序。
- **空值的 `.env` key 也會列進修訂提示**：它們確實會蓋過 `config.env` 的同名值（與執行時一致），
  所以提示不排除它們，只把措辭從「credentials」改成「.env 設定的變數」。

覆蓋判定：不變量 A、B、C、D、E、O、W 全部有測試覆蓋（review 的逐條判定相同）。

### D42 附錄（P1 review r2，Fable）

判定**已收斂**：沒有程式碼層級的 finding，C1–C8 除 C7 外皆判「已解決」。剩下兩處文件措辭，已修正：

| # | 嚴重度 | finding | 處置 |
|---|---|---|---|
| D1 | P3 | 根 README 換上的新句「`.env` 的新值不會在那一場修訂裡被遮蔽」與程式相反：`known_secret_values()` 每次呼叫都重掃，`_cached_env_values` 以檔案身分為快取標籤，修訂中途寫進 `.env` 的一般新值**會**被遮。真正沒保護到的是：中途的新值不再經過開工前的閘，太短或帶跳脫的遮不掉 | **採納**：改寫成正確的版本，與 `docs/tool-calling.md` 一致（C7 因此在本輪結束後才算完整） |
| D2 | P3 | 設計 §9 與 §11-A 仍寫 `config.env` 的值「經過 `redact_known_secrets`」，與 C1 之後的程式不符；後續階段的實作者若照著改回去，C1 的誤報會重現 | **採納**：§9 與 §11-A 改寫成逐字包含規則，並註明不要改用 `redact_known_secrets` 的理由 |

本輪也確認了 C1 的取捨可接受：截斷的秘密（例如 token 的前 19 字）放在 `config.env` 時會原樣顯示、
不標示。設定區只給操作者自己的瀏覽器看，而同一份 `config.env` 在修訂提示裡經過遮蔽器時同樣不會
被遮（結尾守衛只作用在整段文字的最末端），所以設定區的暴露面嚴格小於那條已接受的路徑。

## D43（P2a）：工具執行紀錄 `tool_run`

範圍是設計 §6、§9 的「最近執行」（唯讀＋單筆刪除）與列表的 `last_run`。下面是實作時才定下的細節。

### 執行路徑

- `_run_tool_subprocess` 回傳 `ToolRunResult`；`text` 就是以前回傳的字串。**驗收方式**：用一支
  探測程式對改動前的 commit 與改動後各跑一次八種情況（成功、stdout 超限、非零 exit、帶秘密的
  stderr、stderr 超限、逾時、無法啟動、帶秘密的 stdout），輸出逐字相同。
- `_make_handler` 拆成 `_execute`（照舊的重驗、開關、環境、執行）與 `_handler`（量時間、寫紀錄、
  回傳 `text`）。identity 讀不到與共享鎖拿不到這兩個早退，也記成 `refused`。
- **紀錄在共享鎖釋放之前寫**。直接呼叫服務、自己拿鎖的那條路徑也一樣；AI 請求本來就整場持有。
  這讓 P2b 的「刪除工具後刪紀錄」不會漏掉一筆剛好寫在中間的紀錄。
- **`refused` 不存內文**：工具沒有執行，參數沒有除錯價值，只有隱私成本。`message` 照存（拒絕的
  理由）。
- 非零 exit 的路徑用**同一份**已知秘密遮給模型的字串與紀錄的三段內文，fail-closed 與以前相同。
  逾時與無法啟動的路徑，給模型的字串不含輸出，遮蔽只為紀錄而做：掃描失敗就不存內文
  （`_record_bodies`），不讓紀錄改變呼叫的結果。
- stderr 超限、但行程在被終止前自己以 exit 0 結束時，舊程式回傳 stdout（視為成功），紀錄因此
  也是 `ok`——紀錄描述的是模型實際收到的東西。
- `message` 是模型收到的整段字串，由寫入端截到 500 字；它的輸出部分本來就完整存在 `stderr`。

### 寫入

- 紀錄器在 `main` 的 **lifespan** 接上（`init_db` 之後），不在 import 時：只有 lifespan 知道表已經
  存在。測試的 client 不進 lifespan，所以只有自己裝紀錄器的測試會寫紀錄。lifespan 結束時拆掉。
- **短 busy timeout 用獨立的 engine**（`build_recorder_engine`，1.5 秒），不在共用連線池的連線上
  改 `PRAGMA busy_timeout` 再改回來：改回失敗時那條連線會帶著短 timeout 回到池裡，影響之後的
  一般請求。
- WARNING 只寫例外的**型別**：SQLAlchemy 的錯誤文字會帶 SQL 參數，也就是內文。
- 截斷標記算在上限之內：開頭截斷在尾端加 `\n…[已截斷]`，結尾截斷在前面加 `[前段已截斷]…\n`。
- 寫入前的 `_utf8_safe` 沿用 `llm_log` 的標準作法：一個 lone surrogate 會變成三個 U+FFFD
  （它的三個位元組各自無效），測試以函式本身為準，不寫死字元數。

### 讀出

- **第二次遮蔽 fail-closed**：讀取時對當下的已知秘密再遮一次；掃描失敗就回 500，不回沒檢查過的
  文字。列表只遮 `message`，內文只在單筆詳情裡遮與回傳。
- **AI 日誌連結**：`generate_structured` 在互動期間設定 ContextVar（`LlmInteraction`：紀錄 id、
  process token、workflow），handler 讀它。紀錄活得比日誌環久，所以讀出時 token 不是本行程就把
  id 與 token 一起改成 `null`（同 `_summary_detail`）；是本行程時兩者都回傳，連結帶著 token，
  分頁跨過重啟時 AI 日誌頁照樣拒絕。
- **`vid_state`**（`current`／`older`／`gone`）：設計 §6.4 要求標示「版本已丟棄」。`gone` 同時
  涵蓋丟棄與手動刪除，畫面寫「版本已不存在」，不猜是哪一種。判斷沿用 `current` 與 discard 接受
  一個版本的同一條規則（`version_exists` → `_resolve_version_target`），同一次讀取裡每個 vid 只查
  一次。
- `GET /api/tools/{name}/runs` 的閘是**套件存在**（同設定），不要求 `current` 可解析。
- `PATCH /api/tools/{name}` 回傳的那一列也帶 `last_run`，與列表一致。

### 前端

- 「最近執行」是每一列獨立的開關（`ToolRunsSection`），所有套件都有。失敗的可以展開，詳情在
  展開時才讀 `GET /api/tool-runs/{id}`。
- **刪除一筆時不移除那筆詳情的快取**：元件仍掛著、仍在觀察它，移除會讓它立刻重抓、得到 404。
  列表重抓後那一列卸載，id 不會重用（AUTOINCREMENT），留下的快取項目碰不到。這是瀏覽器實測時
  看到的 404，改掉之後主控台乾淨。
- 版本改變（丟掉這一版、修訂發布）時重新驗證這個工具的執行紀錄，因為 `vid_state` 與 `fixable`
  都是對舊的 `current_vid` 算的；刪除工具時移除它的快取。
- `WORKFLOW_LABELS` 搬到 `constants/labels.js`，`formatTimestamp`／`formatDuration` 搬到
  `utils/format.js`，AI 日誌頁與執行紀錄共用，避免兩份標籤漂移。
- JSX 裡斷行的中文會在斷行處多一個空格，所以說明句改成一個字串常數。

### 驗收

| 不變量 | 測試（`tests/test_tool_runs.py`，另有一則在 `tests/test_main.py`） |
|---|---|
| F | `test_ok_run_returns_the_same_string_and_records_metadata_only`、`test_ok_run_cut_at_the_output_cap_is_recorded_as_truncated`、`test_failed_run_returns_the_same_string_and_keeps_the_bodies`、`test_stderr_overflow_says_exit_minus_nine_but_records_output_overflow`、`test_timeout_returns_the_same_string_and_keeps_what_was_printed`、`test_start_failure_returns_the_same_string_and_keeps_the_args`、`test_refusals_return_the_same_strings_and_record_no_bodies`、`test_a_recorder_that_raises_changes_nothing_and_logs_no_body`；另加上面的新舊比對 |
| G | `test_secrets_in_args_and_output_are_masked_in_the_stored_record`、`test_secrets_are_masked_on_the_timeout_path`、`test_secrets_are_masked_on_the_stderr_overflow_path` |
| H | `test_ok_run_returns_the_same_string_and_records_metadata_only` |
| I | `test_bodies_are_masked_again_on_read`、`test_a_failing_secret_scan_fails_the_read_instead_of_serving_unchecked_text` |
| U | `test_a_lone_surrogate_in_the_args_is_stored_as_a_replacement_character`、`test_a_locked_database_costs_the_record_within_the_short_timeout` |
| W | `test_a_tool_named_jobs_has_its_runs_read_not_a_job_poll`；`test_only_by_id_routes_declare_404` 加上三條新路由 |

其他：AI 日誌連結（`test_a_run_inside_an_interaction_links_to_its_log_record`、
`test_a_log_link_from_another_process_is_not_served`）、可修判斷與 `vid_state`
（`test_fixable_is_a_failure_of_the_current_version`、`test_an_older_version_that_still_exists_is_older_not_gone`、
`test_a_record_whose_package_is_gone_still_reads_as_gone`）、截斷（`test_record_cuts_each_body_to_its_cap_from_the_right_end`）、
lifespan 接線與實際寫入檔案（`test_lifespan_wires_the_tool_run_recorder_and_unwires_it_on_exit`）。前端的判斷在
`toolRuns.js`、`format.js`，各有 vitest；另用實際的瀏覽器跑過：列上的提示、展開詳情、寫入時沒遮到的
秘密在讀出時被遮、單筆刪除後列表更新。

### D43 附錄（P2a review r1，Fable）

判定**已收斂**，沒有 P1／P2。逐條比對過的：F（13 種情境對改動前的程式逐字相同）、G、H、I、U、W、
鎖與執行緒、ContextVar、前後端欄位、快取失效。五條 P3 全部處理：

| # | finding | 處置 |
|---|---|---|
| E1 | `_execute` 拋例外時（非零 exit 後遮蔽用的秘密掃描失敗、tools lock 無法使用）不寫紀錄，與「每一次都記」不符 | **採納**。新增 outcome `error`：記下模型收到的同一句 `tool execution failed: <型別>`（抽成 `llm.tool_exception_text`，兩處共用一個拼法），不存內文，再把例外原樣拋出，交給模型的字串不變。不可一鍵修正（那是後端的問題，不是工具的）。測試 `test_a_call_the_backend_fails_is_recorded_as_error_and_still_raises` |
| E2 | stderr 超過上限、但工具在被終止前以自己的 exit code 結束時，仍記成 `output_overflow`，畫面寫「被終止」 | **採納**。只有 exit code 是 `-SIGKILL` 才算 `output_overflow`，否則是 `failed`。測試 `test_stderr_overflow_that_exits_on_its_own_is_a_plain_failure` |
| E3 | 什麼都沒印的失敗，詳情畫出空白的程式碼框 | **採納**。資料庫照存空字串（「沒有輸出」也是事實），畫面改寫「（沒有輸出）」 |
| E4 | README 說事後才搬進 `.env` 的值「也看不到」，但設計 §6.2 已接受被截斷切開的片段；後端 README 說寫入都在共享鎖內，兩種早退的拒絕其實沒有鎖 | **採納**：兩處都改成精確的說法 |
| E5 | 耗時從取得鎖之前算起，等破壞性操作放鎖的時間算進了工具耗時 | **採納**：改成取得鎖之後才開始計時 |

## D44（P2b）：手動 purge、筆數、刪除工具時清紀錄

範圍是設計 §7、§6.4。下面是實作時才定下的細節。

### API

- **時間條件用絕對時間 `before`，不用 `older_than_days`**。設計寫的是天數；但天數要在預覽與確認
  各算一次，兩次之間剛好跨過界線的紀錄會在確認時被刪掉、卻沒算進預覽的筆數，不變量 J 就破了。
  前端在開啟對話框時取一次「現在」，換算成 `before`，預覽與確認都送同一個值。
- **結果條件是 `all`／`ok`／`not_ok`**。`not_ok` 是成功以外的全部，包括 `refused`：被拒的呼叫
  也不是成功，沒有人會為了它本身留著它。筆數的 `not_ok` 用同一個定義，畫面寫「失敗與被拒」。
- 確認（`dry_run: false`）一定要帶預覽的 `max_id`，缺了回 422；預覽沒有任何符合的紀錄時
  `max_id` 為 `null`，前端因此停用「刪除」。
- **孤兒**是名稱已解析不到套件的紀錄（`_resolve_package_dir_no_alias` 為 None）。`TOOLS_DIR`
  未設定、或設定了但目錄不在時一律**沒有**孤兒（後者是 review r1 F1 補上的）：那時任何名稱都解析不到，把整張表當孤兒等於讓一個暫時關掉的設定
  交出全部紀錄。孤兒在確認時重新判斷，所以預覽與確認之間有人手動刪掉某個工具目錄，那個工具的
  紀錄會多出來；`max_id` 仍然擋住預覽之後寫進來的紀錄。這個時間窗是操作者自己的手動操作，接受。
- `/stats` 宣告在 `/{run_id}` 之前；`/purge` 是 POST，不會與 GET `/{run_id}` 衝突。

### 刪除工具

- 在路由層、`delete_tool` 回 `removed` 或 `retained` 之後才刪紀錄；404 與 409 什麼都不動。紀錄
  刪除失敗只回報 `runs_deleted: false`（`delete_for_tool` 吞下例外、記一行只含型別的 WARNING），
  工具的刪除照樣成立。
- 這一步在排他鎖釋放之後。P2a 讓紀錄在共享鎖釋放之前寫入，而刪除需要排他鎖，所以刪除開始時
  不會有尚未寫入的紀錄；刪除之後，這個工具也不可能再被廣告。

### 前端

- 確認框：每個工具「最近執行」的「清除紀錄」選單（全部／只清成功的／30 天前的）開的是固定
  範圍的框，只列出範圍；工具頁上方的「清除紀錄…」開的是可以調整的框（結果、時間、只清已刪除
  的工具留下的），有孤兒時預設勾選孤兒。兩者都先顯示「將刪除 N 筆」再讓你按「刪除」。
- 預覽的查詢 `staleTime: Infinity`、不在視窗取得焦點時重抓：畫面上的筆數就是確認時的筆數，
  不讓背景重抓在操作者考慮時換掉它與它的 `max_id`。
- 刪除工具的通知在 `runs_deleted: false` 時轉為橘色，並指出用「只清已刪除的工具留下的」清掉。
- 工具頁的「重新整理」也重抓執行紀錄與筆數。

### 驗收

| 不變量 | 測試（`tests/test_tool_runs.py`） |
|---|---|
| J | `test_purge_deletes_exactly_what_its_preview_counted`、`test_purge_scopes_narrow_and_combine`、`test_purge_with_nothing_to_match_has_no_bound`、`test_a_purge_that_deletes_needs_its_preview`、`test_without_a_tools_directory_no_record_is_called_an_orphan` |
| K | `test_deleting_a_tool_deletes_its_runs_and_only_its_runs`、`test_a_refused_delete_keeps_the_runs`、`test_records_that_could_not_be_deleted_are_reported_and_stay_purgeable` |
| W | `test_stats_counts_per_tool_and_orphans_and_is_not_an_id` |

前端的請求內容、範圍說明與筆數文字在 `toolRunsPurge.js`，刪除通知在 `toolDelete.js`，各有 vitest。

### D44 附錄（P2b review r1，Fable）

判定**未收斂**：一條 P2、五條 P3，全部處理。逐條比對過且無誤的：不變量 J（預覽不刪、`max_id` 界線、
預覽後的新紀錄留下）、K（404／409 不動、紀錄刪除失敗時回報）、W、時間比較（aware／naive 都換成
UTC 再比）、stats 的 SQL、孤兒判斷與別名、P2a-r1 的 `error` 路徑。

| # | 嚴重度 | finding | 處置 |
|---|---|---|---|
| F1 | P2 | `TOOLS_DIR` 有設定但目錄不在（卷沒掛上、改了名）時，每個名稱都解析不到，整張表都成了孤兒：頁首寫「已刪除的工具留下 N 筆」，「清除紀錄…」預設勾選孤兒、將刪除全部 | **採納**。比照 `_scan_all` 與 tools lock，目錄不在也視為功能關閉、沒有孤兒。測試 `test_a_missing_tools_directory_makes_no_record_an_orphan` |
| F2 | P3 | 請求模型沿用 pydantic 預設的忽略多餘 key，而設計 §7 還寫著舊的 `older_than_days`：照設計送出時那個條件被丟掉，範圍默默變大，確認時刪掉全部 | **採納**。`ToolRunPurgeRequest` 改為 `extra="forbid"`（422），§7 兩處註明 P2b 的實作；沒有時區的 `before` 視為 UTC，寫進後端 README。測試 `test_a_purge_with_an_unknown_condition_is_refused_not_widened`、`test_a_naive_before_is_read_as_utc` |
| F3 | P3 | 兩個 DELETE 用 SQLAlchemy 預設的 `synchronize_session`，會在 Python 裡對 session 持有的 ToolRun 物件重跑 WHERE；物件的 `started_at` 有時區、條件沒有，比較時拋 `TypeError`。目前的路由碰不到，但 session 是和路由共用的 | **採納**。兩個 DELETE 都加 `synchronize_session=False`。測試 `test_a_purge_through_a_session_holding_run_objects_still_deletes`（沒有修正時重現 `TypeError`） |
| F4 | P3 | 刪除工具的通知在 `retained` 等本來就以「。」結尾的訊息後面再接「。它的執行紀錄…」，出現「。。」 | **採納**：接上之前去掉結尾的「。」，補測試 |
| F5 | P3 | 清除對話框只停用「取消」，Esc／點外面／右上角仍能在刪除進行中關掉；重開後預覽會算到正在被刪的紀錄 | **採納**：刪除進行中對話框不能關（與頁面上其他刪除對話框相同） |
| F6 | P3 | 筆數讀取失敗時整列（含頁面層級的「清除紀錄…」）默默消失 | **採納**：改成紅字「無法讀取工具執行紀錄的筆數」，「清除紀錄…」照樣可用（它自己的預覽會計數） |

## D45（P3）：一鍵請 AI 修正（工具頁）

範圍是設計 §8 的工具列表與「最近執行」兩個入口、§8.4 的除錯程序、§8.5 的防呆、§10 的提示規則。
下面是實作時才定下的細節。

### 請求

- `ToolReviseRequest`：`feedback` 改為選填，`run_ids` 最多 3 筆；兩者至少要有一樣（422）。空白
  意見視同沒有（舊行為是 422，現在只有在也沒附紀錄時才 422，原本的四個 422 測試照樣成立）。
  上限 3 是對送出的清單算（請求本身的上限），其中重複的 id 只附一次；非正數或超出 SQLite 整數
  範圍（`models.SQLITE_MAX_INT`）的 id 422。
- 紀錄在**路由**檢查與複製（`tool_runs.attach_for_revise`），因為那裡有資料庫 session；順序是
  紀錄 → `.env` 閘 → 名額 → 版本比對。不存在的 id 回 `404 run_not_found`、別的工具的回
  `422 run_not_for_tool`，結構化的 `{code, message}`。前端的 404 一律被 `messageFor` 換成「找不到
  項目」，所以 `run_not_found` 的提示文字由前端自己寫。
- 後端不要求附上的紀錄必須「可修」：舊版本的失敗也可以附（提示會寫明它跑的不是工作區這一版），
  按鈕只出現在 `fixable` 的紀錄上。重複付費的防線是 `expected_vid`：修好並發布後，同一個頁面再按
  會得到 `409 version_mismatch`。
- 複製在送出時完成，內容以當下的已知秘密再遮一次（同讀取），之後在提示裡再經過一次遮蔽器。

### 提示

- 失敗紀錄與除錯程序放在**使用者提示**（只有附了紀錄才出現），系統提示只加 `examples.json` 的
  規則：安裝時把測過的輸入寫進去、只放唯讀輸入、最多 10 個；修訂時每個範例都重跑。
- 沒有意見時，意見欄寫 `(none) The operator attached the failing runs below and asks only that you fix them.`
- 每筆紀錄的每段文字都先遮蔽再依提示預算截斷，與修訂提示其他部分相同。

### provenance

`origin.feedback` = 操作者的意見（若有）+ 空行 + 「附上的失敗紀錄：#id（時間 UTC，結果，exit N）、…」。
只有編號、時間、結果與 exit code（不變量 V）。

### 前端

- 一鍵修正就是一次 `reviseMutation`：同一個寫入閘（`versionWritesBlocked`）、同一個 job 追蹤與
  卡片。列表列上的按鈕只在 `last_run.fixable` 而且這一列有可用版本時出現；「最近執行」每筆
  `fixable` 的紀錄有「請 AI 修正」與「加上說明…」（對話框，說明選填、上限同其他 AI 輸入）。
- 原本修訂卡片只在「AI 總結」展開時看得到；一鍵修正是從收合的列發起的，所以卡片改顯示在
  那一列上（r1 起一律在列上，見附錄 G6）。
- 修訂成功的卡片文字從「已依意見更新工具」改成「已發布工具的新版本」：一鍵修正沒有意見。
- **`/tools?job=<id>` 移到 P4**：它只服務其他頁面（擷取、補齊、協助更新）發起的修正，與它的
  使用者一起做、一起測。

### 驗收

| 不變量 | 測試（`tests/test_tool_builder.py`） |
|---|---|
| L | `test_router_revise_keeps_its_copy_when_the_records_are_purged`（送出後刪除紀錄，再執行工作：收到的仍是當時的內容） |
| M | `test_router_revise_refuses_unusable_run_ids_before_any_job`（沒有 job、沒有名額、沒有 LLM 呼叫） |
| N | `test_router_revise_accepts_run_ids_without_feedback`、`test_router_revise_needs_feedback_or_runs` |
| R | P2a 的 `test_fixable_is_a_failure_of_the_current_version`（含已丟棄版本＝`gone`） |
| V | `test_revise_provenance_names_the_runs_but_not_their_contents` |

另有 `test_revise_prompt_carries_the_failing_runs_and_the_procedure`（工作區版本與舊版本的措辭、
`output_overflow` 的說明、秘密在提示裡被遮）與 `test_builder_prompts_ask_for_examples_and_their_rerun`。
前端的請求內容與拒絕提示在 `toolSummary.js`，有 vitest；另用實際的瀏覽器跑過：列上的按鈕送出
`{run_ids, expected_vid}`、「加上說明…」多帶 `feedback`，卡片顯示在列上，LLM 未設定時清楚失敗。

### D45 附錄（P3 review r1，Fable）

| # | 問題 | 處理 |
|---|---|---|
| G1 | `run_ids` 沒有上限，超過 SQLite 整數範圍的 id 在查詢時丟 `OverflowError`，變成 500 | **採納**，並一起補上同類的兩處：`GET/DELETE /api/tool-runs/{run_id}` 與 purge 的 `max_id`。`SQLITE_MAX_INT` 從 `routers/items.py` 移到 `models.py`，三處與 items 共用。文件說「重複的 id 只算一次」與實作（先檢查上限 3 再去重）不符：**改文件**——上限是對送出的清單算（請求本身的上限），重複的 id 只附一次。UI 只送一個 id |
| G2 | 沒有意見時的固定句子 `_NO_FEEDBACK` 也經過遮蔽器；`.env` 有一個值剛好等於句中某個字時，句子被打碎 | **採納**：只有操作者／檔案來的文字要遮蔽（`_revise_system_prompt` 的既有規則），固定句子原樣放入 |
| G3 | 提示裡每段都是保留開頭的截斷，但 stderr 存的是結尾、標頭也說 stderr 保留結尾；預算小時最後幾行（真正的錯誤）被丟掉 | **採納**：stderr 改用保留結尾的 `_truncate_tail_to`（標記放在前面），其他三段照舊保留開頭 |
| G4 | 版本不同的紀錄一律說「OLDER version」，但可能是已丟棄的較新版本或同名的另一個套件；`error` 結果沒有說明文字 | **採納**：改說「a DIFFERENT version」；`_RUN_OUTCOME_PROSE` 補上 `error`（後端在這次呼叫周圍失敗，不是工具自己的結束代碼） |
| G5 | 「最近執行」的修正按鈕只看頁面的寫入閘；重新展開的列先顯示快取清單，`fixable` 可能已過期，重新讀取失敗時更會一直過期 | **採納**：按鈕（含「加上說明…」對話框）在清單 `isFetching`／`isError` 時停用；讀取失敗時顯示橘色「可能已過期」提示。與 AI 總結的 `displayedMayBeStale` 同一條規則 |
| G6 | 「卡片只出現一次」不成立：AI 總結展開但總結讀不到（例如修正剛發布、新版本的總結還沒對上）時面板提早返回，卡片哪裡都看不到 | **採納**：修訂卡片一律顯示在列上，從 AI 總結面板移除 |

測試：`test_router_revise_needs_feedback_or_runs` 加上超出範圍與「送出四個相同 id」兩例；
`test_revise_prompt_keeps_its_own_no_feedback_words_whole`、`test_revise_prompt_keeps_the_end_of_a_long_stderr`、
`test_truncate_tail_to_bounds`、`test_failing_run_text_names_another_version_without_guessing_its_age`、
`test_failing_run_text_explains_a_backend_error`；`test_tool_runs.py` 的單筆路由與 purge 驗證各加上超出範圍的
422。

## D46（P4）：AI 結果旁的修正入口

範圍是設計 §8.2 的第三個入口，以及從 P3 移過來的 `/tools?job=<id>`。下面是實作時才定下的細節。

### 後端

- 收集器是 `tools.collect_tool_failures()`（ContextVar），由 `routers/ai.py` 的
  `_advertised_tools_lock` 一起開：每個會廣告工具的請求都經過它，沒有路由能呼叫工具卻不收集。
  `_record_run` 在寫完紀錄之後把失敗加進目前的收集器；不在 AI 請求裡（直接呼叫服務、P5 的驗證）
  就沒有收集器，什麼都不加。
- 只收**可修的四種**（`tools.RUN_FAILURE_OUTCOMES`，也就是 P2a 的 `FIXABLE_OUTCOMES` 的出處，
  現在兩邊共用一個集合）。`refused` 沒有執行、`error` 是後端的問題，放在 AI 結果旁只會讓人去修
  一個沒壞的工具；它們照樣在工具頁的執行紀錄裡。
- 紀錄沒寫成的失敗照樣列出（`recorded: false`），前端只給連結。

### 前端

- 提醒以**工具**為單位：一次請求可能呼叫好幾個工具，而一次修訂只能修一個工具。每組附上那個工具
  這次已寫成紀錄的失敗，最多最新 3 筆。
- 按下時用 `fetchQuery`（`staleTime: 0`）重讀工具清單再判斷：版本相同才送出；不同、工具不見了、
  或沒有紀錄時不送，只說明原因。`409 version_mismatch` 重讀一次再判斷，第二次就照實說；
  `job_busy`、`env_policy`、`run_not_found` 各有說明。
- `/tools?job=<id>`：路由的 `validateSearch` 只接受 32 位小寫十六進位，其他值丟掉。工具頁的
  「已安裝工具」上方顯示那個工作的卡片；進行中時加入寫入閘（與本頁發起的工作相同），結束後
  以工具**名稱**重新讀取清單、總結與執行紀錄，不寫任何以 vid 為 key 的快取。本頁自己追蹤的工作
  不重複顯示。工作被逐出或後端重啟時沿用既有的「找不到這個修訂工作」文案。

### 驗收

| 不變量 | 測試（`tests/test_tool_runs.py`） |
|---|---|
| Q | `test_capture_reports_the_tool_that_failed_beside_its_result`、`test_a_failure_whose_record_was_not_written_is_still_reported`、`test_a_request_that_ends_in_502_still_records_its_tool_failure`、`test_concurrent_requests_each_collect_only_their_own_failures`、`test_successes_refusals_and_backend_errors_are_not_reported_as_tool_failures` |

前端的分組、判斷與文字在 `toolFailures.js`（含 `/tools?job=` 的驗證），有 vitest。另用實際的瀏覽器
跑過整條路：e2e 的 mock LLM（`#variant:tool_roundtrip`）讓快速捕捉呼叫一個會失敗的工具，結果旁
出現提醒，按「請 AI 修正」送出修訂，「到工具頁看進度」開啟 `/tools?job=<id>`，卡片接上那個工作
並顯示結局。

### D46 附錄（P4 review r1，Fable）

審查結論為收斂（沒有 P1／P2），下列一併處理：

| # | 問題 | 處理 |
|---|---|---|
| H1 | 互動期限（`asyncio.timeout`）在工具執行中觸發時，取消直接送到 handler 的 await；`CancelledError` 不是 `Exception`，所以那次呼叫既沒有紀錄也沒進收集器，工具卻在被丟下的 worker 執行緒裡繼續跑。設計 §11 的 Q 與 README 說「502 時紀錄仍寫入」只對上游錯誤成立；`llm.py` 的 F2 註解說期限「無法搶先中止進行中的工具」也不對——中止的是請求，不是工具 | **採納，修程式**：每次呼叫（含紀錄）在 `_make_handler` 裡以獨立 task 執行、用 `asyncio.shield` 等它；請求被取消時呼叫照樣跑完、遮蔽、寫紀錄。呼叫持有**自己的**共享鎖參照（handler 被呼叫時對請求的 fd 做 `os.dup`），所以請求關掉自己的 fd 不會在工具還在跑時放掉鎖，也不會留給呼叫一個可能已被重新配發的 fd 號碼；鎖在紀錄寫完後才放。順帶修好兩個既有的邊角：直接呼叫者自己取的鎖在取消時不會漏掉、`.env` 值在輸出遮蔽完之前不會被釋放。`llm.py` 的三處 F2 註解改寫成實際行為：請求準時結束，最多一個呼叫在背景多跑一個工具逾時 |
| H2 | 補齊／協助更新的提醒以工具名稱為 key、狀態存在元件裡；同一個工具在下一次結果又失敗時 React 沿用舊狀態，仍顯示「已請 AI 修正」與舊工作連結，不提供新的修正 | **採納**：`ItemAiActions` 為每個動作的結果編號，提醒以編號為 key，新結果一定從頭開始 |
| H3 | 修訂工作的 `tool_name` 只在結束時寫入：`/tools?job=` 進行中的標題沒有工具名稱；走 backstop 結束的工作 `tool_name` 為 null，只重讀工具清單 | **採納**：修訂與健康檢查在 `_admit_job` 就帶上名稱；結束時結果沒有名稱就不覆蓋 |
| H4 | 讀完清單到送出之間工具被刪掉時，`Tool not found`（沒有 code 的 404）被 API client 換成「找不到項目」 | **採納**：`fixRefusalMessage(error, toolName)` 對沒有 code 的 404 說「工具「名稱」已不存在。」 |
| H5 | 手打的 `/tools?job=<32 位數字>` 會被路由的預設 `parseSearch` 當成 JSON 數字解析而丟掉 | **不處理**：頁面產生的連結會加引號、可以往返；32 個字元全是數字的 uuid4 hex 機率約 (10/16)^32 |

測試：`test_a_call_cut_off_by_the_interaction_deadline_still_ends_and_is_recorded`（請求在期限結束時還沒有
紀錄、鎖仍被持有；呼叫結束後紀錄是真正的結果，鎖才釋放）；`test_revise_job_records_its_outcome_and_backstops_bugs`
加上排隊時與 backstop 後的 `tool_name`；`toolFailures.test.js` 加上被刪除工具的說明。

## D47（P5）：跑一次範例與修正後自動驗證

範圍是設計 §10 的後端執行、§8.3 的自動驗證、§9 的驗證狀態。（D46 是 P4。）下面是實作時才定下的細節。

### 執行

- `tools.run_examples` 是兩者共用的執行函式：取得共享 tools lock 並 `bind_request_tools_lock`，
  再用 `checkable_tool(name)` 建出那個工具的 handler——它只掃描那一個套件，但用的是
  `enabled_llm_tools` 同一組函式（`scan_installed`、`_build_llm_tool`），所以身分檢查、遮蔽與執行
  紀錄完全相同（不變量 P）。停用或無效的工具回報原因，不執行。
- 每次呼叫的 outcome 從新的 `CheckContext`（ContextVar）取得：handler 回給模型的只有字串，
  outcome 在 `_record_run` 裡才知道；同一個 context 也讓紀錄帶上 `workflow` 與 `check_id`。紀錄器
  沒接上時 outcome 照樣收得到（`run_id` 為 None）。
- 一個範例在後端出錯（`error`）時 handler 會拋出；執行函式記下那個 outcome 後繼續下一個。
- **預算**：每個範例開始前檢查已用時間，超過 `TOOL_CHECK_BUDGET_SECONDS` 就把其餘標為 `not_run`、
  `budget_exhausted`；第一個 `timeout` 之後其餘標為 `skipped`（不變量 S）。單一範例仍受
  `LLM_TOOL_TIMEOUT_SECONDS` 限制，所以實際上限是預算加一個範例的逾時。
- `examples.json` 讀不到、不是 JSON、形狀不對、超過 10 個或單一範例缺 name／args 時，整份不跑
  並回報原因：只跑看得懂的一部分，會把一次不完整的驗證說成通過。範例名稱截到 200 字。

### job

- `InstallJob` 多了 `kind`（`install`／`revise`／`health_check`）與 `verification`。原本刻意不放 kind，
  因為兩種 job 的卡片相同；健康檢查不建置任何東西，卡片不同，輪詢的人必須知道手上是哪一種。
- 健康檢查的送出順序：取得名額 → 版本比對（不變量 K 的順序）→ 工具能不能跑、有沒有可用的範例；
  不能跑就回 `409 check_unavailable`（附原因），不建立 job。job 期間持有名額，執行範例期間持有共享
  鎖，所以刪除回 `ai_job_in_progress`、丟棄回 `job_busy`，都不動任何東西（不變量 T）。
- 自動驗證在修訂 job 裡、發布與總結**之後**執行，`check_id` 是那個修訂 job 的 id（由 `_run_job`
  設定的 `_CURRENT_JOB_ID`），`expected_vid` 是剛發布的版本；`current` 在這之間被移走就回報
  `version_changed`，不去驗證別的版本。驗證本身出錯不會讓已發布的修訂變成失敗。
- 進度：每跑完一個範例就更新 job 的 `verification`（`finished: false`），卡片顯示「已發布新版本，
  正在跑範例（2/5）」。
- 範例名稱是 builder 寫的版本內容，放進 job 時經過遮蔽器；已知秘密的掃描失敗時改用「範例 N」，
  不讓結果因此遺失。

### 驗證狀態

- 由目前版本最新一批（`check_id`）`verify`／`health_check` 紀錄推導，總數用 `examples.json` 目前的
  範例數；這一批跑的比總數少（預算用完、被略過、或之後加了範例）就是 `incomplete`。舊版本的通過
  不算數。
- 放在 `GET /api/tools/{name}/runs` 的回應裡（「最近執行」展開時才讀），不放在工具列表：列表要為
  每個工具讀一次 `examples.json`，而狀態只在詳情裡看。

### 前端

- 「跑一次範例」是 `checkMutation`，成功後設成面板的 `activeJob`，與修訂共用卡片位置、寫入閘與結束
  後的重新讀取（現在也重讀頁首的筆數）。只有目前版本有範例時才提供按鈕。
- `ToolJobProgress` 以 job 自己的 `kind` 決定卡片：健康檢查有自己的卡片（逐個範例的結果、失敗的
  可以一鍵修正）；修訂成功的卡片附上驗證結果，失敗時轉紅並提供「請 AI 修正」（附上失敗範例的紀錄，
  最多 3 筆）與「丟掉這一版」。修訂發布後那一列會以新版本重新掛載，卡片因此出現在面板層級；面板
  只在那個版本仍是工具的目前版本時提供這兩個按鈕，「丟掉這一版」走既有的確認框，而且只在那一列
  本來就會提供時才出現。
- `WORKFLOW_LABELS` 加上 `health_check`（跑一次範例）與 `verify`（修正後驗證）。

### 驗收

| 不變量 | 測試 |
|---|---|
| P | `test_a_check_runs_each_example_through_the_tool_path_and_records_the_batch`、`test_a_disabled_tool_is_not_run_and_says_so`、`test_a_published_fix_is_verified_against_its_examples_only`（附上的原始輸入沒有被後端執行）、`test_a_published_fix_of_a_disabled_tool_is_reported_unverified` |
| S | `test_after_the_first_timeout_the_rest_are_skipped`、`test_past_the_budget_the_remaining_examples_are_not_run` |
| T | `test_delete_and_discard_are_refused_while_a_check_runs`、`test_a_running_health_check_holds_the_slot_against_a_discard` |

另有 `examples.json` 的解析（`test_an_unusable_examples_file_is_an_error_not_a_partial_list` 等）、
健康檢查 job 與路由的拒絕、驗證狀態的推導（`test_verification_state_follows_the_newest_batch_of_the_current_version`）。
前端的措辭與判斷在 `toolChecks.js`，有 vitest；另用實際的瀏覽器跑過：「最近執行」顯示「有 2 個範例，
尚未驗證」，按「跑一次範例」後卡片列出逐個結果與「請 AI 修正」，驗證狀態與頁首筆數隨之更新。
