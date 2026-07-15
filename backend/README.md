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
| `OPENAI_TIMEOUT_SECONDS` | `60` | 單次 LLM 請求的逾時秒數，邊界 `(0, 600]`。同時是「第一次呼叫 + 一次修正重試」整體的 wall-clock 上限（`asyncio.timeout` 包住整個嘗試迴圈）。 |
| `LLM_PROMPT_BUDGET_CHARS` | `32000` | enrich / assist-update 組 prompt 時，項目快照序列化後的最大字元數，邊界 `[4000, 200000]`。避免超大項目撐爆小 context window 的模型。 |
| `DATABASE_URL` | `sqlite:///./context_memory.db` | SQLAlchemy URL。**只支援 SQLite**，且必須是檔案型（不可為 in-memory）——見下方設計筆記。 |
| `STALE_AFTER_DAYS` | `14` | 非終態項目（`STALE_ELIGIBLE_STATUSES`：除 `done`／`superseded` 外的五種狀態）的 `updated` 超過這個天數，API 回應的 `is_stale` 會是 `true`。邊界 `[0, 36500]`。 |

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

AI 路由的錯誤語意：`503 llm_not_configured`（未設定端點）、
`502 llm_upstream_error`（上游呼叫失敗或輸出無法解析，即使重試一次後仍失敗）、
`409 conflict`（enrich／assist-update 於 LLM 呼叫期間，項目被別的請求改動——樂觀
並發偵測）。

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
  完整出現在新內容裡就視為保留；否則把舊內容整段接到新內容之後、掛上帶日期的
  `superseded` 標記，確保之前的決策脈絡不會無聲消失（合併結果另外有存量上限，超出
  時從最舊的一段開始截斷，並留下明顯的截斷標記）。
- **schema-guided LLM 輸出 + 修正重試**：`context_memory/services/llm.generate_structured`
  把呼叫方的 pydantic model 轉成 JSON Schema 注入 system prompt，要求 LLM 只回傳
  『完全符合 schema 的單一 JSON 物件』；解析失敗或驗證失敗時，把模型自己的錯誤回饋
  給它、給一次修正重試機會，第二次仍失敗才映射成 `502 llm_upstream_error`（固定、
  不含設定值或原始回應內容的訊息）。所有欄位型別轉換、長度上限、陣列筆數上限都在
  pydantic 的 `mode="before"` validator 裡做防禦性處理——LLM 輸出永遠被當作不可信
  輸入。
