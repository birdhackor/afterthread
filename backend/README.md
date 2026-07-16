# Context Memory Backend

`context-memory` file-based 方法論的 web 化後端：同一套快速捕捉／全面補充／回顧
方法論，改成一個 FastAPI + SQLite 服務，供 `frontend/` 的 SPA（或任何 HTTP
client）呼叫。與 repo 根目錄既有的 file-based MVP（`.opencode/`、`memory/`、
`scripts/context_memory.py`）並存，彼此不互相依賴。

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
uv run uvicorn context_memory.main:app --port 8000        # 啟動 API（DB 不存在會自動建立）
uv run uvicorn context_memory.main:app --port 8000 --reload   # 開發時加 --reload 自動重載
uv run context-memory --port 8000              # 打包模式的 console script；資料目錄/`.env`
                                                # 邏輯見 context_memory/cli.py（`--help` 看完整選項）

uv run ruff format .                           # 格式化
uv run ruff format --check .                   # 只檢查格式（CI / gate 用）
uv run ruff check .                            # lint
uv run ty check                                # 型別檢查
uv run pytest                                  # 測試（應全數通過；精確數字隨版本演進，
                                                # 以指令實際輸出為準）
```

打包成內嵌前端 build 的可攜 wheel：見根目錄 `scripts/build-wheel.sh`；對應的
packaged-mode e2e 驗證見 `e2e/wheel_smoke.sh`。

以上指令皆已在本機實際執行過並確認通過（`uv sync` / format --check / check /
ty check / pytest 全綠；`uvicorn` 啟動後 `GET /api/health` 回
`{"status":"ok"}`，未設定 `.env` 時 DB 檔會自動建在
`backend/context_memory.db`，`GET /api/llm/status` 回
`{"configured":false,"model":null}`）。

## 環境變數（`backend/.env`，範本見 `backend/.env.example`）

所有數值型設定都在 `context_memory/config.py` 用 pydantic-settings 的 `Field(..., ge=/le=/gt=)`
在**啟動時**驗證邊界；超出範圍會讓服務直接啟動失敗，而不是留到第一次呼叫才爆炸。

| 變數 | 預設值 | 說明 |
| --- | --- | --- |
| `OPENAI_BASE_URL` | `""`（空） | OpenAI-compatible endpoint 的 base URL。留空＝AI 功能未設定，手動 CRUD 不受影響。 |
| `OPENAI_API_KEY` | `""`（空） | 對應 endpoint 的 API key。**不是**判斷「是否已設定」的條件之一——有些相容 gateway 不需要 key。 |
| `OPENAI_MODEL` | `""`（空） | 呼叫該 endpoint 時使用的 model 名稱。與 `OPENAI_BASE_URL` 兩者都非空、且 base URL 可被解析為合法 http/https URL，才算「已設定」。 |
| `OPENAI_TIMEOUT_SECONDS` | `120` | 單次 LLM 請求的逾時秒數，邊界 `(0, 1800]`。同時是「第一次呼叫 + 一次修正重試」整體的 wall-clock 上限（`asyncio.timeout` 包住整個嘗試迴圈）。預設值已經是為長 context 模型調校過的（見下方 GLM5.2 備註），不是舊版小 context 模型的 60 秒。 |
| `OPENAI_MAX_OUTPUT_TOKENS` | 未設定 | 選填，邊界 `[1, 1000000]`。設定時才以 `max_tokens` 送給 endpoint；留空＝完全不送這個參數（部分 reasoning endpoint 會拒絕顯式 `max_tokens`，但有些 gateway 預設完成長度太短，會截斷長回覆——這是給後者用的上調旋鈕）。 |
| `LLM_PROMPT_BUDGET_CHARS` | `200000` | enrich / assist-update / 工具安裝組 prompt 時，內容序列化後的最大字元數，邊界 `[4000, 2000000]`。避免超大內容撐爆小 context window 的模型；預設值已經是為大 context 模型調校過的（見下方 GLM5.2 備註）。 |
| `LLM_LOG_MAX_ENTRIES` | `50` | 「AI 日誌」頁／`GET /api/llm/logs` 顯示的最近互動筆數上限（記憶體內環狀緩衝，隨程序重啟清空），邊界 `[1, 1000]`。 |
| `LLM_LOG_BODY_MAX_CHARS` | `200000` | 單次互動中，任一則請求/回應內容儲存時的字元數上限，邊界 `[1000, 2000000]`；與 `LLM_LOG_MAX_ENTRIES` 一起讓記憶體用量在兩個軸上都有界。 |
| `LLM_LOG_FILE` | 未設定 | 選填。設定後，每次 LLM 互動會額外追加寫入這個 JSONL 檔案（與記憶體環狀緩衝相同的紀錄，一樣受 `LLM_LOG_BODY_MAX_CHARS` 截斷）；預設關閉——記錄含個人記憶內容，落不落地是使用者自己的隱私選擇。 |
| `TOOLS_DIR` | dev 未設定／打包模式自動注入 `<data-dir>/tools` | 已安裝工具套件所在目錄；留空＝工具功能整個關閉（`GET /api/tools` 回空清單，AI workflow 不帶任何工具，prompt 與無工具版本逐字相同）。dev 模式要用工具功能，需自行在 `backend/.env` 設定這個變數。 |
| `LLM_TOOL_ROUNDS_MAX` | `8` | 一次 AI workflow 呼叫最多允許幾輪工具呼叫，邊界 `[1, 64]`。 |
| `LLM_TOOL_TIMEOUT_SECONDS` | `60` | 單次工具子行程的逾時秒數（到期整個 process group 被砍），邊界 `(0, 600]`。 |
| `LLM_TOOL_OUTPUT_MAX_CHARS` | `50000` | 工具 stdout 餵回給模型的字元數上限，邊界 `[1000, 500000]`。 |
| `TOOL_INSTALL_MAX_ROUNDS` | `24` | KB 網頁安裝器（見下方「工具（KB 網頁安裝器）」）單一安裝工作階段的工具輪數上限，邊界 `[4, 64]`。 |
| `TOOL_INSTALL_TIMEOUT_SECONDS` | `900` | KB 網頁安裝器單一安裝工作階段的整體逾時秒數，邊界 `(0, 3600]`。 |
| `TOOL_INSTALL_SHELL_TIMEOUT_SECONDS` | `120` | 安裝器內單一 `run_shell` 指令的逾時秒數，邊界 `(0, 600]`。 |
| `DATABASE_URL` | `sqlite:///./context_memory.db` | SQLAlchemy URL。**只支援 SQLite**，且必須是檔案型（不可為 in-memory）——見下方設計筆記。 |
| `STALE_AFTER_DAYS` | `14` | 非終態項目（`STALE_ELIGIBLE_STATUSES`：除 `done`／`superseded` 外的五種狀態）的 `updated` 超過這個天數，API 回應的 `is_stale` 會是 `true`。邊界 `[0, 36500]`。 |

