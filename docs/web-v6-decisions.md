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
- `contains_secret` 的判斷是「遮蔽器有沒有改動這個值」。這是零猜測的判斷，涵蓋所有套件的 `.env`
  與進行中登記的秘密；回傳的值本身也是遮蔽後的。
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
