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
| `LLM_LOG_FILE` | 未設定 | 選填。設定後，每次 LLM 互動會額外追加寫入這個 JSONL 檔案（與記憶體環狀緩衝相同的紀錄，一樣受 `LLM_LOG_BODY_MAX_CHARS` 截斷）；預設關閉——記錄含個人記憶內容，落不落地是使用者自己的隱私選擇。 |
| `LLM_LOG_FILE_MAX_BYTES` | `50000000` | 上述 JSONL sink 的輪替門檻（位元組）：檔案超過此大小就改名成帶 UTC 時間戳後綴、另開新檔（守磁碟；RAM 由環狀緩衝負責），邊界 `[1000000, 1000000000]`。僅在有設定 `LLM_LOG_FILE` 時有意義；輪替後的舊檔不會自動刪除，交由使用者自行清理。 |
| `TOOLS_DIR` | dev 未設定／打包模式自動注入 `<data-dir>/tools` | 已安裝工具套件所在目錄；留空＝工具功能整個關閉（`GET /api/tools` 回空清單，AI workflow 不帶任何工具，prompt 與無工具版本逐字相同）。相對路徑或指向此基底的 symlink 會在設定邊界 canonicalize 成同一個絕對路徑；這不會跟隨或放寬任何 package/version symlink 的拒絕。dev 模式要用工具功能，需自行在 `backend/.env` 設定這個變數。 |
| `LLM_TOOL_ROUNDS_MAX` | `8` | 一次 AI workflow 呼叫最多允許幾輪工具呼叫，邊界 `[1, 64]`。 |
| `LLM_TOOL_TIMEOUT_SECONDS` | `60` | 單次工具子行程的逾時秒數（到期整個 process group 被砍），邊界 `(0, 600]`。 |
| `LLM_TOOL_OUTPUT_MAX_CHARS` | `50000` | 工具 stdout 餵回給模型的字元數上限，邊界 `[1000, 500000]`。 |
| `LLM_TOOL_CONVERSATION_BUDGET_TOKENS` | `500000` | 工具迴圈中「實際送給模型的對話」token 預算，邊界 `[50000, 1000000]`；超過即停止附掛工具、走 finalize（防多輪工具結果累積撐爆 context/記憶體）。同樣經字元↔token 比值換算成字元上限。改名自舊的 `LLM_TOOL_CONVERSATION_BUDGET_CHARS`（舊鍵會被靜默忽略）。 |
| `TOOL_INSTALL_MAX_ROUNDS` | `24` | KB 網頁安裝器（見下方「工具（KB 網頁安裝器）」）單一安裝工作階段的工具輪數上限，邊界 `[4, 64]`。 |
| `TOOL_INSTALL_TIMEOUT_SECONDS` | `900` | KB 網頁安裝器單一安裝工作階段的整體逾時秒數，邊界 `(0, 3600]`。 |
| `TOOL_INSTALL_SHELL_TIMEOUT_SECONDS` | `120` | 安裝器內單一 `run_shell` 指令的逾時秒數，邊界 `(0, 600]`。 |
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

AI 路由的錯誤語意：`503 llm_not_configured`（未設定端點）、
`502 llm_upstream_error`（上游呼叫失敗或輸出無法解析，即使重試一次後仍失敗）、
`409 conflict`（enrich／assist-update 於 LLM 呼叫期間，項目被別的請求改動——樂觀
並發偵測）。