> **GLM5.2（或其他 1M-token context 模型）備註**：上面 `OPENAI_TIMEOUT_SECONDS`
> （120）與 `LLM_PROMPT_BUDGET_CHARS`（200000）的預設值本身已經是為大 context
> 模型調校過的數字。內部 LLM 若是 GLM5.2 這類 1M-token context 模型，
> `backend/.env.example` 內建了進一步調高的建議（取代上方預設）：
>
> ```bash
> LLM_PROMPT_BUDGET_CHARS=800000   # 讓超大項目也能整項進 prompt，不截斷
> OPENAI_TIMEOUT_SECONDS=300       # 長 context 生成較慢，放寬逾時避免誤判 502
> ```

## API 概覽

所有路由掛在 `/api` 前綴下（見 `context_memory/main.py`）。完整 request/response schema
以啟動後的 `GET /docs`（Swagger UI）／`GET /openapi.json` 為準；以下是各路由的
用途摘要。

**Health**
- `GET /api/health` — liveness probe。

**Items CRUD**（`context_memory/routers/items.py`，前綴 `/api/items`）
- `POST /api/items` — 建立項目，並自動寫入第一筆 progress entry。
- `GET /api/items` — 分頁列表（`limit` 1–200，預設 50；`offset`），支援
  `status`／`stage`／`tag`（JSON array 精確比對，Unicode-safe）／`q`（對
  title／snapshot／recovery_keywords 做大小寫不分、Unicode-casefold 的模糊搜尋）
  篩選；依 `updated` 新到舊排序。
- `GET /api/items/{id}` — 單筆項目 + 完整 progress 歷史（依時間正序）。
- `PATCH /api/items/{id}` — 局部更新；有提供欄位即推進 `updated`（即使值與現值相同），空 payload 不推進。
- `DELETE /api/items/{id}` — 刪除項目；progress entries 透過 DB 層 FK cascade 一併刪除。
- `POST /api/items/{id}/progress` — 追加一筆 append-only progress entry。

