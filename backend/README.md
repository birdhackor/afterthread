# afterthread Backend

`afterthread` file-based 方法論的 web 化後端：同一套快速捕捉／全面補充／回顧
方法論，改成一個 FastAPI + SQLite 服務，供 `frontend/` 的 SPA（或任何 HTTP
client）呼叫。與 repo 根目錄既有的 file-based MVP（`.opencode/`、`memory/`、
`scripts/afterthread.py`）並存，彼此不互相依賴。

## 技術棧

- **FastAPI**（`fastapi[standard]`）+ **Uvicorn** — HTTP API
- **SQLModel**（SQLAlchemy 2.x + Pydantic v2）— ORM / schema
- **SQLite** — 唯一支援的資料庫後端（見下方設計筆記）
- **pydantic-settings** — 從 `backend/.env` 讀設定，啟動時就驗證邊界
- **openai**（`AsyncOpenAI`）— 呼叫任何 OpenAI-compatible endpoint
- **uv** — 套件管理與執行器；`ruff`（format + lint）、`ty`（型別檢查）、
  `pytest` 為 dev dependency group
- Python `>=3.14`（見 `.python-version`）

## 指令

全部從 `backend/` 目錄執行：

```bash
uv sync                                        # 安裝依賴（含 dev group）；本專案自身也會被裝成
                                                # editable（見 pyproject.toml，已移除 [tool.uv] package = false）
uv run uvicorn afterthread.main:app --port 8000        # 啟動 API（DB 不存在會自動建立）
uv run uvicorn afterthread.main:app --port 8000 --reload   # 開發時加 --reload 自動重載
uv run afterthread --port 8000              # 打包模式的 console script；資料目錄/`.env`
                                                # 邏輯見 afterthread/cli.py（`--help` 看完整選項）
uv run afterthread init-env                  # 將套件內 env.example 範本寫到資料目錄，存成 .env（可再編輯）

uv run ruff format .                           # 格式化
uv run ruff format --check .                   # 只檢查格式（CI / gate 用）
uv run ruff check .                            # lint
uv run ty check                                # 型別檢查
uv run pytest                                  # 測試（應全數通過；精確數字隨版本演進，
                                                # 以指令實際輸出為準）
```

PyPI 使用者可直接以 `uvx afterthread` 啟動，或用 `uv tool install afterthread`
常駐安裝。支援 Python 3.14 的 Linux／macOS；Windows 尚未列入支援範圍。

從 checkout 打包內嵌前端 build 的可攜 wheel：見根目錄
`scripts/build-wheel.sh`；對應的 packaged-mode e2e 驗證見
`e2e/wheel_smoke.sh`。正式發布流程與一次性 Trusted Publishing 設定見
`docs/releasing.md`。

以上指令皆已在本機實際執行過並確認通過（`uv sync` / format --check / check /
ty check / pytest 全綠；`uvicorn` 啟動後 `GET /api/health` 回
`{"status":"ok"}`，未設定 `.env` 時 DB 檔會自動建在
`backend/afterthread.db`，`GET /api/llm/status` 回
`{"configured":false,"model":null}`）。

## 環境變數（`backend/.env`，範本見 `backend/afterthread/env.example`）

所有數值型設定都在 `afterthread/config.py` 用 pydantic-settings 的 `Field(..., ge=/le=/gt=)`
在**啟動時**驗證邊界；超出範圍會讓服務直接啟動失敗，而不是留到第一次呼叫才爆炸。

| 變數 | 預設值 | 說明 |
| --- | --- | --- |
| `TLS_NO_VERIFY` | `false` | 關閉本服務**所有**對外連線的 TLS 憑證／主機名稱驗證：OpenAPI 文件下載、LLM endpoint 呼叫，並以 `TLS_NO_VERIFY=1` 透傳進已安裝工具（與安裝器 `run_shell`）的子行程環境（工具是否遵守則看工具自身實作）。僅供內網自簽憑證／私有 CA 環境使用；打開後上述連線即可能遭中間人攻擊，是維運者主動的安全取捨，絕不會被靜默開啟。 |
| `OPENAI_BASE_URL` | `""`（空） | OpenAI-compatible endpoint 的 base URL。留空＝AI 功能未設定，手動 CRUD 不受影響。 |
| `OPENAI_API_KEY` | `""`（空） | 對應 endpoint 的 API key。**不是**判斷「是否已設定」的條件之一——有些相容 gateway 不需要 key。 |
| `OPENAI_MODEL` | `""`（空） | 呼叫該 endpoint 時使用的 model 名稱。與 `OPENAI_BASE_URL` 兩者都非空、且 base URL 可被解析為合法 http/https URL，才算「已設定」。 |
| `OPENAI_TIMEOUT_SECONDS` | `120` | 單次 LLM 請求的逾時秒數，邊界 `(0, 1800]`。同時是「第一次呼叫 + 一次修正重試」整體的 wall-clock 上限（`asyncio.timeout` 包住整個嘗試迴圈）。預設值已經是為長 context 模型調校過的（見下方 GLM5.2 備註），不是舊版小 context 模型的 60 秒。 |
| `OPENAI_MAX_OUTPUT_TOKENS` | 未設定 | 選填，邊界 `[1, 1000000]`。設定時才以 `max_tokens` 送給 endpoint；留空＝完全不送這個參數（部分 reasoning endpoint 會拒絕顯式 `max_tokens`，但有些 gateway 預設完成長度太短，會截斷長回覆——這是給後者用的上調旋鈕）。 |
| `LLM_PROMPT_BUDGET_TOKENS` | `200000` | enrich / assist-update / 工具安裝組 prompt 時，內容序列化後的 **token** 預算，邊界 `[4000, 1000000]`。實際套用時經動態「字元↔token 比值」換算成字元上限（比值由近期互動的 usage 學得，冷啟動用保守值 1.0，比值下限 0.1 兼作絕對字元兜底——見 `services/token_budget.py`、`docs/tool-calling.md`）。改名自舊的 `LLM_PROMPT_BUDGET_CHARS`（舊鍵會被靜默忽略）。 |
| `LLM_LOG_MAX_ENTRIES` | `50` | 「AI 日誌」頁／`GET /api/llm/logs` 顯示的最近互動筆數上限（記憶體內環狀緩衝，隨程序重啟清空），邊界 `[1, 1000]`。 |
| `LLM_LOG_BODY_MAX_CHARS` | `200000` | 單次互動中，任一則請求/回應內容儲存時的字元數上限，邊界 `[1000, 2000000]`；與 `LLM_LOG_MAX_ENTRIES` 一起讓記憶體用量在兩個軸上都有界。 |
| `LLM_LOG_FILE` | 未設定 | 選填。設定後，每次 LLM 互動會額外追加寫入這個 JSONL 檔案（與記憶體環狀緩衝形狀相同的紀錄，一樣受 `LLM_LOG_BODY_MAX_CHARS` 截斷）；預設關閉——記錄含個人記憶內容，落不落地是使用者自己的隱私選擇。每一行連同行尾換行，追加前以寫出的樣子再檢查一次：已知秘密的某一處出現碰到文字欄位（本文、回覆、錯誤、工具名稱、model）時，這一行的文字欄位全部換成遮蔽標記，字數跟著改；仍碰到時（只會是沒分類的欄位）不寫這一行，只記一筆不含內容的警告；寫出時已知秘密的清單不完整，這一行的文字欄位全部改存「本文未記錄」的標記。整處落在鍵、標點、時間、計數與小寫識別字（workflow、outcome、role）裡的出現照寫。每一行各自檢查，橫跨兩行的值看不到。記憶體環狀緩衝與 AI 日誌頁照舊顯示原本的紀錄。 |
| `LLM_LOG_FILE_MAX_BYTES` | `50000000` | 上述 JSONL sink 的輪替門檻（位元組）：檔案超過此大小就改名成帶 UTC 時間戳後綴、另開新檔（守磁碟；RAM 由環狀緩衝負責），邊界 `[1000000, 1000000000]`。同一秒內已有同名的舊檔時改用 `-1`、`-2`…後綴，不會蓋掉它；查不到某個名稱是否已被占用時，這一次不輪替、照常寫在原檔，下一筆再試。僅在有設定 `LLM_LOG_FILE` 時有意義；輪替後的舊檔不會自動刪除，交由使用者自行清理。 |
| `TOOLS_DIR` | dev 未設定／打包模式自動注入 `<data-dir>/tools` | 已安裝工具套件所在目錄；留空＝工具功能整個關閉（`GET /api/tools` 回空清單，AI workflow 不帶任何工具，prompt 與無工具版本逐字相同）。相對路徑或指向此基底的 symlink 會在設定邊界 canonicalize 成同一個絕對路徑；這不會跟隨或放寬任何 package/version symlink 的拒絕。路徑裡還不存在的部分照原樣保留（工具目錄還沒建立時照常）；某一段是檔案時同樣是那裡沒有工具目錄：照設定的路徑使用（只轉成絕對路徑，不解析連結、不化簡 `..`），沒有工具，AI workflow 照常不帶工具，已知秘密的清單照常完整。解析時遇到其他錯誤（上層目錄無法搜尋、連結迴圈、I/O 錯誤）不會被當成功能關閉：工具清單、capture／enrich／assist-update 等用到工具目錄的請求回 `500 tools_dir_unresolved`，訊息寫出設定的路徑、錯誤型別與下一步（檢查上層目錄的權限、連結與磁碟狀態，或清空這個設定），已知秘密的清單也因此不完整。dev 模式要用工具功能，需自行在 `backend/.env` 設定這個變數。 |
| `LLM_TOOL_ROUNDS_MAX` | `8` | 一次 AI workflow 呼叫最多允許幾輪工具呼叫，邊界 `[1, 64]`。 |
| `LLM_TOOL_TIMEOUT_SECONDS` | `60` | 單次工具子行程的逾時秒數（到期整個 process group 被砍），邊界 `(0, 600]`。 |
| `LLM_TOOL_OUTPUT_MAX_CHARS` | `50000` | 工具 stdout 餵回給模型的字元數上限，邊界 `[1000, 500000]`。 |
| `LLM_TOOL_CONVERSATION_BUDGET_TOKENS` | `500000` | 工具迴圈中「實際送給模型的對話」token 預算，邊界 `[50000, 1000000]`；超過即停止附掛工具、走 finalize（防多輪工具結果累積撐爆 context/記憶體）。同樣經字元↔token 比值換算成字元上限。改名自舊的 `LLM_TOOL_CONVERSATION_BUDGET_CHARS`（舊鍵會被靜默忽略）。 |
| `TOOL_INSTALL_MAX_ROUNDS` | `24` | KB 網頁安裝器（見下方「工具（KB 網頁安裝器）」）單一安裝工作階段的工具輪數上限，邊界 `[4, 64]`。 |
| `TOOL_INSTALL_TIMEOUT_SECONDS` | `900` | KB 網頁安裝器單一安裝工作階段的整體逾時秒數，邊界 `(0, 3600]`。 |
| `TOOL_INSTALL_SHELL_TIMEOUT_SECONDS` | `120` | 安裝器內單一 `run_shell` 指令的逾時秒數，邊界 `(0, 600]`。 |
| `TOOL_CHECK_BUDGET_SECONDS` | `300` | 跑一次 `examples.json`（「跑一次範例」與 AI 修正發布後的自動驗證）的總預算秒數；超過就不再跑剩下的範例、標示未完成。執行期間持有共享 tools lock：預算是在每個範例開始前檢查的，已經開始的那一個會跑到它自己的 `LLM_TOOL_TIMEOUT_SECONDS`，所以刪除／丟棄最久會被擋住預算加一個範例的逾時。邊界 `(0, 3600]`。 |
| `TOOL_DRAFT_MAX_FILES` | `2000` | 一輪修訂保存成草稿（見下方「沒發布的修訂留成草稿」）時，工作區最多幾個項目（檔案、目錄、連結都算），存進草稿的複本也是（複製時逐一計數，換回的起點版本也算）；超過就不存檔案、記下原因。接續草稿時，把草稿的檔案複製進新的暫存工作區也受同一個上限（複製前先量一次、複製時逐一計數），超過就不開始那一輪；發布草稿（見下方「發布草稿（不經 AI）」）時的同一個複製也是，超過就不發布。存成新檔案目錄的保存都複製整個工作區並 fsync 複本，這個上限與下一個限制的是一次複製的量、fsync 的時間與一份檔案目錄佔的磁碟（接續留下的舊檔案目錄另外佔）。預設是 `list_dir` 列出上限 500 的四倍。邊界 `[1, 100000]`。 |
| `TOOL_DRAFT_MAX_BYTES` | `50000000` | 同上，工作區與存進草稿的複本各自的檔案內容總位元組上限（複本以實際寫入的位元組計，換回的起點版本也算），接續與發布草稿時複製進暫存工作區的也是。預設是估計值、不是量測：一般套件預期是幾十 KB，連依賴一起放進來的也只有幾 MB。邊界 `[1000000, 2000000000]`。 |
| `DATABASE_URL` | `sqlite:///./afterthread.db` | SQLAlchemy URL。**只支援 SQLite**，且必須是檔案型（不可為 in-memory）——見下方設計筆記。 |
| `STALE_AFTER_DAYS` | `14` | 非終態項目（`STALE_ELIGIBLE_STATUSES`：除 `done`／`superseded` 外的五種狀態）的 `updated` 超過這個天數，API 回應的 `is_stale` 會是 `true`。邊界 `[0, 36500]`。 |