**Tools**（`afterthread/routers/tools.py`，前綴 `/api/tools`；即「工具」頁的
後端）
- `GET /api/tools` — 列出所有已安裝工具套件（含無效的），依名稱排序；`TOOLS_DIR`
  未設定或尚無工具時回空清單（非錯誤）。每列帶 `current_vid` 與三態
  `lineage`（`sole`／`usable`／`broken`）；套件沒有可解析的生效版本時仍會列出，
  但 `description`／`current_vid` 為 `null`、`valid=false`、`lineage=broken`，只能
  整包刪除。`description` 可為空不是把 manifest 契約放寬，而是讓壞掉的套件仍有
  一列可供清理。
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
- `DELETE /api/tools/{name}` — 刪除整個工具套件目錄；找不到回 404。若刪除當下**正好
  有工具子行程在跑任一版本**，套件目錄不會被直接刪掉，而是改名成一個隱藏名稱、等該次
  呼叫結束後，由**同一個後端程序**稍後觀察到 idle 時才可在工具工作的收尾清掃中收走——
  直接刪會讓那個子行程的相對開檔全部失敗。工具在回應那一刻就已經從清單與模型可見的工具中
  消失，行為與立即刪除沒有差別。若後端在兩者之間被 hard restart，
  `start_new_session` 子行程可能仍活著，但 process-local registry 已清空；新程序因此不會
  自動清掉前一程序留下的隱藏目錄，須由操作者確認子行程已結束後自行處理。
- `DELETE /api/tools/{name}/versions/{vid}` — 丟掉畫面所指的**精確目前版本**。後端先
  取得全域工具名額，再比對 path 裡的 `vid`；不相符回
  `409 version_mismatch`。`lineage=usable` 時把 `current` 原子切回
  `origin.json.previous`，之後才盡力清掉原版本；若原版本仍在執行則先改名成
  `<vid>.discarded`，同樣只允許觀察過 running→idle 的原後端程序清掃；跨程序留下者
  交由操作者確認後處理。`lineage=sole` 在 UI 走既有的整包刪除確認；
  前一版缺失、未提交或指回自己則回 `409 lineage_unavailable`，不動任何檔案。
- `POST /api/tools/install` — 送出 KB 網頁安裝器工作（見下方「工具（KB 網頁
  安裝器）」）；202 + `job_id`，建置在背景執行。`TOOLS_DIR` 未設定回 `503
  tools_not_configured`；名額被佔用時回 `409 install_in_progress`。**那個名額
  不是安裝專屬的**：安裝、AI 修訂與同步的「重新產生總結」共用同一個 single-flight
  （見下方三條路由與「依意見修訂既有工具」），所以這個 409 最常見的來源其實是
  別的分頁正在修訂。代碼與訊息字串維持既有的 `install_in_progress`／
  「已有安裝正在進行中，請等待其完成」（前端有 pin 住的分支，不動），但前端**刻意
  覆寫**成中性文案「已有工具任務正在進行中（安裝、AI 修訂或重新產生總結），請等待
  完成後再安裝」——指名一個使用者從沒送出的「安裝」只會讓他去找一個不存在的東西。
- `GET /api/tools/jobs/{job_id}` — 輪詢一個工具工作（安裝**或**修訂）的狀態
  （`queued`／`running`／`succeeded`／`failed` + 完成後的 `tool_name`／
  `summary`／`llm_log_id`／`llm_log_process`）；工作已完成的清單被裁剪掉、或後端
  重啟過（工作只存在記憶體）則回 404。`llm_log_process` 與側檔那一個是同一個
  token、同一條「沒有 id 就沒有 token」規則，只是**由這個路由在回應當下蓋章**：
  工作表是行程內記憶體，與 id 空間同生共死，所以還答得出來的工作，它的
  `llm_log_id` 必然是本行程鑄的（側檔要存下來是因為**檔案**活得比行程久）。工作
  本身不會跨重啟，但**瀏覽器分頁會**：前端在工作終局後就停止輪詢並一直沿用快取，
  所以那張卡片與它的「查看 AI 日誌」連結可能在重啟後還留在畫面上——token 跟著
  連結走，AI 日誌頁才拒絕得掉。安裝與修訂共用同一張工作表、同一個輪詢端點，回應形狀完全
  相同（前身是 `GET /api/tools/install/{job_id}`，改名後舊路徑**直接移除**、不留
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
  否則窗內被放行的修訂會換掉整包、寫下新的 sidecar，再被這次較舊的總結蓋回去；LLM 未
  設定／上游失敗與捕捉、補齊等同步 AI 動作共用同一組錯誤（`503
  llm_not_configured`／`502 llm_upstream_error`），失敗時不會覆蓋既有總結；
  取得名額後若目前版本不是 `expected_vid`，回 `409 version_mismatch`，不花 LLM。