**Review**（`context_memory/routers/review.py`）
- `GET /api/review` — 把五種非終態項目分成
  `needs_enrichment`／`active`／`waiting`／`parked` 四組，各組依 `updated`
  舊到新排序（單一查詢一次性快照，避免分組間 race）。

**AI workflows**（`context_memory/routers/ai.py`）
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
  1–500，預設 50），新到舊排序。
- `GET /api/llm/logs/{log_id}` — 單筆互動的內容（每次嘗試的請求訊息與回應，
  每則受 `LLM_LOG_BODY_MAX_CHARS` 截斷、超過每次嘗試總量預算的較早訊息會被
  省略成一則標記、工具呼叫參數只保留約 200 字元預覽）；記錄已被環狀緩衝擠出
  （見 `LLM_LOG_MAX_ENTRIES`）或程序重啟過則 404。

AI 路由的錯誤語意：`503 llm_not_configured`（未設定端點）、
`502 llm_upstream_error`（上游呼叫失敗或輸出無法解析，即使重試一次後仍失敗）、
`409 conflict`（enrich／assist-update 於 LLM 呼叫期間，項目被別的請求改動——樂觀
並發偵測）。

**Tools**（`context_memory/routers/tools.py`，前綴 `/api/tools`；即「工具」頁的
後端）
- `GET /api/tools` — 列出所有已安裝工具套件（含無效的），依名稱排序；`TOOLS_DIR`
  未設定或尚無工具時回空清單（非錯誤）。
- `PATCH /api/tools/{name}` — 切換某工具的 `enabled`；找不到回 404。
- `DELETE /api/tools/{name}` — 刪除整個工具套件目錄；找不到回 404。
- `POST /api/tools/install` — 送出 KB 網頁安裝器工作（見下方「工具（KB 網頁
  安裝器）」）；202 + `job_id`，建置在背景執行。`TOOLS_DIR` 未設定回 `503
  tools_not_configured`；已有安裝在跑時回 `409 install_in_progress`（同一時間
  只允許一個安裝工作）。
- `GET /api/tools/install/{job_id}` — 輪詢一個安裝工作的狀態
  （`queued`／`running`／`succeeded`／`failed` + 完成後的 `tool_name`／
  `summary`／`llm_log_id`）；工作已完成的清單被裁剪掉、或後端重啟過（工作只存在
  記憶體）則回 404。

## 工具（KB 網頁安裝器）

- **工具套件格式**：`<TOOLS_DIR>/<name>/`，內含 `tool.json`（`name`／
  `description`／`parameters`(JSON Schema)／`entry`(argv)／可選 `enabled`）、
  `entry` 會執行的實作檔，以及可選的 `.env`（該工具自己的秘密，例如某個 KB 的
  API key，啟動子行程時注入）。`name` 必須等於目錄名，且符合
  `^[a-z0-9][a-z0-9_-]{0,63}$`。執行契約（`services/tools.py` 的
  `_run_tool_subprocess`）：`cwd` = 工具目錄，參數 JSON 寫進子行程 STDIN，
  STDOUT 當作結果（超過 `LLM_TOOL_OUTPUT_MAX_CHARS` 截斷）、非 0 結束碼視為
  失敗，逾時（`LLM_TOOL_TIMEOUT_SECONDS`）整個 process group 被砍。子行程環境
  是**從零打造**的（只透傳 `PATH`/`HOME`/`LANG`/`LC_ALL`/`TMPDIR` + 工具自己的
  `.env`）——絕不整包繼承父行程環境，因為父行程環境帶著 `OPENAI_API_KEY`；這
  防的是「不小心」外洩，不是對抗惡意子行程的沙箱（同 UID 的子行程理論上仍讀得
  到 `/proc/<ppid>/environ`）。
- **安裝器**（`services/tool_builder.py`，`/tools` 頁「安裝新工具」分頁的後端；
  設計依據見 `docs/web-v2-decisions.md` D21/D27）：`POST /api/tools/install`
  在背景跑一次帶有四個 meta-tool（`write_file`／`read_file`／`list_dir`／
  `run_shell`）的 `generate_structured` 工具迴圈，讓「工具建造者」LLM 讀
  OpenAPI 文件、在一個暫存目錄（`<TOOLS_DIR>/.staging/<uuid>`，隱藏目錄，
  registry 掃描略過）裡寫檔、用 `run_shell` 實際呼叫目標 API 測試，反覆直到
  判定完成或放棄；完成後以安裝套件的**同一套**驗證規則
  （`tools.validate_package`）檢查暫存內容，通過才搬進 `<TOOLS_DIR>/<name>`。
  全程互動記錄進 AI 日誌（`workflow="tool_install"`），失敗時這是主要除錯
  入口。