> **GLM5.2（或其他 1M-token context 模型）備註**：上面 `OPENAI_TIMEOUT_SECONDS`
> （120）與 `LLM_PROMPT_BUDGET_TOKENS`（200000）的預設值本身已經是為大 context
> 模型調校過的數字。內部 LLM 若是 GLM5.2 這類 1M-token context 模型，
> `backend/afterthread/env.example` 內建了進一步調高的建議（取代上方預設）：
>
> ```bash
> LLM_PROMPT_BUDGET_TOKENS=800000  # 讓超大項目也能整項進 prompt，不截斷
> OPENAI_TIMEOUT_SECONDS=300       # 長 context 生成較慢，放寬逾時避免誤判 502
> ```

## API 概覽

所有路由掛在 `/api` 前綴下（見 `afterthread/main.py`）。完整 request/response schema
以啟動後的 `GET /docs`（Swagger UI）／`GET /openapi.json` 為準；以下是各路由的
用途摘要。

**Health**
- `GET /api/health` — liveness probe。

**Items CRUD**（`afterthread/routers/items.py`，前綴 `/api/items`）
- `POST /api/items` — 建立項目，並自動寫入第一筆 progress entry。
- `GET /api/items` — 分頁列表（`limit` 1–200，預設 50；`offset`），支援
  `status`／`stage`／`tag`（JSON array 精確比對，Unicode-safe）／`q`（對
  title／snapshot／recovery_keywords 做大小寫不分、Unicode-casefold 的模糊搜尋）
  篩選；依 `updated` 新到舊排序。
- `GET /api/items/{id}` — 單筆項目 + 完整 progress 歷史（依時間正序）。
- `PATCH /api/items/{id}` — 局部更新；有提供欄位即推進 `updated`（即使值與現值相同），空 payload 不推進。
- `DELETE /api/items/{id}` — 刪除項目；progress entries 透過 DB 層 FK cascade 一併刪除。
- `POST /api/items/{id}/progress` — 追加一筆 append-only progress entry。

**Review**（`afterthread/routers/review.py`）
- `GET /api/review` — 把五種非終態項目分成
  `needs_enrichment`／`active`／`waiting`／`parked` 四組，各組依 `updated`
  舊到新排序（單一查詢一次性快照，避免分組間 race）。

**AI workflows**（`afterthread/routers/ai.py`）
- `GET /api/llm/status` — 是否已設定 LLM（`configured`）與 model 名稱；不回傳
  base URL／API key。
- `POST /api/capture` — 把一段原始文字快速捕捉成結構化項目（LLM 呼叫在任何 DB
  transaction **之外**執行；失敗不留下任何列）。
- `POST /api/items/{id}/enrich` — 依 checklist 全面補充項目，回傳仍缺的
  gaps；checklist 完整時把 `stage` 推進為 `full`，並把仍在捕捉狀態的項目升級為
  `active`。
- `POST /api/items/{id}/assist-update` — 依一段近況文字追加 progress 並局部更新
  項目內容。
- `GET /api/llm/logs` — 「AI 日誌」頁用：最近的 LLM 互動摘要清單（`limit`
  1–500，預設 50），新到舊排序。回應另外帶一個 `process_token`：**這一頁的 id
  屬於哪一個行程**。AI 日誌的 id 是每個行程各自從 0 開始的計數器（環狀緩衝隨
  重啟清空），所以客戶端手上那個 id——例如瀏覽器快取住的工具工作卡片給出的
  `?log=<id>` 深連結——重啟後照樣解析得到，只是指到另一次互動；token 是前端唯一
  能分辨的依據。它掛在**外層信封**而不是每一列上：那是「回答這次請求的行程」的
  性質，不是任何一筆紀錄的欄位。
- `GET /api/llm/logs/{log_id}` — 單筆互動的內容（每次嘗試的請求訊息與回應，
  每則受 `LLM_LOG_BODY_MAX_CHARS` 截斷、超過每次嘗試總量預算的較早訊息會被
  省略成一則標記、工具呼叫參數只保留約 200 字元預覽，並另記當輪廣告給模型的
  工具名單）；記錄已被環狀緩衝擠出（見 `LLM_LOG_MAX_ENTRIES`）或程序重啟過則
  404。

`capture`／`enrich`／`assist-update` 的成功回應都另帶 `tool_failures`（web-v6 §8.2）：這次請求
裡失敗的已安裝工具呼叫（`failed`／`output_overflow`／`timeout`／`start_failed`；被拒與後端錯誤不算），
每筆 `{run_id, tool_name, vid, outcome, recorded}`。收集器放在每個廣告工具的請求都會經過的
`_advertised_tools_lock` 裡（ContextVar，每個請求各自一份）；紀錄沒寫成的失敗照樣列出，
`recorded: false`、`run_id: null`。LLM 失敗回 502 時沒有回應可放，但紀錄照樣寫進資料庫；
互動期限到了、工具還在跑時也一樣：請求在期限回 502，那次呼叫在背景跑完（最晚到它自己的
`LLM_TOOL_TIMEOUT_SECONDS`）才寫紀錄，期間仍持有共享鎖，刪除與丟棄照常回「AI 工作進行中」。

AI 路由的錯誤語意：`503 llm_not_configured`（未設定端點）、
`502 llm_upstream_error`（上游呼叫失敗或輸出無法解析，即使重試一次後仍失敗）、
`409 conflict`（enrich／assist-update 於 LLM 呼叫期間，項目被別的請求改動——樂觀
並發偵測）。

給操作者照著修的兩種 500（`afterthread/main.py` 的 app 層處理，任何路由都適用）：
`500 tools_dir_unresolved`（`TOOLS_DIR` 的路徑解析不了，見上方設定表）與
`500 known_secrets_incomplete`（有 `.env` 或工具目錄讀不到，已知秘密的清單不完整）。
`detail.message` 只寫路徑、工具名、錯誤型別與下一步，不含任何值，結尾不帶句號（頁面可能接著寫下去）；
路徑在 Linux 上不是 UTF-8 的位元組會換成替代字元（U+FFFD）。修訂、安裝的背景工作因這兩種錯誤停下時，
job 的 `error` 也是這段訊息（帶結尾句號）；其他未預期錯誤仍只寫型別名。

**Tools**（`afterthread/routers/tools.py`，前綴 `/api/tools`；即「工具」頁的
後端）。回應裡的後端路徑（欄位型別 `schemas.BackendPath`），以及 `500 tools_lock_unavailable` 與
`500 draft_not_removed` 訊息裡的路徑，在 Linux 上不是 UTF-8 的檔名位元組會換成替代字元（U+FFFD）：
這樣的路徑不能原樣拿去開檔或複製，但回應照常送出；路徑本身不遮蔽。
- `GET /api/tools` — 列出所有已安裝工具套件（含無效的），依名稱排序；`TOOLS_DIR`
  未設定或尚無工具時回空清單（非錯誤）。每列帶 `current_vid` 與三態
  `lineage`（`sole`／`usable`／`broken`）；套件沒有可解析的生效版本時仍會列出，
  但 `description`／`current_vid` 為 `null`、`valid=false`、`lineage=broken`，只能
  整包刪除。`description` 可為空不是把 manifest 契約放寬，而是讓壞掉的套件仍有
  一列可供清理。若非空套件完全沒有 `.afterthread.meta/`，只有 migration legacy
  preflight 也接受的扁平 shape（尤其不能已經有 `versions/`）才會提示執行
  `python -m afterthread.migrate_tools_v5`；保有 target namespace 的模糊 shape 會
  回報損壞、要求人工檢查，不會推薦一條 migration 必然拒絕的指令。在 Linux 上，工具目錄裡名稱不是 UTF-8
  的資料夾也列成一列：`name` 裡那些位元組換成替代字元（U+FFFD），`valid=false`、`last_run` 為 `null`。
  這樣的名稱不是合法的工具名稱，`/api/tools/{name}` 底下的路由都以 422 拒絕它；要在檔案系統上自行改名或移除。
- `PATCH /api/tools/{name}` — 切換某工具的 `enabled`；找不到回 404。寫的是套件的
  `.afterthread.meta/state.json`（見下方「工具套件格式」），**完全不碰任何版本的
  `tool.json`**——所以一次
  切換不會移動 manifest 身分，不會讓進行中的修訂作廢、也不會讓一趟總結往返的
  寫入被拒。**修訂或重新產生進行中也可以切換**：修訂只新增
  `versions/<vid>` 並切換 `current`，套件層根本不會被換掉，因此不再需要
  狀態搬運或 package-state 發布鎖，前端也不必把開關鎖起來。
  `tool.json` 讀不到／過大／是 FIFO 都不再是拒絕理由（那些檢查守的是
  已經不存在的 manifest 改寫），因此 manifest 壞掉但 `current` 可解析的套件仍可
  切換；它照樣列成無效、照樣不會被端給模型。`current` 無法解析時則拒絕切換，
  因為沒有一個版本可讓該列描述。**另一條 404 的理由**：`state.json` 是可讀但沒有
  backend marker 的 foreign 檔案時，這條路由拒絕而不是覆蓋（見下方）。
- `DELETE /api/tools/{name}` — 將整個工具套件從 registry 移除；找不到回 404。
  名稱本身是連結時只移除連結，不碰它指向的地方。這一筆的查詢失敗（看不出它是連結還是目錄），
  或解析出來不是 `<tools_dir>/<name>` 本身時，也回 404，不動任何檔案。
  只有非阻塞 exclusive tools lock 取得成功才會改名或刪除；AI request 或繼承 shared
  descriptor 的工具子行程仍活著時，回獨立的 `409 ai_job_in_progress`，套件完全不動。
  工具從清單消失（`removed` 或 `retained`）之後，它的執行紀錄一併刪除，回應的
  `runs_deleted` 說明有沒有刪成（`false` 時工具照樣已刪除，紀錄改由 purge 的孤兒範圍清掉）；
  404／409 時紀錄不動。
  取得 lock 後先把套件改成隱藏的 `.stale-` 名稱，再盡力遞迴移除；這個 rename 是為了
  讓 registry 消失與失敗後清理可重試，不再負責保護執行中的目錄。成功（清理後的 `lstat` 回「不存在」或「某一層不是目錄」）回 200
  `{outcome: "removed", retained_path: null, retention_reason: null}`；若遞迴清理失敗或無法確認已清掉，
  回 `{outcome: "retained", retained_path: "<隱藏目錄的絕對路徑>",
  retention_reason: "cleanup_failed"}`。兩種回應下工具都已不可見；後者表示設定與
  金鑰檔仍在、或可能仍在磁碟，修復權限／檔案系統問題後才手動清理。
- `DELETE /api/tools/{name}/versions/{vid}` — 丟掉畫面所指的**精確目前版本**。後端先
  取得全域工具名額，再比對 path 裡的 `vid`；不相符回
  `409 version_mismatch`。`lineage=usable` 時把 `current` 原子切回
  `origin.json.previous`，之後才盡力清掉原版本。和整包刪除一樣，未取得非阻塞
  exclusive tools lock 時回 `409 ai_job_in_progress`，不發布 pointer、不移動檔案。
  pointer 的目錄 fsync 已確認後，原版本先改名成 `<vid>.discarded` 再刪除；成功（清理後的 `lstat` 回「不存在」或「某一層不是目錄」）回
  `{outcome: "removed", retained_path: null, retention_reason: null}`。清理失敗或無法確認已清掉時回
  `retention_reason: "cleanup_failed"`，UI 提示檢查權限／檔案系統後處理；若
  `current=P` 已換上但目錄 fsync 未確認，V 保留原位並回
  `retention_reason: "durability_unconfirmed"`，UI 明確禁止手動刪除，要求先修復
  儲存問題、重啟並確認 pointer 仍指 P。停止行程本身不能補上 durability invariant。
  `lineage=sole` 在 UI 走既有的整包刪除確認；
  前一版缺失、未提交或指回自己則回 `409 lineage_unavailable`，不動任何檔案。
- `DELETE /api/tools/{name}/draft?draft_id=…&generation=…`（或 `?discard_token=…`）— 放棄工具的
  草稿（見下方「沒發布的修訂留成草稿」），連檔案一起刪。以 `draft_id`＋`generation` 指名，說明檔
  讀不出來的草稿改用清單給的 `discard_token`，兩種恰好給一種（否則 422）。先取得全域名額
  （`409 job_busy`），所以期間沒有任何保存或 sweep；再持共享 tools lock，刪除工具因此回
  `409 ai_job_in_progress`。草稿不是呼叫者指名的那一份（之後又被寫過，或放棄後又建了一份新的）回
  `409 draft_changed`；工具不存在回 404（`TOOLS_DIR` 未設定也是），工具沒有草稿回
  `404 draft_not_found`。任何狀態的草稿都能放棄。`draft/` 先改名成
  `.afterthread.meta/draft.discarded-<hex>` 再刪；回應 `{residual_path}` 是刪不掉而留下的路徑（沒有就是
  `null`），之後安裝、修訂或發布草稿工作收尾時的 sweep 會再收。`draft/` 只在 `lstat` 回「不存在」時才算刪掉：
  改名不成、原地也沒刪乾淨，或 `draft/` 看不到（`lstat` 是別的錯誤）時回 `500 draft_not_removed`，
  訊息寫出 `draft/` 的路徑，不算放棄成功。動手之前就看不到的草稿（`draft/` 或它所在的
  `.afterthread.meta/` 的 `lstat` 是「不存在」以外的錯誤）原樣不動，同樣回這個 500：看不到就無法確認
  它在，也無法排除 `.afterthread.meta/` 是指向別的工具的連結。以 `discard_token` 放棄時，在鎖內重新
  觀測一次再比對：其中任何一項查詢失敗（不是回報「不存在」或「某一層不是目錄」），就無法確認是同一份
  草稿，什麼都不刪，同樣回這個 500——清單在這種情況下給的 `discard_token` 只供顯示。取不到共享
  tools lock（工具目錄不是看得到的目錄）時，只有工具目錄的 `lstat` 回「不存在」或「某一層不是目錄」
  才回 `404 draft_not_found`，其他情況同樣回這個 500。宣告在 `/jobs/{job_id}` 之前（工具可以叫
  `jobs`）。