- `POST /api/tools/{name}/revise` — body
  `{feedback: 1..20000, expected_vid}`，依使用者意見
  送出一次 **AI 修訂**工作（見下方「依意見修訂既有工具」）；202 + `job_id`，
  用上面的 `GET /api/tools/jobs/{job_id}` 輪詢。工具不存在回 404；已有工具工作
  在跑回 `409 job_busy`；取得名額後版本不符回 `409 version_mismatch`，兩者都不
  建立工作。**不宣告** `502`／`503`：修訂是背景工作，LLM 沒
  設定或上游失敗都封在工作狀態裡（失敗的 job + 友善訊息 + AI 日誌連結），與
  安裝送出同一套契約。
  這三條路由都**不宣告** `503 tools_not_configured`：`TOOLS_DIR` 未設定時
  任何名稱都解析不到工具，404 已經是誠實答案。

## 工具（KB 網頁安裝器）

- **磁碟版面與所有權**：

  ```text
  <TOOLS_DIR>/<name>/
      .afterthread.meta/
          state.json
          current
          migration-owner.json  # 只存在於一次性 web-v5 遷移產物
      versions/
          <vid>/
              .afterthread.meta/
                  origin.json
                  summary.json       # 可缺
              tool.json
              run.py ...
          <vid>.discarded/           # 執行中版本的暫停清理名
      .env
  ```

  套件層只有後端狀態與操作者的 `.env`；工具內容全部在版本層。
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
  `<TOOLS_DIR>/.staging/<uuid>/{build,shell}`；`finally` 永遠清完整 session root。
  builder 在 `build` 工作，後端先擷取並剝除 builder 寫的根層 `.env`，再剝除
  版本根層的 `.afterthread.meta/`，然後驗證 `BuildRoot`。legacy
  `.ai_meta.json` 與 `.afterthread-state.json` 在版本層是一般工具內容；同名的舊
  package-root 檔案才屬於 legacy 後端命名空間。新安裝在 `shell`
  組出完整 package：版本內容、`origin.json(previous=null)`、初始 enabled state、
  `current`，以及表單秘密注入的 package `.env`；所有內容持久化後，以一次
  `os.rename(shell, <name>)` 上線。durability walker 只 fsync 這棵樹內的真目錄：
  內容中的 directory symlink 會保留但不跟隨、不 fsync 外部目標；任何真實樹內目錄
  若所屬 filesystem 拒絕 directory fsync，durability 仍未成立，整次 build 失敗。
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
  內容到 `build`；只排除該版後端自己的根層 `.afterthread.meta/` 與 package
  `.env`。版本中的 legacy `.ai_meta.json`、FOREIGN
  `.afterthread-state.json`（包含 migration 帶入者）及其巢狀同名檔都是工具內容，
  會進工作區並隨新版本保留。package `.env` 不被複製或改寫，只把解析後的值注入
  `run_shell` 供實測與遮蔽；builder 寫出的 `.env` 同樣被剝除。通過驗證後，
  `shell` 被組成一個版本目錄並先寫
  `origin.json(previous=<舊 vid>, feedback=<本次意見>)`；版本持久化、rename 到
  `versions/<新 vid>` 後才原子發布 `current`。因此舊版本永久保留、套件 state 與
  `.env` 原地不動，`current` 是修訂唯一 commit point。六位 hex 可能碰撞，建立時
  會避開 `versions/` 下所有以候選 vid 開頭的項目，包括 `.discarded`。