- **安全立場（v1）**：這是單人本機工具，shell 能力是明確需求（比照 Claude
  Code 建 skill 的能力/風險模型）——`run_shell` 是以**本服務自身權限**執行的
  真實 bash，只是預設從暫存目錄開始（工作慣例，不是圍籬），v1 刻意不做容器
  隔離。唯一被強制圍住的邊界是 `write_file`／`read_file`／`list_dir` 三個
  meta-tool：路徑一定會被限制在暫存目錄之內（絕對路徑、`..` traversal、
  symlink 逃逸都會被擋下）。信任邊界因此是「只安裝你信任的 OpenAPI 文件與
  指示」，不是程式碼在幫你圍出一個對抗式安全沙箱。安裝指示文字（作為提示）與
  工具呼叫參數的摘要／預覽（約前 200 字元）都會進 AI 日誌（見上方
  `LLM_LOG_FILE`，兩者同樣受 `LLM_LOG_BODY_MAX_CHARS` 逐則截斷）——貼給 AI 的
  任何第三方秘密都要當作「會被記錄」處理。
- **`.staging` 殘留**：清理是 best-effort（所有清理例外都被抑制），正常結束
  時通常會清掉暫存目錄；但後端在建置途中被中斷（當掉、被砍、主機重開機），
  **或清理本身失敗**（例如 `run_shell` 改了目錄權限）時，會留下
  `<TOOLS_DIR>/.staging/<uuid>`。這個目錄名稱以 `.` 開頭，registry 掃描
  （`tools._scan_all`）會直接跳過隱藏目錄，不會被列成無效工具，可以安全地手動
  刪除。
- **同一時間只允許一個安裝工作**：`start_install_job` 在同一把鎖底下檢查「是否
  已有 queued/running 的工作」並建立新工作；已有工作在跑時，第二個 submit 回
  `409 install_in_progress`。工作狀態是 in-memory、不持久化，後端重啟後所有
  工作（含仍在跑的）都會消失，FE 對舊 `job_id` 的輪詢會收到 404。

## 設計筆記

- **SQLite-only guard + 存活探測（persistence probe）**：`context_memory/db.py` 的
  `create_db_engine` 只接受 dialect 為 `sqlite` 的 `DATABASE_URL`（tag 篩選依賴
  SQLite 專屬的 `json_each`，沒有可攜寫法），並在建立 engine 後立刻對
  `pragma_database_list` 送一次探測查詢——不管 in-memory URL 怎麼拼（`:memory:`、
  `uri=true&mode=memory`、各種 truthy 變形…），SQLite 自己回報的 `file` 欄位一定是
  空的，藉此在啟動時就擋下任何不會落地存檔的設定，而不是留到執行期才發現資料不會
  持久化。
- **FK pragma**：SQLite 預設不強制 foreign key 約束，且是 per-connection 設定。
  `context_memory/db.py` 在每個連線的 `connect` event 上送 `PRAGMA foreign_keys=ON`，避免例如
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
  寫。`context_memory/services/memory_ai.merge_with_supersede` 逐行比對：舊內容的每一行只要
  完整出現在新內容裡就視為保留；否則把舊內容整段接到新內容之後、掛上
  `--- (superseded YYYY-MM-DD) ---` 這樣帶日期的標記，確保之前的決策脈絡不會
  無聲消失（合併結果另外有存量上限，超出時從最舊的一段開始截斷，並留下明顯的
  截斷標記）。
- **schema-guided LLM 輸出 + 修正重試**：`context_memory/services/llm.generate_structured`
  把呼叫方的 pydantic model 轉成 JSON Schema 注入 system prompt，要求 LLM 只回傳
  『完全符合 schema 的單一 JSON 物件』；解析失敗或驗證失敗時，把模型自己的錯誤回饋
  給它、給一次修正重試機會，第二次仍失敗才映射成 `502 llm_upstream_error`（固定、
  不含設定值或原始回應內容的訊息）。所有欄位型別轉換、長度上限、陣列筆數上限都在
  pydantic 的 `mode="before"` validator 裡做防禦性處理——LLM 輸出永遠被當作不可信
  輸入。