- `POST /api/tools/install` — 送出 KB 網頁安裝器工作（見下方「工具（KB 網頁
  安裝器）」）；202 + `job_id`，建置在背景執行。`TOOLS_DIR` 未設定回 `503
  tools_not_configured`；名額被佔用時回 `409 install_in_progress`。**那個名額
  不是安裝專屬的**：安裝、AI 修訂與同步的「重新產生總結」共用同一個 single-flight
  （見下方三條路由與「依意見修訂既有工具」），所以這個 409 最常見的來源其實是
  別的分頁正在修訂。代碼與訊息字串維持既有的 `install_in_progress`／
  「已有安裝正在進行中，請等待其完成」（前端有 pin 住的分支，不動），但前端**刻意
  覆寫**成中性文案「已有工具任務正在進行中（安裝、AI 修訂或重新產生總結），請等待
  完成後再安裝」——指名一個使用者從沒送出的「安裝」只會讓他去找一個不存在的東西。
- `GET /api/tools/jobs/{job_id}` — 輪詢一個工具工作（安裝、修訂、跑一次範例或發布草稿）的狀態
  （`queued`／`running`／`succeeded`／`failed` + 完成後的 `tool_name`／
  `summary`／`llm_log_id`／`llm_log_process`／`outcome`／`kind`／`verification`）；`kind` 是
  `install`／`revise`／`health_check`／`publish_draft`，`verification` 是範例的執行結果（健康檢查的全部結果，
  或修訂、發布草稿之後的自動驗證），執行中就會逐個更新；`outcome` 是終局的細分
  （`published`／`not_published`／`failed`，進行中為 `null`），`not_published` 指
  builder 自己回 `ready=false`、沒有發布任何東西——前端以橘色而非紅色呈現，且不宣稱
  原因是外部還是修不好（web-v6 §8.3）。修訂的 builder session 跑過之後，工作另帶後端
  **量到**的結局（`llm.StructuredRunStats`，不是模型的自述）：`stopped_by` 是結束工具
  階段的預算——`rounds`（用完 `max_rounds` 次工具回合，即 `TOOL_INSTALL_MAX_ROUNDS`）
  或 `conversation`（對話大小超過 `LLM_TOOL_CONVERSATION_BUDGET_TOKENS`），沒有就是
  `null`；`failure` 是 session 最後怎麼失敗：`empty_response`（最後的回覆沒有內容）、
  `invalid_output`（更正一次後格式仍錯）、`upstream`（其他上游錯誤，連線失敗的
  `APIConnectionError` 也在這一類）、`request_timeout`（單一請求等候回應逾時：SDK 依
  `OPENAI_TIMEOUT_SECONDS` 擲出的 `APITimeoutError`，或請求本身在整場期限觸發前擲出的
  builtin `TimeoutError`）、`deadline`（整場時間 `TOOL_INSTALL_TIMEOUT_SECONDS` 用完）、
  `not_configured`，正常回答為 `null`。兩者可以同時成立，例如回合用完迫使收尾、
  收尾那次又回了空內容。`rounds_used` 是用掉的工具回合數：通過數量與大小檢查、被接受的工具回覆
  才算一回合，session 結束時正在跑的那一回合也算（例如期限在第一回合的工具執行中觸發，記 1）；
  `max_rounds` 是給定的回合數。
  `handoff`（至多 4000 字，給接手者的交接筆記）與 `operator_action`（至多 1000 字，需要
  操作者在工作區外動手的事）是模型自己寫的，先遮蔽再截斷，只供顯示與交接，後端不據以做
  任何判斷。這六個欄位在安裝、跑範例、發布草稿、session 開始前就結束的修訂，以及因未預期錯誤
  而中止的工作上都是 `null`；`run_shell` 指令或 `write_file` 寫入無法確認停止的修訂只帶前四個，`summary`、
  `handoff`、`operator_action` 都是 `null`（AI 日誌仍有整場紀錄）。修訂另帶 `draft`：這一輪的草稿怎麼了
  （`{saved, draft_id, generation, attempts, reason, retained_path}`）。`saved` 是草稿是否持有這一輪工作區
  的每個條目——對條目做出判定而不存的除外（命中已知秘密、不留的連結、看得到是特殊檔），有條目因為查詢
  失敗而沒存時就是 `false`（見「看不了的條目不存檔案」）；接續時工作區與帶進來的內容相同，只記下這一輪、
  沿用原本的檔案目錄，也算——但只在鎖內確認那個檔案目錄現在的雜湊仍等於開工時複製出來的那份時；這一輪
  進行中它被改過、被移走或讀不完整時是 `false`，原因說明這件事，暫存保留並回報路徑；`saved` 為 `true` 時
  `reason` 是附註（例如上一份檔案目錄沒刪、留在哪裡、為什麼），為 `false` 時是沒存的原因。發布成功而草稿
  留下時，`reason` 是它為何留下的說明：確認過草稿只有發布前存的那份、刪除卻沒完成時請你放棄它；草稿還有
  額外的項目（例如接續留下的舊檔案目錄）或內容不同、或無法確認時，請你先取出需要的內容再放棄；接續的發布前
  保存沒存成、發布卻成功時，說這一輪沒存進草稿。`attempts` 是這份草稿累計的輪數。`retained_path` 是成果在
  草稿之外留著的地方——暫存工作區，或發布搬移到一半時已在 `versions/` 底下、`current` 沒有指到的那一版：
  `saved` 為 `false` 時成果就在那裡、沒有存成草稿；為 `true` 時表示說明檔沒確認落盤，成果另外留在那裡。
  工作區本身確認不了（可能已不是當初那個暫存目錄）時不給路徑。`draft_id`／`generation`／`attempts` 是這一輪
  最後一次成功寫入說明檔時的那一份（接續而開工之後沒有寫入成功時，就是開工時那一份）；第一輪什麼都沒寫成時
  為 `null`。沒有值得存的東西、發布成功且草稿已刪除、指令或寫入無法確認停止（什麼都不存）時，以及上面六個欄位為
  `null` 的情況，`draft` 都是 `null`。
  發布草稿的工作沒有 session，也不呼叫模型：上面六個欄位與 `summary`、`llm_log_id` 都是 `null`，也不帶
  `draft`；另帶 `draft_publish`（`{draft_id, generation, removed, reason, retained_path}`），說它對草稿做了什麼
  （見下方「發布草稿（不經 AI）」）。`generation` 是工作最後留給草稿的那一個：沒寫過說明檔時是判定時的，寫了
  「正在發布」的標記是標記寫入的，收回標記後是收回寫入的——放棄這份草稿要帶的就是它。`removed` 只在發布成功、
  之後親眼看到 `draft/` 已不存在時為 `true`。`reason`：發布成功而草稿留下時是留下的原因，分得出有項目沒帶進
  發布（後面列出那些路徑）、草稿目錄多了東西或檔案與帶出時不同、無法確認，以及刪除沒完成；沒發布而「正在
  發布」的標記收不回來時，說標記留著、清單會顯示結果未知；其他為 `null`（沒發布的原因在 `error`）。
  `retained_path`：沒發布時，要發布的那份內容若已被發布搬進 `versions/` 底下（`current` 沒有指到的那一版），就是
  那一版；還在暫存工作區、而草稿已不持有它時，就是暫存工作區（工作區本身確認不了時不給）；其他為 `null`。沒有
  輪次：發布草稿不是一輪。工作因未預期的錯誤中止時 `draft_publish` 是 `null`。
  工作已完成的清單被裁剪掉、或後端
  重啟過（工作只存在記憶體）則回 404。`llm_log_process` 與側檔那一個是同一個
  token、同一條「沒有 id 就沒有 token」規則，只是**由這個路由在回應當下蓋章**：
  工作表是行程內記憶體，與 id 空間同生共死，所以還答得出來的工作，它的
  `llm_log_id` 必然是本行程鑄的（側檔要存下來是因為**檔案**活得比行程久）。工作
  本身不會跨重啟，但**瀏覽器分頁會**：前端在工作終局後就停止輪詢並一直沿用快取，
  所以那張卡片與它的「查看 AI 日誌」連結可能在重啟後還留在畫面上——token 跟著
  連結走，AI 日誌頁才拒絕得掉。安裝、修訂、跑一次範例與發布草稿共用同一張工作表、同一個輪詢端點，
  回應形狀完全相同（前身是 `GET /api/tools/install/{job_id}`，改名後舊路徑**直接移除**、不留
  相容別名——前後端同一個 wheel 出貨，不會有版本偏斜）。
- `GET /api/tools/{name}/summary` — 該工具的 AI 總結（`summary`／
  `updated_at`／`llm_log_id`／`current_vid`，見下方「安裝後的 AI 總結」）；尚無總結時
  前三個欄位皆為 `null`、`current_vid` 仍存在的 200（不是錯誤），工具不存在（含 `TOOLS_DIR` 未設定）
  才回 404。`current_vid` 是這次實際讀到的版本；請求按名稱定址，所以它可能在
  pointer 變動後讀到另一版，client 必須丟棄與自己那列不符的回應，不能讓 P 的總結
  污染 V 的快取。`llm_log_id` 另外在**後端重啟過**（側檔記的是前一個行程的 id）時
  一律回 `null`，理由見下方同一節。
- `POST /api/tools/{name}/summary/regenerate` — **同步**重新產生總結（不是背景
  工作），body 必須帶 `{expected_vid}`，成功回新的總結與實際版本。工具不存在回
  404；有工具工作正在進行時回 `409 job_busy`——而且它
  **自己也會佔住那個名額**（整趟 LLM 往返期間，install／revise 送出一律 409），
  否則窗內被放行的修訂會發布一個新版本並把 `current` 移過去，這趟已經付過錢的總結
  就落在一個不再生效的版本上，畫面上永遠不會出現；LLM 未
  設定／上游失敗與捕捉、補齊等同步 AI 動作共用同一組錯誤（`503
  llm_not_configured`／`502 llm_upstream_error`），失敗時不會覆蓋既有總結；
  取得名額後若目前版本不是 `expected_vid`，回 `409 version_mismatch`，不花 LLM。
- `POST /api/tools/{name}/revise` — body
  `{feedback?: ≤20000, run_ids?: [id ×≤3], expected_vid, draft_id?, draft_generation?}`，依使用者意見及／或附上的失敗
  執行紀錄送出一次 **AI 修訂**工作（見下方「依意見修訂既有工具」）；意見與紀錄至少要有一樣，
  空白的意見視同沒有。帶 `draft_id`（32 個小寫十六進位字元）與 `draft_generation`（≥1）就是**接續**
  工具的草稿（見下方「沒發布的修訂留成草稿」）：兩者要嘛都有、要嘛都沒有（否則 422）；這時 `feedback`
  是這一輪的說明、`run_ids` 是新附上的紀錄，兩者都可省略，`expected_vid` 是草稿的 `base_vid`。上限 3 是對送出的清單算，其中重複的 id 只附一次，超出 SQLite
  整數範圍的 id 回 422（web-v6 §8）。**附上的紀錄最先檢查並複製**：不存在
  （或已被清除）回 `404 run_not_found`，屬於別的工具回 `422 run_not_for_tool`，兩者都在名額
  與任何 LLM 呼叫之前；複製下來的內容（已遮蔽）隨工作帶走，送出後再清除那幾筆也不影響這次
  修訂。接續時路由另在名額之前讀一次說明檔，以 DB 寬鬆重讀它記著的紀錄（已清除的只留說明檔裡的
  摘要，不拒絕），與新附上的合併：新附的在前、去重、最多 3 筆。202 + `job_id`，
  用上面的 `GET /api/tools/jobs/{job_id}` 輪詢。**先**檢查套件的 `.env`／`config.env`
  政策（web-v6 §5.2）：不過就回 `409 env_policy`。值太短或寫法無法對應時，`message`
  會列出 key 名（永不列值）；檔案讀不到或超過上限時只說明狀況。工作開跑時會再檢查一次。工具不存在回 404；已有工具工作
  在跑回 `409 job_busy`；取得名額後版本不符回 `409 version_mismatch`。接著看草稿：沒帶 `draft_id` 而工具
  有草稿（`draft/` 存在就算，看不到也算）回 `409 draft_pending`，草稿可以接續時 `message` 說請接續或放棄，
  其他狀態說先取回需要的檔案、再放棄（狀態與清單的 `state` 是同一個判定）。帶了 `draft_id` 時依序看第一個
  成立的：看到 `draft/` 不存在，或 `draft_id` 不同（放棄後又建了一份）回 `409 draft_gone`；`draft/` 看不到、
  說明檔讀不出或形狀不對，或檔案目錄不在回 `409 draft_unusable`；`generation` 不同回 `409 draft_changed`；
  正在從它發布、已發布或結果未知回 `409 draft_publishing`；說明檔的 `base_vid` 不是目前版本回
  `409 version_mismatch`（後端自己比，`expected_vid` 相符也一樣）；名額之前讀到的不是同一份說明檔（含讀不到）
  回 `409 draft_changed`。這些都不建立工作。**不宣告** `502`／`503`：修訂是背景工作，LLM 沒
  設定或上游失敗都封在工作狀態裡（失敗的 job + 友善訊息 + AI 日誌連結），與
  安裝送出同一套契約。