- **執行綁定廣告時的版本**：registry 廣告工具時把 `PackageRoot`、
  `VersionRoot`、entry 與 manifest identity 一起封進 handler；模型稍後真的呼叫時
  直接以那個 `VersionRoot` 當 cwd。package `.env` 與開關則在呼叫當下讀。
  **`current` 在呼叫當下仍會解析一次，但只用來確認「這個套件還有可用的版本」，
  不用來決定跑哪一版**——否則廣告綁定就沒有意義了。少了這道確認，列表會把
  `current` 壞掉的套件判成 invalid＋停用，而已廣告的 handler 照樣跑得起來，
  同一個問題兩條路徑兩個答案（overall review r2-4）。這避免 pointer 改變後用舊
  schema 執行新程式；若操作者在廣告與
  呼叫間親手 discard 該版，呼叫會得到明確拒絕，系統刻意不為這個單人操作引入
  reservation/refcount。
- **版本保留、lineage 與 discard**：`origin.json.previous` 是前一版的唯一來源，
  不是目錄排序；沒有自動回收。`lineage=sole` 表示 previous 為 null，
  `usable` 表示 previous 是另一個合法已提交版本，`broken` 表示 pointer 壞掉或
  指回自己。discard 的順序固定為：確認 expected vid → sole 則整包刪除 → 驗前一版
  → 發布 `current=P` → 只有 pointer 的目錄 fsync 已確認才盡力移除 V。V 還在執行
  時改名 `<vid>.discarded`；無法確認持久化時保留 V 但仍回成功，因為多一個未指向
  版本比「斷電後 current 指到已刪版本」安全。掃描捕捉一個可解析版本時，就在
  process-local registry 取得或建立該 `VersionRoot` 的 advertisement generation；
  同一輪稍後建立的每個 handler 都持有這個物件。discard 成功發布 `current=P` 時，
  會在與掃描捕捉共用的 lock 內把 V 當下的 generation 標成 retired 並移出
  registry；因此即使持久化未確認或停放 rename 失敗、使 V 留在原廣告路徑，舊
  handler 仍明確拒絕。retired generation 由既有 handler 持有到該對話／handler
  釋放為止；之後從備份恢復並重新掃描同一 vid 會取得新 generation。退役完全不讀
  目錄的 device、inode 或其他可由 rename／刪除／重建影響的磁碟屬性，避免磁碟
  身分重用或搬回原位使舊廣告復活。執行保護與 deferred-cleanup 資格也都是
  process-local：同一程序內，delete、discard 與 `.stale-` 清掃共用「所有版本是否
  running」判斷；跨程序則不把空 registry 當成 idle 證明。新程序從未親眼看過
  running→idle 的 stale／discarded tree 一律保留給操作者。
- **AI 總結拆成兩份 sidecar**：每版不可變的
  `.afterthread.meta/origin.json` 保存來源、安裝指示、修訂意見與 previous；
  可重新產生的 `.afterthread.meta/summary.json` 保存 summary、updated time 與
  AI log identity。兩者都經 typed／bounded／secret-redacted／fail-closed／atomic
  publisher；來源 URL 在捕捉時先收斂成不帶 userinfo/path/query/fragment 的
  provenance。summary 缺席或損壞是「尚無可用總結」，不會使版本失效；安裝／修訂
  上線後的自動產生是 best-effort，失敗不能翻轉已完成的提交。
- **全域 single-flight**：安裝與修訂共用背景 job 表；同步 regenerate 與 discard
  也透過同一個 admission lock 取得名額。它們的 API 形狀不同，但比對
  `expected_vid` 都排在取得名額之後、實際寫入／花 LLM 之前，避免另開一條
  check-then-write race。背景 job 是 process-local，重啟後舊 id 404。
- **安全立場（v1）**：builder 的 `run_shell` 是服務權限下的真實 bash，不是沙箱；
  只有 `write_file`／`read_file`／`list_dir` 被限制在 build root。信任邊界仍是
  「只安裝可信的 OpenAPI 文件與指示」。工具子行程的環境從零建立，只透傳
  `PATH`／`HOME`／`LANG`／`LC_ALL`／`TMPDIR`，再加 package `.env` 與正規化的
  TLS 設定，避免不小心把父行程 `OPENAI_API_KEY` 一起交出去；同 UID 行程仍可能
  讀 `/proc`，所以這不是對抗惡意程式的隔離。
