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