- `POST /api/tools/{name}/draft/publish` — body `{draft_id, draft_generation, expected_vid}`，三者都必填，
  `draft_id` 是 32 個小寫十六進位字元、`draft_generation` ≥1（否則 422）：**不經 AI** 把工具的草稿照它現在的
  檔案發布成新版本（見下方「發布草稿（不經 AI）」），給已經自己把草稿改完的操作者；`expected_vid` 是草稿的
  `base_vid`。202 + `job_id`，用 `GET /api/tools/jobs/{job_id}` 輪詢（`kind: "publish_draft"`）。已有工具工作在跑
  回 `409 job_busy`（最先檢查）；取得名額後工具不存在回 404（`TOOLS_DIR` 未設定也是），版本不符回
  `409 version_mismatch`；接著照上面 revise 帶 `draft_id` 時的判定看草稿，回同樣的 `draft_gone`／
  `draft_unusable`／`draft_changed`／`draft_publishing`／`version_mismatch`，只少了最後一項：發布不附紀錄，路由在
  名額之前不讀說明檔，沒有那一份可比。所以只有可以接續的草稿（清單的 `open`）發布得了。這些都不建立工作。
  **不宣告** `502`／`503`：發布不呼叫模型。宣告在 `/jobs/{job_id}` 之前（工具可以叫 `jobs` 或 `draft`）。
- `POST /api/tools/{name}/check` — body `{expected_vid}`，「跑一次範例」（web-v6 §10）：把目前版本的
  `examples.json` 當成一個 job 跑一次；202 + `job_id`，用 `GET /api/tools/jobs/{job_id}` 輪詢
  （`kind: "health_check"`，結果在 `verification`）。占用全域名額，執行範例期間持有共享 tools
  lock，所以刪除與丟棄會被擋（不變量 T）。工具不存在回 404；名額被占回 `409 job_busy`；取得名額
  後版本不符回 `409 version_mismatch`；沒有範例、`examples.json` 無法使用、或工具已停用回
  `409 check_unavailable`，`message` 說明是哪一種——這些都不建立工作。
- `GET /api/tools/{name}/settings` — 工具的套件層設定，唯讀（web-v6 §4、§9）：
  `.env` 的 **key 名**（值永不離開後端）、`config.env` 的每一組（值**逐字包含**某個
  已登記秘密時整個以遮蔽標記回傳，其餘原樣；並標示 `overridden_by_env`／
  `secret_like_name`／`contains_secret`）、兩個檔案
  各自是否讀得到，以及是否為 0.4.0 以前建、遷移來的工具（`built_before_v5`）。
  只要求套件存在、不要求 `current` 可解析——`current` 壞掉的套件正是需要看設定的
  時候。宣告在 `/jobs/{job_id}` 之前（工具可以叫 `jobs`）。
- `GET /api/tools/{name}/runs?limit=1..100`（預設 20）— 這個工具最近的執行紀錄，新的
  在前，**不含內文**（web-v6 §6）：每筆有時間、耗時、執行的 `vid` 與它現在的位置
  `vid_state`（`current`／`older`／`gone`——丟掉的版本紀錄仍在）、呼叫的 `workflow`、
  `outcome`（`ok`／`failed`／`output_overflow`／`timeout`／`start_failed`／`refused`／`error`）、
  `exit_code`、`truncated`、AI 收到的字串開頭 `message`（非 `ok` 才有）、`has_details`，
  以及後端算好的 `fixable`（失敗、而且失敗的就是目前版本；前端不自己判斷）。
  `llm_log_id`／`llm_log_process` 連回造成這次呼叫的 AI 日誌；id 是前一個行程鑄的
  時兩者都回 `null`（紀錄活得比日誌環久，理由同 `summary` 的 `llm_log_id`）。
  另帶 `current_vid`、總筆數 `total`，以及目前版本的驗證狀態 `verification`（web-v6 §9）：
  `state` 是 `no_version`／`examples_invalid`／`no_examples`／`never`／`passed`／`failed`／
  `incomplete`（另帶 `reason`：`budget`／`interrupted`／`examples_changed`），取目前版本最新一次
  檢查的結論（`tool_check` 表，每次跑完範例寫一筆）與 `examples.json` 目前的範例數比對；
  不由執行紀錄推導，所以清除紀錄不會改變它。只要求套件存在（同 `settings`），宣告在 `/jobs/{job_id}` 之前。
  這五條路由都**不宣告** `503 tools_not_configured`：`TOOLS_DIR` 未設定時
  任何名稱都解析不到工具，404 已經是誠實答案。

`GET /api/tools` 的每一列另有 `last_run`：該工具最新一筆執行紀錄的
`{id, outcome, started_at, vid, fixable}`，沒有紀錄時為 `null`；以及 `draft`：工具的草稿，沒有時為
`null`。`draft` 是 `{draft_id, generation, attempts, state, interrupted, base_vid, path, draft_dir, kept,
discard_token, last}`：`state` 依序判定為 `running`（本行程有一輪正在處理它）、`unusable`（說明檔讀不出、形狀不對，
或檔案目錄不在；只能放棄，說明檔讀不出時 `draft_id` 等欄位為 `null`）、`publishing`／`published`／`unknown`（從它
發布的那一次：還在進行、已生效——`current` 的 origin 記著這份草稿——、或無從得知）、`stale`
（`current` 已不是 `base_vid`）、`open`。`path` 是檔案所在（無法使用時是 `draft/` 本身），
`draft_dir` 是整個 `draft/`（放棄刪的就是它，裡面可能不只這裡列出的路徑），`kept` 是接續留下的舊檔案目錄
的路徑（說明檔記著、而且沒有親眼看到不在的那些；說明檔讀不出時是空的）。
`last` 是最後一輪的停止原因、結局、`operator_action`、摘要前 300 字與沒存的檔案數；結局是 `interrupted`
（那一輪標了進行中、卻沒有留下結束紀錄，由下一輪開工時記下）時，結束時間 `finished_at` 是 `null`。文字在讀出時
以當下的已知秘密再遮一次；已知秘密的掃描失敗時整個清單回 500，不回未經檢查的文字。

**Tool runs**（`afterthread/routers/tool_runs.py`，前綴 `/api/tool-runs`）——不在
`/api/tools/` 底下，因為那裡任何字面的第一段都可能是套件名：
- `GET /api/tool-runs/{run_id}` — 一筆執行紀錄**含內文**：`args`（工具從 stdin 收到的
  JSON，保留開頭 8,000 字）、`stderr`（保留**結尾** 16,000 字）、`stdout`（保留開頭
  4,000 字）；成功或被拒的執行三者皆為 `null`。內文寫入前已遮蔽，**讀出時再對當下的
  已知秘密遮一次**；已知秘密的掃描失敗時這條路由回 500，不回未經檢查的文字。套件已
  不存在的紀錄照樣回應（`vid_state: gone`、`fixable: false`）。找不到回 404。
- `DELETE /api/tool-runs/{run_id}` — 刪除一筆；204，找不到回 404。
- `GET /api/tool-runs/stats` — `{total, not_ok, orphans, tools: [{tool_name, total, not_ok}]}`：
  `not_ok` 是成功以外的全部（被拒也算），`orphans` 是看得到套件已不在的紀錄數。算：名稱不
  可能是套件名；`<TOOLS_DIR>/<名稱>` 不存在；那裡是目錄與連結以外的東西（例如一般檔）；那裡
  是連結，而它指向的地方不存在、不是目錄，或連結繞回自己。不算：查詢失敗（例如權限不足、I/O 錯誤）；指向
  目錄的連結（工具清單把它列成無效的一列）。`orphans_only` 的清除用同一個判斷。`TOOLS_DIR`
  未設定、或設定了但目錄不在時一律 0，因為那時沒有任何名稱判斷得出來。宣告在 `/{run_id}`
  之前。
- `POST /api/tool-runs/purge` — body `{tool?, outcome?: all|ok|not_ok, before?, orphans_only?,
  dry_run = true, max_id?}`，回 `{matched, deleted, max_id}`（web-v6 §7）。條件只會縮小範圍；
  `before` 是絕對時間（前端在開啟對話框時算一次；沒有時區的值視為 UTC），預覽與確認因此
  判斷同一批紀錄。不認得的 key 回 422，不會被默默略過——少一個條件等於刪除範圍變大。
  `dry_run` 只計數並回傳符合者的最大 id；`dry_run: false` 必須帶那個 `max_id`（缺了回 422），
  只刪 `id <= max_id` 的——預覽之後才寫進來的紀錄不會被一起刪掉（id 只增不減，
  AUTOINCREMENT）。不會自動刪除任何紀錄。

## 工具（KB 網頁安裝器）

- **磁碟版面與所有權**：

  ```text
  <TOOLS_DIR>/
      .afterthread-tools.lck         # 永久保留的 shared/exclusive lock inode
      <name>/
          .afterthread.meta/
              state.json
              current
              migration-owner.json  # 只存在於一次性 web-v5 遷移產物
              draft/                # 沒發布的修訂（草稿），可缺
                  manifest.json
                  files-<hex>/      # 草稿的檔案（說明檔的 files_dir）；接續留下的舊檔案目錄也是這個名字，記在 kept
              draft.discarded-<hex>/  # 放棄時沒刪乾淨的殘留，sweep 收
          versions/
              <vid>/
                  .afterthread.meta/
                      origin.json
                      summary.json       # 可缺
                  tool.json
                  run.py ...
              <vid>.discarded/           # cleanup 失敗的隱藏重試名
          .env                           # 操作者的秘密
          config.env                     # 操作者的一般設定（web-v6，可缺）
  ```

  套件層只有後端狀態與操作者的 `.env`、`config.env`；工具內容全部在版本層。
  `.env` 的每個值都是遮蔽器的秘密，遮蔽與比對只用至少 6 字元的值；值含換行（CRLF 或單獨的 CR）時，另把它們
  換成 LF 的形式一起比對、遮蔽：後端以文字讀進來的檔案內容與子行程輸出，換行已轉成 LF，只會有這個形式。
  長度下限只看原值，換成 LF 後變短的形式照樣比對。
  收集時不存在（`ENOENT`／`ENOTDIR`）、是連結或特殊檔、超過大小上限的 `.env`
  沒有值，其他讀取錯誤（權限、I/O）則讓已知秘密的清單不完整：需要它的遮蔽與檢查不放行（保存與發布失敗並寫出
  讀不到的 `.env` 與下一步，清單與紀錄的讀取回 `500 known_secrets_incomplete`，工具與 `run_shell` 的輸出不交出），AI 日誌的每段本文改存「無法確認是否含已知秘密，本文未記錄」的標記（不是表示命中秘密的遮蔽標記），JSONL 檔的那一行在寫出時清單不完整，整行的文字欄位都改存這個標記；
  `config.env` 的值不是秘密、不進遮蔽集合、
  沒有長度下限，修訂時整份交給 builder。執行環境的順序是透傳 → `TLS_NO_VERIFY`
  → `config.env` → `.env`，同一個 key 以 `.env` 為準。
  `tool.json` 的 `name` 必須等於套件名並符合
  `^[a-z0-9][a-z0-9_-]{0,63}$`，另含非空 `description`、
  `parameters` JSON Schema 與 `entry` argv。版本 id 符合
  `^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}$`。
- **五個型別是防錯邊界，不是命名慣例**：`PackageLayoutRoot` 表示可能仍在 staging
  或已退休的 package-shaped layout，`PackageRoot` 是其只表示已安裝套件的子型別，
  `VersionRoot` 只表示一個已安裝版本，`BuildRoot` 只表示尚未安裝的 builder
  內容，`Resolution` 則只能是帶有合法 package/version/vid 的 `Resolved`
  或帶原因的 `Unresolved`。餵錯層級通常不會立刻炸掉，而會從錯的位置讀到「缺席」，
  所以用型別讓 package state、version manifest 與 staging 驗證無法混用。
  一次性 migration 需要辨識 shell／`.at-migrated` 的 package-shaped layout，
  因此改用 `resolve_layout_current(PackageLayoutRoot)` 與
  `PackageLayoutResolved`／`PackageLayoutUnresolved`；只有 live installed package
  才能呼叫 `resolve_current(PackageRoot)`，migration 不會替 staging 或 retired
  layout 鑄造 `PackageRoot`。
  `validate_tool_content(BuildRoot, expected_name)` 與
  `scan_installed(PackageRoot)` 共用 `_scan_tool_content` 的 manifest／entry／內容
  規則；前者不碰 `current` 或開關，後者先解析一次 `current`，再從同一個
  `Resolved` 讀 manifest、lineage 與總結。這保證安裝前後的內容規則不會分叉。