- **一次性 web-v5 遷移**：先停掉 afterthread；為使 migrated version 精確反映開始時
  的內容，仍建議關閉編輯器並暫停手動／同步寫入。即使遷移期間發生 autosave，舊套件
  現在也不會被刪除：那次編輯會留在 retained quarantine，**不保證進入 migrated
  version**，操作者可事後比對與取回。確認 `TOOLS_DIR` 指向舊扁平套件，再於
  `backend/` 執行：

  ```bash
  uv run python -m afterthread.migrate_tools_v5 --dry-run
  uv run python -m afterthread.migrate_tools_v5
  # 已人工審過同一份計畫的非互動執行：
  uv run python -m afterthread.migrate_tools_v5 --yes
  ```

  `--dry-run` 只做唯讀預檢並列出整體計畫；一般執行會逐套件報告 legacy state／
  `.ai_meta.json` 分類、enabled 與 `.env` **key 名**（永不列值），然後在第一次
  寫入前最後一次詢問。只有明確 `y`／`yes` 才繼續；拒絕、關閉或無法詢問的 stdin
  都視為 no，tree 保持 byte-identical。`--yes` 只適合計畫已審過的自動化執行。
  確認後先在 `TOOLS_DIR` 的兄弟位置做完整備份，再建立
  `.afterthread-migration.json` write-ahead journal。預檢一次收集所有問題；任何
  legacy 套件不可遷移就完全不開始。每包把工具內容搬進單一初始版本、移除
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
  vid + role 的 marker。對 migration 自己建立的 partial **新 shell**，identity +
  marker（完整 target-layout 亦可用精確 VID）仍是清理證明；新 shell 在空目錄時先記
  identity，再以目前 journal 的 atomic hard link 暫時充當 bootstrap marker，正式
  JSON marker 完整落盤後才移除 bootstrap。這套 destructive ownership proof 不再用於
  committed 舊套件：舊套件只產生 256-bit 隨機 quarantine 名稱、rename 過去並把
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
  還會被存進側檔的 `origin` 供之後的修訂 session 使用——**打進 `instructions`
  的東西一律當成「模型看得到、日誌留得住」處理**。憑證因此走
  `secret_name`／`secret_value` 這對欄位（下一條），不是寫進指示：貼在指示裡的
  值在它還不是「已知秘密」的那一刻就落地了，遮蔽器事後補登記救不回既有紀錄。
- **安裝表單的秘密欄位**（`secret_name`／`secret_value`，D36；兩者要嘛都給、
  要嘛都不給，名稱須為合法環境變數名，值至少 6 字元且不含換行，否則 422）：值
  以該名字注入 `run_shell` 的環境（AI 因此能實測真的 API）、整個建置期間登記成
  已知秘密（AI 日誌落地前就遮蔽）、驗證通過後由後端寫進該工具自己的 `.env`
  （`_inject_secret_into_env`，在 `validate_tool_content` 之後、搬移之前）。值不進任何
  提示，模型只拿得到**名字**。
- **`.staging` 殘留**：清理是 best-effort（所有清理例外都被抑制），正常結束
  時通常會清掉暫存目錄；但後端在建置途中被中斷（當掉、被砍、主機重開機），
  **或清理本身失敗**（例如 `run_shell` 改了目錄權限）時，會留下
  `<TOOLS_DIR>/.staging/<uuid>`。這個目錄名稱以 `.` 開頭，registry 掃描
  （`tools._scan_all`）會直接跳過隱藏目錄，不會被列成無效工具，可以安全地手動
  刪除。
- **同一時間只允許一個工具任務**：`start_install_job` 在同一把鎖底下檢查「名額是否
  被佔用」並建立新工作；被佔用時，第二個 submit 回 `409 install_in_progress`。
  **名額由四種動作共用**：安裝工作、AI 修訂工作（同一張 `_JOBS` 表），以及同步的
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