- **`current` 是唯一生效指標**：讀取最多 64 bytes，只接受一個可選尾端換行後的
  合法 vid；目標必須是 `versions/<vid>` 的真目錄，且有合法
  `.afterthread.meta/origin.json`。`origin.json` 同時是不可變 provenance 與
  **已提交標記**；缺或壞的版本不可被指向。任何錯誤都得到 `Unresolved`，不猜最新
  版本、不 fallback。寫入使用原子且持久的同目錄發布；discard 另讀到「名稱已換上」
  與「目錄 fsync 已確認」兩個事實，因為前者足以回報 pointer 切換成功，後者才足以
  安全刪除舊目標。
- **啟用開關**：live state 只在 `<name>/.afterthread.meta/state.json`，內容為
  `{"afterthread":"tool-state","enabled":bool}`。檔案缺席或 foreign 都是
  **disabled**，完全不再讀 `tool.json.enabled`；這是 fail-closed 的必要條件，
  否則套件層沒有 manifest 的半發布套件會被舊預設 `true` 自己打開。foreign 檔案
  可讀但沒有 marker，後端不讀、不覆寫、不刪除，列表會帶 notice，PATCH 回 404；
  unreadable／非一般檔／marker 正確但值壞掉則列為 invalid + disabled。一般檔內容
  壞掉可由 API PATCH 原子覆寫修復，非一般檔只能由操作者先移除；單純刪掉檔案只會
  回到 disabled，不會重新開啟。
- **安裝器與提交順序**：每場工作用
  `<TOOLS_DIR>/.staging/<uuid>/{build,shell}`；`finally` 清完整 session root（安裝在組出套件、改名上線
  的那一步還在跑時被取消，就保留：取消不會停下那個 worker，它可能還在寫暫存，也可能在工作結束之後才把工具
  裝上，路徑寫進後端記錄；修訂另依下面「沒發布的修訂留成草稿」的「成果沒有人接住，就不清暫存」保留，發布
  草稿另依「發布草稿（不經 AI）」的暫存規則保留）。
  session 結束時（含逾時與被取消），後端先對這一場的每個 `run_shell` 指令與 `write_file` 寫入發出停止
  訊號，等每個指令都有已停止的證據、每個寫入都有已結束的證據，才剝除、驗證、保存、發布或清理。寫入的
  證據是執行它的 worker 已返回（寫完或失敗都算）；task 已結束、handler 的等待被取消都不算：期限觸發時
  取消的只是等待，worker 會把檔案繼續寫完。停止訊號設下時還沒進入 worker 的寫入，進入後什麼都不寫，不必
  等它。指令的證據只有兩種：
  leader 已回收，而且回收之後觀察到它的 process group 已不存在（回收後立刻看一次，
  之後持續看到回收時限為止；由 `_communicate_bounded` 的拆除在觀察到的當下記下，
  不論它之後正常返回還是擲出例外），或根本沒有啟動子行程（含在 fork 之前就失敗的
  啟動）。啟動在 fork 之後才失敗時，只看 CPython 自己留下的記錄：它已回收子行程
  （exec 失敗的情形），之後 group 也已不存在，才算停止；子行程已 fork、沒被回收，
  就是未確認。SIGKILL 送出成功、worker 返回、例外、時間到都不算指令停止的證據。拿不到
  證據就是無法確認停止：worker 回報指令未確認（例如在拆除完成前被例外打斷）、已開始的
  worker（指令的或寫入的）等了 `TOOL_INSTALL_SHELL_TIMEOUT_SECONDS` 加上回收時限仍沒有
  回報（這一場的指令與寫入共用這一個時限，不是各等一次），或等待被再次取消打斷。
  這時這一場的剝除、發布、清理永久放棄，session root 原地保留，路徑寫進
  後端記錄，之後的工作也不會再碰它；除非 session 本身被取消，工作照樣以失敗結束、
  錯誤訊息附上這個路徑，名額照樣釋放。
  builder 在 `build` 工作，後端先擷取並剝除 builder 寫的根層 `.env`，再剝除
  版本根層的 `.afterthread.meta/`，然後驗證 `BuildRoot`。legacy
  `.ai_meta.json` 與 `.afterthread-state.json` 在版本層是一般工具內容；同名的舊
  package-root 檔案才屬於 legacy 後端命名空間。新安裝在 `shell`
  組出完整 package：版本內容、`origin.json(previous=null)`、初始 enabled state、
  `current`，以及表單秘密注入的 package `.env`；所有內容持久化後，以一次
  `os.rename(shell, <name>)` 上線。durability walker 只 fsync 這棵樹內的真目錄：
  內容中的 directory symlink 會保留但不跟隨、不 fsync 外部目標；任何真實樹內目錄
  若所屬 filesystem 拒絕 directory fsync，durability 仍未成立，整次 build 失敗；列不出內容的目錄、
  或分不出是不是目錄的項目也一樣（底下的檔案沒被 fsync 過）。
  目標只要已存在任何內容就拒絕，不覆蓋。
  完整互動記在 `workflow="tool_install"`。
- **builder 不出貨 `.env`**：非秘密預設值應放在該版程式碼，以
  `os.environ.get(KEY, default)`（或其他語言等價寫法）讀取；切換 `current` 時預設
  自然跟版本一起切。builder 無論在 install 或 revise 寫出根層 `.env`，後端都先
  只擷取 KEY 名、再刪檔；值不進結果。job response 的 `env_keys` 只列名稱，供
  操作者判斷哪些值要自行放進 `<name>/.env`，或哪些非秘密預設應移回程式碼。安裝表單提供的
  `secret_name`／`secret_value` 是另一條後端受控路徑：值注入 `run_shell`、全程
  遮蔽，最後才由後端寫進 package `.env`，從未交給 builder。
- **修訂只新增版本，不再換整包**：`run_revise` 從目前的 `VersionRoot` 複製工具
  內容到 `build`（接續時改從草稿的檔案目錄複製，見下方「接續草稿」）；只排除根層裡保存前會被剝掉的名字
  （`tool_drafts.STRIPPED_ROOT_NAMES`：該版後端自己的 `.afterthread.meta/` 與 `.env`），各含大小寫不同、
  確認是同一個條目的名字，判斷不了是不是同一個條目時也排除（版本裡仍有它）——與接續時不帶入的根目錄條目
  是同一條規則。子目錄裡的同名項目都是工具內容。版本中的 legacy `.ai_meta.json`、FOREIGN
  `.afterthread-state.json`（包含 migration 帶入者）及其巢狀同名檔都是工具內容，
  會進工作區並隨新版本保留。package `.env` 不被複製或改寫，只把解析後的值注入
  `run_shell` 供實測與遮蔽；`config.env` 的值墊在它底下一起注入，文字與 `.env`
  的 key 名則放進修訂提示。開工前（送出時與 job 開跑時各一次）`.env` 的每個值
  必須至少 6 字元、寫法能被遮蔽器逐字對回，`config.env` 必須讀得到；前兩項不過的
  訊息會列出 key 名。builder 寫出的 `.env` 同樣被剝除。通過驗證後，
  `shell` 被組成一個版本目錄並先寫
  `origin.json(previous=<舊 vid>, feedback=<本次意見；接續時是原本的意見加上各輪的說明>)`；版本持久化、rename 到
  `versions/<新 vid>` 後才原子發布 `current`。因此舊版本永久保留、套件 state 與
  `.env` 原地不動，`current` 是修訂唯一 commit point。六位 hex 可能碰撞，建立時
  會避開 `versions/` 下所有以候選 vid 開頭的項目，包括 `.discarded`。
- **沒發布的修訂留成草稿**（`services/tool_drafts.py`）：一輪修訂沒有發布時——builder 自己回
  `ready=false`、session 失敗、試圖改名——工作區存成工具的草稿，在
  `<name>/.afterthread.meta/draft/`；準備發布的那一輪也先存一份（一律複製成新的檔案目錄），發布成功後刪除——只在鎖內確認 `draft/` 裡只有說明檔與那次保存的複本、而且複本的指紋（存好時取的 `tree_digest`，不是工作區）沒變時才刪；被改過、多了東西（例如操作者自己的目錄、接續留下的舊檔案目錄）或讀不完整就整份保留（比對完到改名之間的修改、比對走訪中已讀過又被改的項目不在保證內）——發布失敗時清掉
  「正在發布」的標記、把錯誤記在那一輪。每一次保存都排在「指令或寫入無法確認停止」那個出口之後、
  builder 的 `.env` 與根層 `.afterthread.meta/` 剝除之後；剝除失敗就不存檔案。順序是固定的：
  值不值得存（第一輪相對開工時複製進來的內容沒有變更、模型也沒交回任何筆記，就什麼都不建；接續時工作區與
  帶進來的內容逐檔雜湊相同，就只在說明檔記下這一輪、不換檔案目錄，接著要發布的那次保存除外——記下之前在
  鎖內確認檔案目錄現在的雜湊仍等於開工時的 `copied_out`，與刪除上一份檔案目錄前確認「它現在的雜湊仍等於
  `copied_out`」是同一個比對；不相等或無法確認，例如這一輪進行中被改過、被移走或讀不完整，這一輪就記成
  沒存，檔案目錄不動，暫存保留）、
  大小上限（`TOOL_DRAFT_MAX_FILES`／`TOOL_DRAFT_MAX_BYTES`）、完整的內嵌秘密掃描（以保存開始時取的
  已知秘密掃工作區，決定要複製什麼：檔名含秘密的不複製；內容或連結目標含已知秘密的路徑改複製起點版本
  的那一份——一般檔案，或通過下一項連結判斷的連結；掃描失敗就不存檔案，內容永遠不改寫成遮蔽標記）、
  連結（以連結所在目錄為起點、純字串解析後仍在工作區內才存，存成相對連結；路徑裡有 `..` 緊接在一個
  本身是連結的元件之後也不存，作業系統會先解析那個連結、再從它指向的地方往上走，字串看不到；`..` 前面那個
  元件查不到（`lstat` 是「不存在」或「某一層不是目錄」以外的錯誤）時，無法確認它是不是連結：這是查詢失敗，
  整輪不存檔案（見「看不了的條目不存檔案」）。絕對目標要以工作區自己的路徑
  ——照給定的寫法或解析後的寫法，結尾對齊路徑分隔——開頭才算在工作區內，對不上就記 `link_outside`；
  解析後的寫法要以會傳出錯誤的解析取得，取不到時只比對給定的寫法，對不上同樣是查詢失敗；
  只有這段前綴當成文字剝掉，其餘元件照寫的原樣保留，含 `.`、`..` 與空元件（連結在最上層時開頭的空
  元件會去掉，否則會變成絕對路徑），前面依連結所在目錄的層數補上 `..`，再走同一套逐段判斷）。接著持共享 tools lock，並確認 `<TOOLS_DIR>/<name>` 仍是開工時那個目錄（比對
  `(st_dev, st_ino)`，不是比名稱）：複製（不是搬移、不是 hard link）到 `draft/.incoming-<hex>`、
  fsync、改名成 `files-<hex>`、原子寫入 `manifest.json`。複製時每個項目與每個寫入的位元組都計入同樣
  的上限（換回的起點版本也算，它可能比被換掉的大，也可能在保存途中被手改變大），超過就中止、清掉
  `.incoming-<hex>`，記成大小上限的原因。連結全部放進複本之後、fsync 之前，再以複本為根，對複本裡的
  每個連結重做同一個連結判斷：換回起點版本的地方（一般檔案換成連結、連結換成目標不同的連結），複本
  和工作區不同，而這個判斷只在被判斷的那棵樹上成立。全部判完才刪掉不過的，記成沒存的檔案（換回時
  已記過的路徑不重記）。接著不靠上面的規則，照作業系統解析連結的方式實際走一次每個留下的連結（查詢
  錯誤會傳出來的 `os.path.realpath(..., strict=True)`），直接檢查結果：落在複本內，或確認懸空、而解析失敗的位置在
  複本內，才留；落在複本外，或懸空在複本外（例如第一輪的 `manifest.json` 要等複本存好才寫出來，之後
  就解析得到）記 `link_outside`；無法確認的是查詢失敗。同樣全部判完才刪，存下的連結字串不改。
  之後、fsync 之前，再以同一份已知秘密對整個複本重跑一次內嵌秘密掃描
  （`_remove_secrets`）：內容、檔名或連結目標含秘密的路徑都從複本刪掉並依命中種類記成沒存的檔案
  （`secret_content`／`secret_name`／`secret_link_target`；已記過的路徑不重記），掃描沒能檢查的路徑
  是查詢失敗；這次掃描無法完成就整次不存，清掉 `.incoming-<hex>`。「存下的檔案裡沒有
  保存開始時已知的秘密」由這一次保證，不是工作區那一次：複本不等於工作區——起點版本換回時沒有先檢查，
  指向工作區內的絕對連結改存成相對路徑時，前面補上的 `../` 是新加的文字，和後面接起來可能拼出原字串
  沒有的秘密。
  **看不了的條目不存檔案**：上面各步對條目做出判定而不存的——檔名、內容或連結目標命中已知秘密（換回
  起點版本，或整個不存）、不留的連結、看得到是特殊檔的——記成沒存的檔案，保存照常。因為查詢失敗而沒存的
  ——掃描沒能檢查它（不論掃描給的原因）、連結某一層或它的解析查詢失敗、要換回的起點版本查不到（不是
  「不存在」）——不論在規劃時或在複本重判時，整輪都不存檔案：不提交新的檔案目錄、上一份不動、這次做到
  一半的複本清掉；這一輪記成沒存（第一輪不建草稿），原因（`REASON_UNLOOKED`）列出那些條目，最多 5 個、
  其餘只計數，暫存保留並回報路徑。少了那個條目的複本若照樣提交，上一份仍可能通過刪除前的確認而被刪、暫存
  也跟著清掉，那個條目就一份都不剩。接著要發布的那次保存因此沒存成時，跟它其他沒存成的情況一樣：接續的
  一輪當下不寫說明檔、發布照跑，發布的結果出來後才記這一輪。複製進接續工作區的方向不同：這類條目不帶入、
  記進沒帶入的清單（原因是 `unreadable`，或掃描給的原因），草稿裡仍有它。
  `draft/` 只用不遞迴的 `os.mkdir` 建在既有的
  `.afterthread.meta/` 下，被刪掉的套件不會被重建。讀寫 `draft/` 一律經過套件自己的
  `.afterthread.meta/`：套件目錄與它都必須是真目錄、不是連結；任一個是連結時，這個套件沒有自己的
  草稿（清單不顯示、不擋修訂、放棄回 404），不會沿著連結動到別的工具的草稿。看不到的（`.afterthread.meta/`
  或 `draft/` 的 `lstat` 是「不存在」以外的錯誤）讀取時當作有：清單列出、擋修訂；寫入（含放棄）時不動手：
  保存不寫、發布前後的標記與發布後的刪除都不做，放棄原樣不動並回 `500 draft_not_removed`。第一輪
  保存失敗會收回它建的東西，連空的 `draft/` 一起。說明檔的文字先遮蔽、再截斷、再處理孤立的
  surrogate，最後用同一個比對器把文字本身與它寫進檔案的樣子（JSON 字串字面）各再檢查一次（`tools._redacted`）：
  替代字元或跳脫字元（`\n`、`\"`、`\\`、`\uXXXX`）在任一種樣子裡拼出已知秘密的欄位，整欄寫成遮蔽標記；識別碼、
  時間、數字與詞彙值改為檢查形狀。寫入前再以寫出的整份文字做一次底線（`tools._encode_checked`）：
  已知秘密的每一處出現都看，重疊的也看；整處落在固定內容（鍵、JSON 標點與空白、形狀固定的值、退化時
  放進去的遮蔽標記）裡的照寫，碰到文字欄位的——包括橫跨鍵名、引號或兩個欄位的——就把所有文字欄位都
  寫成遮蔽標記再查一次，仍碰到就不寫，保存以「說明檔寫下去仍會含有已知的秘密值」失敗。精確比對看不到表示法的差異：以真換行登記、
  文字卻寫成 `\n` 兩個字元的秘密，base64 等其他編碼，`.env` 讀入時壞位元組已被替換的值，都不在這個
  保證內。一個工具一份草稿：`draft/` 存在時，沒指名草稿的修訂一律拒絕（`draft_pending`），要嘛接續、
  要嘛照現在的檔案發布（見「發布草稿（不經 AI）」）、要嘛放棄。新版本 `origin.feedback` 的文字遮蔽不了
  （已知秘密讀不到）時算發布失敗，什麼都不發布。
  **成果沒有人接住，就不清暫存**：指令與寫入確認停止之後，這一輪的暫存目錄只在下列情況清除——成果已存進草稿、
  而且說明檔確認落盤（發布的 worker 可能還在寫暫存目錄時除外）；已經發布；沒有值得存的東西。其他出口一律
  保留成果所在之處，包括保存沒存成、說明檔寫了卻無法確認落盤、發布失敗而發布前的保存沒確認落盤、遮蔽模型
  筆記時已知秘密讀不完整（這時說明檔什麼都不寫）、builder 的 `.env` 剝除失敗（不存檔案）：成果在暫存目錄
  就原地不動（sweep 照常執行，它不看 `.staging`），後端記錄寫出它的路徑；發布失敗時成果已搬進
  `versions/<vid>`（`current` 沒指到的那一版），就留著那一版，暫存目錄照常清掉。工作的
  `draft.retained_path` 寫出成果當下所在的路徑（工作區本身確認不了時不給）。開始對話之前，工作在複製進
  暫存工作區（第一輪從版本、接續從草稿）的途中被取消時，暫存目錄也不清：取消不會停下複製的 worker，它可能
  還在寫；暫存原地保留，後端記錄寫出路徑。
  保留之前一律先剝除 builder 的 `.env`：指令與寫入確認停止後先剝除，再遮蔽筆記、再保存；之後若因例外或取消
  （例如後端正常關閉）中止，保留之前補剝。剝除或補剝失敗時照樣保留，不清掉：讓剝除的 `unlink` 失敗的原因
  （例如 builder 把工作區改成唯讀）通常也會讓清除對同一個檔案失敗，清掉只會留下那個 `.env`、刪掉其他還刪
  得掉的成果。保留之前最後一次剝除仍失敗時，後端記錄另外說明那裡可能還有 builder 寫的 `.env`。例外或
  取消時路徑只寫在後端記錄（例外時工作由未預期錯誤的兜底收尾，不帶 `draft`）。安裝、
  修訂與發布草稿工作收尾時的 sweep（排他鎖下；指令或寫入無法確認停止的那一場不跑）也收草稿的殘留：放棄或發布後刪除時
  改名留下的 `draft.discarded-<hex>`，以及說明檔讀得出、形狀正確、它指的檔案目錄在、而且說明檔與 `draft/`
  都先 fsync 成功時的 `.incoming-<hex>`（保存中途當機留下的半成品）；任何一項不成立，整個 `draft/` 都不碰。
  說明檔沒有點名、也不在 `kept` 的 `files-<hex>` 不收：沒被點名不是可以刪的證據——說明檔寫完、還沒確認
  落盤就當機，舊說明檔回來，新的檔案目錄就沒被點名，而操作者可能已經照畫面給的路徑改過它。這種目錄留到
  放棄為止。
- **接續草稿**（`tool_drafts.begin_round`／`copy_into_workspace`／`record_round`）：送出時（取得名額之後）判定
  可以接續、轉成工作之後，接續的那一輪先在共享 tools lock 內、確認套件仍是開工時那一個、說明檔仍是判定時那一份而且沒在
  發布，寫入 `running`（job id 與開始時間，`generation` 加 1）；寫不進去就不開始、不呼叫 AI，工作失敗並
  說明。說明檔留著上一個 `running`（那一輪沒有留下結束紀錄）時，先把它記成一輪，結局 `interrupted`、
  `finished_at` 留空；`kept` 先拿掉看到不在的名字，仍達上限（100 個）就不開始。接著同樣持鎖，先取草稿檔案目錄的
  雜湊（`copied_out`），量一次大小，再用保存的同一條管線複製到新的暫存工作區：連結改寫成相對連結、邊複製
  邊計數、在複本上再判一次連結並解析、整棵重掃已知秘密，命中的不帶入（也不換成更早的版本）；檔案保住
  權限位元（至少擁有者可讀寫）。保存前會被剝掉的根目錄名字——`.env` 與 `.afterthread.meta/`，各含大小寫
  不同、其實是同一個條目的名字，判斷不了時也算——不帶入：帶進來的話，保存前的剝除會把它刪掉（在不分大小寫
  的檔案系統上，剝除用的路徑也碰得到別名），新的一份就沒有它，上一份又因為整份都帶進來、沒被改過而被刪；
  草稿裡的 `.env` 的值也不是已知秘密，`read_file` 讀進對話就沒有東西遮得掉它。這份名單、兩個剝除
  （`_strip_builder_env`、`_strip_builder_sidecars`）與第一輪從版本複製時排除的名字，取自同一個定義
  `tool_drafts.STRIPPED_ROOT_NAMES`。
  沒帶入的路徑記在那一輪，也告訴模型。複製、掃描或大小上限不過時，清掉自己的 `running`，不算
  一輪。之後每個正常出口（對話結束、指令與寫入確認停止）都把這一輪記進說明檔一次並清掉 `running`：保存已經記下
  的不再記；工作區確認不了、剝除失敗、保存沒寫成時，另外寫一次只記這一輪。接著要發布的那次保存沒存成時，
  不論在哪一步失敗，當下都不寫說明檔、`running` 留著，等發布的結果出來才寫一次只記這一輪：發布失敗記成
  `failed`、錯誤是發布的錯誤，發布成功記成 `published`，兩者都是沒存、原因是保存失敗的原因；發布途中丟出
  未預期的例外則不碰說明檔，留給下一輪記成 `interrupted`。這一輪新附上的紀錄在這次寫入時
  併進 `runs`（新的在前、去重、最多 3 筆）。這些寫入都在鎖內確認套件、`draft_id`、開工時寫下的
  `generation` 與 `running.job_id`；任何一項不符或寫不進去，`running` 就留著。指令或寫入無法確認停止，或未預期的
  例外，都不碰說明檔，留給下一輪記成 `interrupted`。保存換成了不同的新檔案目錄時，上一份在同一次說明檔寫入
  記進 `kept`，下面三件事都確認了才當場刪：說明檔確認落盤；`copied_out` 與工作區開始時的雜湊相同（帶進
  工作區的正是開工時的內容，沒有漏掉、改寫或複製期間被換掉又換回來的項目）；它現在的雜湊仍等於
  `copied_out`（這一輪期間沒人改它）。其餘留在 `kept`，工作的 `draft.reason` 說它在哪、為什麼留；名字
  要等之後改寫說明檔時，親眼看到它不在（`lstat` 回不存在）才拿掉。只記一輪、保存失敗時都不動上一份。
  模型看到的：系統提示說這是第幾次嘗試、工作區是上一輪留下的成果；使用者提示依序是原本的意見、這一輪的
  說明、上一輪怎麼結束與它的筆記、更早的每輪一行、沒帶入的路徑、相對於基底版本的變更清單（列不出時照實
  說）、工作區的 `tool.json`、套件層設定與附上的紀錄。每段有上限，整段不超過 `LLM_PROMPT_BUDGET_TOKENS`
  換算的字元數，與系統提示合計不超過 `LLM_TOOL_CONVERSATION_BUDGET_TOKENS` 換算的一半；超過時依序縮減，
  這一輪的說明與固定指示最後才縮。第一輪的提示用同一個函式，也受這個總上限。
- **發布草稿（不經 AI）**（`tool_builder.run_publish_draft`；`tool_drafts.copy_for_publish`／`mark_publishing`／
  `unmark_publishing`／`holds_copy`／`remove_after_publish`）：把可以接續的草稿照它現在的檔案發布成新版本，給已經
  自己把草稿改完的操作者。不呼叫任何模型：沒有 builder session，也不重產總結——新版本一開始沒有總結，要用
  「重新產生總結」才有；不寫 `running`、不開一輪、不改 `attempts`。依序：
  1. 工作開始時再解析一次 `current`：已不是草稿的基底就不發布（草稿已過期）。
  2. 帶入：持共享 tools lock、確認套件仍是開工時那一個、說明檔的 `draft_id` 與 `generation` 仍是判定時那一份而且
     沒在發布（不要求 `running`），用接續的同一條管線（`_copy_draft_files`）把草稿的檔案目錄複製進新的暫存工作區：
     先取 `copied_out`，量大小，掃描，根目錄的保留名字不帶入，連結改存成相對的、在複本上再判一次並實際解析，整棵
     重掃已知秘密；複製完在同一把鎖內取工作區的 `copied_in`。草稿只讀不寫。帶入交回全部的事實：沒帶入的每個
     路徑與原因、其中哪些是查詢失敗，以及秘密命中——包括不帶入的根目錄條目裡的（規劃略過它們，命中要另外從
     掃描結果分出來）。
  3. 能不能發布：帶入本身停下的——整棵掃描無法完成、已知秘密讀不完整、超過大小上限、草稿的檔案目錄讀不完或
     複製失敗、說明檔已不是判定時那一份或已在發布、檔案目錄不在、套件已換掉或取不到鎖——不發布；帶入完成後，有不在豁免內的秘密命中（檔名、內容、連結目標；掃描草稿與重掃
     複本的都算）就不發布，否則有查詢失敗就不發布。查詢失敗是因為查不到而沒帶入的條目（規劃時或複本重判時：
     掃描讀不到、連結某一層或它的解析查不到），以及判斷不了是不是保留名字別名的條目。豁免只有帶不進版本的根目錄
     條目：`STRIPPED_ROOT_NAMES` 列的名字（`.env`、`.afterthread.meta/`），以及**確認**與它們是同一個條目的大小寫
     別名（`(st_dev, st_ino)` 相同）。掃描正常交回之後，它們裡面的秘密命中與掃描記下讀不到的檔案（例如打不開的
     根目錄 `.env`）都不擋發布；但帶入前的大小量測（`_over_limits`）與整棵秘密掃描仍會走進它們，這兩步失敗（例如
     根目錄 `.afterthread.meta/` 列不出內容）時帶入停下、不發布，草稿不動。大小寫不同而
     獨立存在的條目、巢狀的 `sub/.env`、一般檔都不豁免。不發布時什麼都不寫進草稿，工作的錯誤說明原因並列出
     路徑（遮蔽、最多 5 個，其餘只計數）。因為它是什麼而不帶入的（指到外面的連結、`..` 跟在連結之後的連結、
     特殊檔）照常發布，草稿留著（見 5）。
  4. 標記：先做出版本的 `origin.feedback`——草稿的意見、草稿還記著的各輪說明，最後一行完整保留「操作者發布的草稿
     <draft_id>，未經 AI」（`tool_drafts.operator_publish_line`；清單靠 origin 裡的 `draft_id` 對帳，與修訂的發布同一個
     方式）——遮蔽不了就不發布。接著在鎖內比對套件、`draft_id`、`generation`，寫入 `publishing`（job id 與時間，
     `generation` 加 1），之後一律用這次寫入交回的 `generation`。寫入要確認落盤（`mark_publishing` 的
     `durable_out`）：沒寫成就不發布；寫了卻沒確認落盤也不發布，並收回自己的標記。
  5. 發布：`_publish_revised_version`，與修訂相同（剝除根目錄的後端中繼資料、驗證、提交版本、原子移動
     `current`）。成功時，三項都確認了才刪草稿：`copied_out` 等於 `copied_in`——工作在呼叫路徑上自己比：帶入的
     正是帶出的全部，沒有漏帶、改寫成相對的連結、權限位元不同，或複製期間被換掉又換回；在鎖內比對套件、
     `draft_id` 與標記寫入的 `generation` 之後，`draft/` **實際的**成員只有說明檔與帶出的那個檔案目錄（不看
     `kept`）；那個目錄現在的雜湊仍等於 `copied_out`（`remove_after_publish`）。只有之後親眼看到 `draft/` 不在了，
     才算刪掉。否則整份草稿連同 `publishing` 留著（清單對帳成「已發布」），工作的 `draft_publish.reason` 說原因。
     接著照修訂跑 `_verify_published`；範例失敗不撤銷發布。
  6. 發布器正常回失敗、`current` 沒動：收回自己的標記（`unmark_publishing`：只收同一份草稿、標記寫入的
     `generation`、而且是這個工作寫的標記；`generation` 加 1、`publishing` 清掉，其餘一字不動——`attempts` 尤其
     不動：這次發布不是哪一輪的，不像修訂的發布失敗時 `clear_publishing` 把錯誤記在最後一輪）。基底在發布期間
     換了，清單就看到已過期。收不回時標記留著，清單顯示結果未知，工作說明。例外與取消不收標記：那時不知道
     發布有沒有成功。
  暫存的規則：帶入或發布的 worker 可能還在寫時一律不清——取消不會停下 worker，coroutine 結束不是停止的證據
  （`copy_running`、`publish_running`）。發布開始前 `holding` 升起，之後只在三種情況放下：已經發布；發布器回報的
  內容所在（`content`）已不在這場暫存內，也就是已搬進 `versions/<vid>`——那一版留著、路徑回報、暫存清掉；或
  草稿被看到仍持有暫存裡的內容（`holds_copy`：`copied_out` 等於 `copied_in`，而且草稿檔案目錄現在的雜湊等於
  `copied_out`）。持有不成立時暫存保留，`draft_publish.retained_path` 寫出內容所在（工作區本身確認不了時不給）。
  發布開始之前的拒絕與失敗，
  暫存只是剛帶出的複本、草稿沒動、worker 已返回，清掉。保留時後端記錄寫出路徑，sweep 照常執行。這個釋放條件與
  每個參與的賦值都登記在 `tests/test_staging_release_guard.py`。
- **執行綁定廣告時的版本**：registry 廣告工具時把 `PackageRoot`、
  `VersionRoot`、entry 與 manifest identity 一起封進 handler；模型稍後真的呼叫時
  直接以那個 `VersionRoot` 當 cwd。package `.env` 與開關則在呼叫當下讀。
  **`current` 在呼叫當下仍會解析一次，但只用來確認「這個套件還有可用的版本」，
  不用來決定跑哪一版**——否則廣告綁定就沒有意義了。少了這道確認，列表會把
  `current` 壞掉的套件判成 invalid＋停用，而已廣告的 handler 照樣跑得起來，
  同一個問題兩條路徑兩個答案（overall review r2-4）。這避免 pointer 改變後用舊
  schema 執行新程式。**「廣告與呼叫之間被 discard」已經不可能發生**：請求在送出 tools
  array 之前就取得 shared lock 並持有到結束，而 discard 需要 exclusive lock，所以那段期間
  按下 discard 會直接得到 `409 ai_job_in_progress`，`current` 與該版都不會被動到。（先前
  這裡寫的是「discard 會成功、呼叫再得到拒絕」——那是互斥鎖出現以前的行為，已作廢。）
- **執行紀錄是觀察者（web-v6 §6）**：經由已廣告 handler 的每一次呼叫（`_make_handler`
  這條路；builder 的 meta-tool 不算）都寫一筆 `tool_run`。`_run_tool_subprocess` 回傳
  `ToolRunResult`：給模型的字串 `text` **與加入紀錄之前逐字相同**（不變量 F，六種 outcome
  都對舊程式比對過），其餘是紀錄要的 outcome、exit code 與已遮蔽的內文。handler 把字串交回、
  把其餘交給注入的紀錄器（`tools.set_run_recorder`，由 `main` 的 lifespan 在 `init_db` 之後
  接上 `tool_runs.record`；測試的 client 不進 lifespan，所以不寫紀錄）。紀錄器有自己的
  engine 與 1.5 秒的 busy timeout，在自己的短 transaction 裡寫；任何失敗只留一行只含例外
  **型別**的 WARNING（資料庫錯誤的文字會帶 SQL 參數，也就是內文），工具結果照常回給模型。
  寫入在共享鎖釋放之前完成，所以刪除工具（需要 exclusive）不會插在一次執行與它的紀錄之間
  （兩種在取得鎖之前就拒絕的呼叫沒有鎖可持有，但它們什麼也沒執行）。呼叫在後端這一側拋出例外
  （例如 tools lock 無法使用）也會記一筆 `error`，內容只有模型收到的
  `tool execution failed: <型別>`，例外照樣往上拋。工具已經執行、只是輸出遮蔽不了（例如已知秘密的清單不完整）時
  不拋例外：模型收到 `tool ran (exit N), but its output is withheld: ...`，照實說它執行過，紀錄同樣是 `error`、
  沒有內文；builder 的 `run_shell` 同理（`command ran (exit N), ...`）。
  內文只在**啟動過而且失敗**的執行才有：先在整段文字上遮蔽（非零 exit 時與給模型的那份共用
  同一刻的已知秘密、同樣 fail-closed），再 `_utf8_safe`、再截斷；逾時與無法啟動兩條路徑的遮蔽
  只為紀錄而做，掃描失敗就不存內文，不影響呼叫。`workflow` 與 AI 日誌連結來自
  `llm.generate_structured` 在互動期間設定的 ContextVar。
- **request／子行程與破壞操作互斥**：capture、enrich、assist-update 在掃描並把
  tools array 交給模型**之前**取得 `.afterthread-tools.lck` 的 shared `flock`，在整個
  請求（所有模型 round、工具呼叫與錯誤清理）結束後才關閉 descriptor；shared lock
  彼此相容，所以兩場一般對話不會被序列化。工具使用
  `Popen(pass_fds=(fd,), start_new_session=True)` 繼承同一個 open-file reference：
  backend 即使被 `kill -9`，只要 child 還活著，exclusive 仍取不到。整包 delete、
  discard 與 `.stale-`／`.discarded` 背景清掃都只嘗試非阻塞 exclusive lock；前兩者
  真正的 contention 回 `409 ai_job_in_progress`，清掃則略過、等下次工作再試。若 lock
  path 無法開啟或鎖定，delete／discard 改回非 retryable 的
  `500 tools_lock_unavailable`，訊息會列出完整路徑與原地修復 ownership／owner
  read-write 權限的方式，不會假稱有 AI job。後端擁有的既存 `0400`／`0000` inode
  會先在原地補回 owner `rw` 再開啟；不同 owner、非 regular file 或修復失敗則明確
  中止，migration 也會收到同一個具名錯誤。安裝／修訂結尾的 best-effort 清掃若遇到
  此錯誤，不會把已成功發佈的工具回報成失敗；完整路徑與同一套原地修復方式會寫入
  backend 的 `afterthread.tool_builder` WARNING console log，之後的工具工作仍會重試清掃。
  lock 檔**不得手動刪除或重建**：新 inode 不會和舊 holder 衝突，保護會無聲失效。
  `flock` 只協調有檢查它的程式碼，這裡的 checker 都由 afterthread 控制，並不是防止
  操作者或工具直接改檔的 sandbox。最後，這個 orphan 保護唯一依賴的工具行為是 child
  不自行關閉繼承的 descriptor。
- **版本保留、lineage 與 discard**：`origin.json.previous` 是前一版的唯一來源，
  不是目錄排序；沒有自動回收。`lineage=sole` 表示 previous 為 null，
  `usable` 表示 previous 是另一個合法已提交版本，`broken` 表示 pointer 壞掉或
  指回自己。discard 的順序固定為：確認 expected vid → sole 則整包刪除 → 驗前一版
  → 發布 `current=P` → 只有 pointer 的目錄 fsync 已確認才盡力移除 V。V 還在執行
  時 exclusive lock 根本不會取得，discard 回 409 而不動任何檔案；無法確認持久化時
  保留 V 但仍回成功，因為多一個未指向版本比「斷電後 current 指到已刪版本」安全。
  掃描捕捉一個可解析版本時，就在
  process-local registry 取得或建立該 `VersionRoot` 的 advertisement generation；
  同一輪稍後建立的每個 handler 都持有這個物件。discard 成功發布 `current=P` 時，
  會在與掃描捕捉共用的 lock 內把 V 當下的 generation 標成 retired 並移出
  registry；因此即使持久化未確認或停放 rename 失敗、使 V 留在原廣告路徑，舊
  handler 仍明確拒絕。retired generation 由既有 handler 持有到該對話／handler
  釋放為止；之後從備份恢復並重新掃描同一 vid 會取得新 generation。退役完全不讀
  目錄的 device、inode 或其他可由 rename／刪除／重建影響的磁碟屬性，避免磁碟
  身分重用或搬回原位使舊廣告復活。這個 generation 只回答「舊 handler 是否已被
  明確 discard」，不是執行中目錄的 mapping；後者的 package-inode registry、
  `running/unknown/locally-proven-idle` 判斷、本地建立 provenance 與
  park-because-running cleanup 資格都已刪除，由跨 restart 的 flock 排他性取代。
  `.stale-`／`.discarded` 只代表清理失敗，後續 sweep 也必須先取得 exclusive lock。
- **AI 總結拆成兩份 sidecar**：每版不可變的
  `.afterthread.meta/origin.json` 保存來源、安裝指示、修訂意見與 previous；
  可重新產生的 `.afterthread.meta/summary.json` 保存 summary、updated time 與
  AI log identity。兩者都經 typed／bounded／secret-redacted／fail-closed／atomic
  publisher：文字欄位先遮蔽，再以文字本身與寫進檔案的樣子各檢查一次（任一種拼出已知秘密就整欄寫成遮蔽標記，與草稿說明檔
  同一套），寫出的整份文字再做一次同樣的底線：鍵、標點與形狀固定的欄位（`previous`、`updated_at`、
  `llm_log_id`）是固定內容，`llm_log_process` 照呼叫者給的寫、算文字但不換成標記，所以碰到它的出現
  會讓整份不寫；`updated_at` 必須是 `datetime.now(UTC).isoformat()` 的形狀，否則拒寫。
  `origin.json` 寫不出來就是發布失敗、成果保留。來源 URL 在捕捉時先收斂成不帶 userinfo/path/query/fragment 的
  provenance。summary 缺席或損壞是「尚無可用總結」，不會使版本失效；安裝／修訂
  上線後的自動產生是 best-effort，失敗不能翻轉已完成的提交。
- **全域 single-flight**：安裝、修訂、跑一次範例與發布草稿共用背景 job 表；同步 regenerate 與 discard
  也透過同一個 admission lock 取得名額。它們的 API 形狀不同，但比對
  `expected_vid` 都排在取得名額之後、實際寫入／花 LLM 之前，避免另開一條
  check-then-write race。背景 job 是 process-local，重啟後舊 id 404。
- **安全立場（v1）**：builder 的 `run_shell` 是服務權限下的真實 bash，不是沙箱；
  只有 `write_file`／`read_file`／`list_dir` 被限制在 build root。信任邊界仍是
  「只安裝可信的 OpenAPI 文件與指示」。工具子行程的環境從零建立，只透傳
  `PATH`／`HOME`／`LANG`／`LC_ALL`／`TMPDIR`，再加正規化的 TLS 設定、package
  `config.env` 與 `.env`，避免不小心把父行程 `OPENAI_API_KEY` 一起交出去；同 UID 行程仍可能
  讀 `/proc`，所以這不是對抗惡意程式的隔離。
- **一次性 web-v5 遷移**：先停掉 afterthread；migration 會先以非阻塞方式取得同一個
  exclusive tools lock 並持有到整體完成，取不到即拒絕開始。取得 lock **只證明當下
  沒有 cooperating request 或 inherited tool child 持有它**；idle backend 與已停止
  backend 無法區分，舊版 backend 也根本不知道這個 lock，因此不能用成功取得 lock
  取代「先停服務」的操作前提。為使 migrated version 精確反映開始時的內容，仍建議
  關閉編輯器並暫停手動／同步寫入。即使遷移期間發生 autosave，舊套件
  現在也不會被刪除：那次編輯會留在 retained quarantine，**不保證進入 migrated
  version**，操作者可事後比對與取回。確認 `TOOLS_DIR` 指向舊扁平套件，再於
  `backend/` 執行：

  ```bash
  uv run python -m afterthread.migrate_tools_v5 --dry-run
  uv run python -m afterthread.migrate_tools_v5
  # 已人工審過同一份計畫的非互動執行：
  uv run python -m afterthread.migrate_tools_v5 --yes
  ```

  `--dry-run` 除了建立（若尚不存在）並鎖住永久的 `.afterthread-tools.lck` inode，
  只做唯讀預檢並列出整體計畫；一般執行會逐套件報告 legacy state／
  `.ai_meta.json` 分類、enabled 與 `.env` **key 名**（永不列值），然後在第一次
  寫入前最後一次詢問。只有明確 `y`／`yes` 才繼續；拒絕、關閉或無法詢問的 stdin
  都視為 no，tree 保持 byte-identical。`--yes` 只適合計畫已審過的自動化執行。
  確認後先在 `TOOLS_DIR` 的兄弟位置做完整備份，再建立
  `.afterthread-migration.json` write-ahead journal。預檢一次收集所有問題；任何
  legacy 套件不可遷移就完全不開始。程式會在寫入新 package `state.json` 的最後一刻
  重讀 legacy enabled；若值與預檢不同，代表停在確認畫面時來源仍被修改，整趟會清楚
  拒絕並 rollback，而不採用未審閱的新值或覆蓋它。每包把工具內容搬進單一初始版本、移除
  manifest legacy `enabled`、把 owned state 搬成 package `state.json`、FOREIGN
  legacy state 原封不動當工具內容、把 `.ai_meta.json` 拆成 origin/summary，並將
  `.env` 位元與 mode 保留在 package 層。所有套件啟用後才持久寫下
  `committed`；commit 前錯誤回復所有名稱，commit 後只重試清理、不再 rollback。
  為縮短第一次複製後仍可能收到編輯器 autosave 的窗口，程式會先把舊套件停放到
  `.at-premigrate`，再從該停放來源重抄一次 `.env`，成功才啟用新套件；重抄失敗會
  走 commit 前 rollback。其他內容只在組 shell 時複製一次；之後對 `run.py`、
  `tool.json` 或任何工具內容的編輯可能只出現在舊套件。commit 後程式會把整棵舊套件
  從 `.at-premigrate` 改名成隨機、隱藏的 quarantine 並永久保留，因此這些 late edits
  可復原，但不會被偷偷合併到 migrated version。
  journal 另持久記錄 shell／舊套件目錄 identity，並在目錄內寫入綁定 package +
  vid + role 的 marker。對 migration 自己建立且**從未完成 activation** 的 partial
  新 shell，identity + marker（完整 target-layout 亦可用精確 VID）仍是清理證明；
  新 shell 在空目錄時先記
  identity，再以目前 journal 的 atomic hard link 暫時充當 bootstrap marker，正式
  JSON marker 完整落盤後才移除 bootstrap。一個 shell 只要曾完成 activation（或
  journal 停在 activation 可能已落地的 pending 狀態），就已可能以 live package
  身分接受操作者寫入；rollback 不再 `rmtree`，而會改名成 role=`shell` 的隨機 hidden
  quarantine 並列入報告。這套 destructive ownership proof 不再用於已 activation 的
  新 shell 或 committed 舊套件：舊套件只產生 256-bit 隨機 quarantine 名稱、rename
  過去並把
  retained 階段持久寫進 tree 外的 journal，之後**沒有 `rmtree`**。舊套件 marker
  留在 quarantine 內作為 fresh preflight 的持久分類標記；沒有 marker 的同形 sibling
  仍視為 operator-owned 並拒絕。隨機 hidden quarantine 不會被 package scanner、
  `.env` redaction scan 或 by-name API 當成 phantom package，且 `.at-premigrate`
  不會成為 terminal 名稱。
  成功報告會逐一列出 retained quarantine 的完整路徑，提醒其中可能有遷移期間才寫入
  的內容，並明說檢查後是否移除由操作者決定。第二次 fresh run 也會辨識、列出並忽略
  這些 quarantine，成為乾淨 no-op。舊的「quarantine 已刪但 journal 尚未記錄」空窗
  已不存在，所以 restore 出來的 `.at-premigrate` 不可能再被該空窗藏在成功結果後面。
  中斷後以同一指令重跑，journal 會按其狀態續做／回復／清理；不要手動猜測或刪除
  `.at-*` 兄弟目錄。既有 journal 已代表先前確認過的 migration authority，所以
  真實重跑會直接對帳，不再詢問；此時 `--dry-run` 只報 journal status，不做對帳。
  唯一需要 operator 決定的是 v1 journal 搭配非空、markerless 且未完成的 shell：
  v1 沒留下可驗 ownership，程式會保持所有內容不變，列出確切路徑，並要求先檢查後
  將可丟棄的 v1 partial shell 刪除，或把要保留的資料移出 reserved 名稱，再重跑。
- **安全立場（v1）**：這是單人本機工具，shell 能力是明確需求（比照 Claude
  Code 建 skill 的能力/風險模型）——`run_shell` 是以**本服務自身權限**執行的
  真實 bash，只是預設從暫存目錄開始（工作慣例，不是圍籬），v1 刻意不做容器
  隔離。唯一被強制圍住的邊界是 `write_file`／`read_file`／`list_dir` 三個
  meta-tool：路徑一定會被限制在暫存目錄之內（絕對路徑、`..` traversal、
  symlink 逃逸都會被擋下）。信任邊界因此是「只安裝你信任的 OpenAPI 文件與
  指示」，不是程式碼在幫你圍出一個對抗式安全沙箱。安裝指示文字（作為提示）與
  工具呼叫參數的摘要／預覽（約前 200 字元）都會進 AI 日誌（見上方
  `LLM_LOG_FILE`，兩者同樣受 `LLM_LOG_BODY_MAX_CHARS` 逐則截斷），而指示文字
  還會被存進該版本自己的 `versions/<vid>/.afterthread.meta/origin.json` 供之後的
  修訂 session 使用——**打進 `instructions`
  的東西一律當成「模型看得到、日誌留得住」處理**。憑證因此走
  `secret_name`／`secret_value` 這對欄位（下一條），不是寫進指示：貼在指示裡的
  值在它還不是「已知秘密」的那一刻就落地了，遮蔽器事後補登記救不回既有紀錄。
- **安裝表單的秘密欄位**（`secret_name`／`secret_value`，D36；兩者要嘛都給、
  要嘛都不給，名稱須為合法環境變數名，值至少 6 字元且不含換行，否則 422）：值
  以該名字注入 `run_shell` 的環境（AI 因此能實測真的 API）、整個建置期間登記成
  已知秘密（AI 日誌落地前就遮蔽；工作正常結束或失敗時解除登記，後端關閉時被中止的工作則留到行程結束：
  正在組出套件的那一步不會因此停下，值寫進已安裝套件的 `.env` 之前，它還要拿已知秘密掃描工具包、寫
  `origin.json`）、驗證通過後由後端寫進該工具自己的 `.env`
  （`_inject_secret_into_env`，在 `validate_tool_content` 之後、搬移之前）。值不進任何
  提示，模型只拿得到**名字**。
- **`.staging` 殘留**：清理是 best-effort（所有清理例外都被抑制），正常結束
  時通常會清掉暫存目錄；但後端在建置途中被中斷（當掉、被砍、主機重開機），
  **或清理本身失敗**（例如 `run_shell` 改了目錄權限），**或無法確認暫存工作區仍是
  `.staging` 裡原本那個真目錄**（它或上層的 `.staging` 被換成連結，或查看它們的
  查詢失敗；剝除、保存草稿與發布之前也各查一次，查不過就不做），**或有 `run_shell` 指令或
  `write_file` 寫入無法確認停止**（見「安裝器與提交順序」的例外，路徑寫在後端記錄裡）時，會留下
  `<TOOLS_DIR>/.staging/<uuid>`。這個目錄名稱以 `.` 開頭，registry 掃描
  （`tools._scan_all`）會直接跳過隱藏目錄，不會被列成無效工具。刪除前要做的確認見專案 README 的
  〈`.staging` 殘留清理〉。指令或寫入無法確認停止而留下的，可能還有後端行程裡的寫入執行緒，或 `run_shell`
  留下的程序在寫它。這種保留下來的工作區沒有剝除過，裡面可能有
  builder 寫的 `.env`，安裝時可能就是你在表單填的秘密，而工作結束後（後端關閉時被中止的工作，是行程結束後）
  它已不在遮蔽名單裡；請盡快刪除。安裝工作在組出套件、改名上線的那一步還在跑時停下（例如被取消），也會留下暫存目錄，路徑寫在
  後端記錄裡：那個 worker 可能還在寫它，也可能在工作結束之後才把工具裝上。修訂工作
  還會刻意留下暫存目錄，路徑寫在後端記錄裡：一是這一輪的成果沒有人接住
  （見「沒發布的修訂留成草稿」的「成果沒有人接住，就不清暫存」），它可能是那一輪成果唯一的一份，先取回需要
  的檔案；其中 builder 的 `.env` 到最後都剝除不掉的，後端記錄會說明裡面可能還有那個 `.env`，取回
  之後請刪除整個目錄。二是工作停下時它的發布還在跑，那個發布可能還在寫它。發布草稿的工作也會留下：
  取消時，帶入或發布的 worker 可能還在寫它；發布開始之後才因例外或取消中止時，沒有確認草稿持有它（這兩種的
  路徑只寫在後端記錄裡）；發布失敗而草稿已不持有那份內容時，路徑另寫在工作的 `draft_publish.retained_path`。
- **同一時間只允許一個工具任務**：`start_install_job` 在同一把鎖底下檢查「名額是否
  被佔用」並建立新工作；被佔用時，第二個 submit 回 `409 install_in_progress`。
  **名額由這些動作共用**：背景工作——安裝、AI 修訂、跑一次範例、發布草稿（同一張 `_JOBS` 表）——以及同步的
  「重新產生總結」與 discard（兩者都用 `_SYNC_OPS` token，在同一把鎖底下
  「檢查即取得」）——所以擋下這次送出的，未必是另一個安裝。工作狀態是 in-memory、
  不持久化，後端重啟後所有工作（含仍在跑的）都會消失，FE 對舊 `job_id` 的輪詢會
  收到 404。

## 設計筆記

- **SQLite-only guard + 存活探測（persistence probe）**：`afterthread/db.py` 的
  `create_db_engine` 只接受 dialect 為 `sqlite` 的 `DATABASE_URL`（tag 篩選依賴
  SQLite 專屬的 `json_each`，沒有可攜寫法），並在建立 engine 後立刻對
  `pragma_database_list` 送一次探測查詢——不管 in-memory URL 怎麼拼（`:memory:`、
  `uri=true&mode=memory`、各種 truthy 變形…），SQLite 自己回報的 `file` 欄位一定是
  空的，藉此在啟動時就擋下任何不會落地存檔的設定，而不是留到執行期才發現資料不會
  持久化。
- **FK pragma**：SQLite 預設不強制 foreign key 約束，且是 per-connection 設定。
  `afterthread/db.py` 在每個連線的 `connect` event 上送 `PRAGMA foreign_keys=ON`，避免例如
  `POST /items/{id}/progress` 與併發的 `DELETE /items/{id}` 競速時，插入一筆指向
  已刪除項目的孤兒 progress entry。同一個 event 也註冊了 `py_casefold`
  SQLite function，讓 `q` 搜尋能做全 Unicode 範圍的大小寫不分比對（SQLite 內建
  `LIKE`／`lower()` 只認 ASCII）。
- **AI 寫入採樂觀鎖定（optimistic 409）**：`enrich`／`assist-update` 在呼叫 LLM
  **之前**先記錄項目當下的 `updated` 時間戳，LLM 呼叫全程不持有任何 DB
  transaction；寫入時用『鎖定該時間戳』的單一 conditional UPDATE 落地——若比對不到
  列，代表項目在等待 LLM 回覆期間已被刪除（404）或被別的請求改動（409），寫入直接
  作廢，不會被悄悄覆蓋。
- **supersede-not-delete**：`decisions`／`rationale`／`alternatives`／
  `consequences` 這幾個帶有歷史意義的欄位，enrich／assist-update 永遠不會直接覆
  寫。`afterthread/services/memory_ai.merge_with_supersede` 逐行比對：舊內容的每一行只要
  完整出現在新內容裡就視為保留；否則把舊內容整段接到新內容之後、掛上
  `--- (superseded YYYY-MM-DD) ---` 這樣帶日期的標記，確保之前的決策脈絡不會
  無聲消失（合併結果另外有存量上限，超出時從最舊的一段開始截斷，並留下明顯的
  截斷標記）。
- **schema-guided LLM 輸出 + 修正重試**：`afterthread/services/llm.generate_structured`
  把呼叫方的 pydantic model 轉成 JSON Schema 注入 system prompt，要求 LLM 只回傳
  『完全符合 schema 的單一 JSON 物件』；解析失敗或驗證失敗時，把模型自己的錯誤回饋
  給它、給一次修正重試機會，第二次仍失敗才映射成 `502 llm_upstream_error`（固定、
  不含設定值或原始回應內容的訊息）。所有欄位型別轉換、長度上限、陣列筆數上限都在
  pydantic 的 `mode="before"` validator 裡做防禦性處理——LLM 輸出永遠被當作不可信
  輸入。
