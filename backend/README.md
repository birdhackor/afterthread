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
| `TOOLS_DIR` | dev 未設定／打包模式自動注入 `<data-dir>/tools` | 已安裝工具套件所在目錄；留空＝工具功能整個關閉（`GET /api/tools` 回空清單，AI workflow 不帶任何工具，prompt 與無工具版本逐字相同）。dev 模式要用工具功能，需自行在 `backend/.env` 設定這個變數。 |
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
  未設定或尚無工具時回空清單（非錯誤）。每一列附帶
  `summary_status`（`"draft"`／`"final"`／`null`＝尚無可讀的總結 sidecar），
  列表頁靠它直接標示每個工具的總結狀態，不必逐一再打一次總結 API。內部別名
  （`tools/alias -> tools/real` 這種 symlink）那一列固定回 `null`，與 by-name 的三條
  總結路由一致（它們對別名都回 404）——否則列表會標示**真包**的狀態，而那個標示
  任何請求都重現不出來。一列的六個欄位**一定出自同一個套件實例**：manifest 掃描
  與 sidecar 讀取是兩次讀，中間夾著一次修訂換裝就可能拼出「A 的說明配 B 的徽章」，
  所以掃描捕捉的身分會在 sidecar 讀完之後重驗，不符就把那一包重掃一次（實例真的
  換掉時列的是**新**那一包）；重驗一直不成立就只把 `summary_status` 降成 `null`
  ——與「沒有／讀不懂 sidecar」同一個答案，不另立詞彙。
- `PATCH /api/tools/{name}` — 切換某工具的 `enabled`；找不到回 404。寫的是套件的
  `.state.json`（見下方「工具套件格式」），**完全不碰 `tool.json`**——所以一次
  切換不會移動 manifest 身分，不會讓進行中的修訂作廢、也不會讓一趟總結往返的
  寫入被拒。**修訂或重新產生進行中也可以切換**：落在修訂裡的一次 `PATCH` 會被
  換裝前的重新寫入帶過去，落在換裝那一瞬間的則等換裝結束、寫到剛發佈的那一包
  （見下方 `.state.json` 那節），兩邊都算數，前端因此不再把開關鎖起來。
  `tool.json` 讀不到／過大／是 FIFO 都不再是拒絕理由（那些檢查守的是
  已經不存在的 manifest 改寫），因此壞掉的套件現在也關得掉；它照樣列成無效、
  照樣不會被端給模型。
- `DELETE /api/tools/{name}` — 刪除整個工具套件目錄；找不到回 404。若刪除當下**正好
  有工具子行程在跑那個套件**，目錄不會被直接刪掉，而是改名成一個隱藏名稱、等該次呼叫
  結束後由下一個工具工作的收尾清掃收走——直接刪會讓那個子行程的相對開檔全部失敗。工具
  在回應那一刻就已經從清單與模型可見的工具中消失，行為與立即刪除沒有差別。
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
- `GET /api/tools/{name}/summary` — 該工具的 AI 總結（`summary`／`status`／
  `updated_at`／`llm_log_id`，見下方「安裝後的 AI 總結與定版」）；尚無總結時
  四個欄位皆為 `null` 的 200（不是錯誤），工具不存在（含 `TOOLS_DIR` 未設定）
  才回 404。`llm_log_id` 另外在**後端重啟過**（側檔記的是前一個行程的 id）時
  一律回 `null`，理由見下方同一節。
- `PATCH /api/tools/{name}/summary` — body `{status: "draft"|"final"}`，定版／
  解除定版；成功回更新後的總結。工具不存在回 404；**沒有東西可定版**回
  `409 summary_missing`——包含「還沒有 sidecar」與「sidecar 的 `summary` 是空的
  ／只有空白」兩種，因為對使用者是同一個答案（而且定版一個空總結不是無害的
  no-op：之後重新產生會被 `tool_finalized` 擋住，唯一能補內容的動作反而被鎖
  死）。反方向的 `draft`（解除定版）**永遠不設條件**——逃生門不能自己被擋住。
- `POST /api/tools/{name}/summary/regenerate` — **同步**重新產生總結（不是背景
  工作），成功回新的總結。工具不存在回 404；已定版回 `409 tool_finalized`
  （要先解除定版）；有工具工作正在進行時回 `409 tool_job_in_progress`——而且它
  **自己也會佔住那個名額**（整趟 LLM 往返期間，install／revise 送出一律 409），
  否則窗內被放行的修訂會換掉整包、寫下新的 sidecar，再被這次較舊的總結蓋回去；LLM 未
  設定／上游失敗與捕捉、補齊等同步 AI 動作共用同一組錯誤（`503
  llm_not_configured`／`502 llm_upstream_error`），失敗時不會覆蓋既有總結。
  `tool_finalized` 有**兩個發生點**、同一個代碼：呼叫前的檢查，以及等 LLM 回來
  要寫檔時的再檢查——中途被 `PATCH` 定版的話，剛產生的文字**不會寫進去**，一樣
  回 409。
- `POST /api/tools/{name}/revise` — body `{feedback: 1..20000}`，依使用者意見
  送出一次 **AI 修訂**工作（見下方「依意見修訂既有工具」）；202 + `job_id`，
  用上面的 `GET /api/tools/jobs/{job_id}` 輪詢。工具不存在回 404；已定版回
  `409 tool_finalized`（定版就是凍結 AI 迭代）；已有工具工作在跑回
  `409 tool_job_in_progress`。**不宣告** `502`／`503`：修訂是背景工作，LLM 沒
  設定或上游失敗都封在工作狀態裡（失敗的 job + 友善訊息 + AI 日誌連結），與
  安裝送出同一套契約。
  這四條路由都**不宣告** `503 tools_not_configured`：`TOOLS_DIR` 未設定時
  任何名稱都解析不到工具，404 已經是誠實答案。

## 工具（KB 網頁安裝器）

- **工具套件格式**：`<TOOLS_DIR>/<name>/`，內含 `tool.json`（`name`／
  `description`／`parameters`(JSON Schema)／`entry`(argv)——**只有規格**）、
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
- **啟用開關存在 `.state.json`，不在 `tool.json` 裡**（web-v5 P1，設計依據見
  `docs/web-v5-decisions.md`）。套件層的隱藏檔，內容就是 `{"enabled": bool}`，
  寫入者**只有兩個**，而且共用同一套原子發佈（mkstemp → fsync → `os.replace`
  → **fsync 目錄**，並保留既有檔案的權限）：`PATCH /api/tools/{name}`
  （`tools.set_enabled`），以及**修訂換裝前**把正式套件當下的狀態（含檔案權限）
  重新寫進暫存區的那一步（`tools.carry_package_state`，見下面保留名域那一項）。
  兩者互斥執行，所以一次落在換裝過程中的 `PATCH` 不會被換裝原樣蓋回去。
  目錄的 fsync 讓改名**斷電也不會回頭**：這個檔案不像總結可以重新產生，而且它遺失
  的方向是**開**（legacy 欄位寫著 true 的舊套件，一次成功關掉之後掉電，退回
  fallback 就自己開回來），與本子系統其他每一處的失敗方向相反。代價是一次發佈
  約 1.2 → 2.5 ms（本機 ext4 實測），而發佈只發生在人按開關、一趟總結往返結束、
  一次修訂換裝——都不在掃描或呼叫路徑上。`set_enabled` 另外在**取鎖之後、發佈之前**
  重驗一次名稱解析與 containment：解析出的路徑是個字串，等鎖可能等掉一整個修訂尾段，
  而發佈器自己的 `lstat` 只管最後一段、不管任何上層目錄。**為什麼分開**：`tool.json` 的
  `(dev, ino, ctime)` 是本子系統回答「這個路徑上還是我剛才看的那一包嗎」的判準
  （修訂換裝、總結側檔寫入、對話中途執行前都要問），而 `enabled` 住在裡面時，
  一次開關就地改寫那個檔案、把判準推走——一次開關對每一道守衛都長得像「整包被
  換掉」。現在一次開關讓 manifest **逐位元組不變、身分也不動**。
  - **遷移**：沒有 `.state.json` 的既有套件，退回讀 `tool.json` 的舊 `enabled`
    欄位（預設 true）。這就是全部的遷移——沒有啟動掃描、讀取路徑永遠不寫入；
    第一次切換才產生 `.state.json`，且**刻意不**刪除 manifest 裡那個已成 legacy
    的欄位（為了整潔改寫 manifest，正好會移動這次改動要固定住的身分）。
  - **「有效啟用狀態」只有一條優先序，執行前那道檢查也走它**（`package_enabled`，
    就是掃描回答清單與廣告時用的同一個答案）。對話中途被停用的工具在執行前會被
    拒絕（回一段「這個工具在被端出去之後被停用了」的結果字串，與「套件被換掉」
    分開講），而那道檢查**不是只看狀態檔**：狀態檔不存在時一樣退回 manifest 的
    legacy 欄位。差別看得見的情形：一個 legacy 欄位寫著 `false` 的舊套件被 `PATCH`
    打開、廣告出去，之後狀態檔又被刪掉（上面第二種修法）——有效狀態回到 `false`，
    只看狀態檔的檢查會把它跑起來。manifest 身分檢查只證明 `tool.json` 沒被動過，
    它不看 `.state.json`。檢查放在**兩個**地方：handler 進入時，以及子行程
    `Popen` 的前一行（中間夾著 `.env` 讀取、參數序列化與長度無上限的 threadpool
    排隊）。**優先序本身是一個小函式**（`_effective_enabled`），掃描與這兩處檢查
    都呼叫它——掃描把自己已經讀到的兩份資料交給它，執行前的檢查則由
    `package_enabled` 去讀；所以「清單、廣告與執行回答同一個問題」是同一份程式碼
    的性質，不是兩份拼法要靠人維持一致。`package_enabled` **最後才讀 `.state.json`**
    （不存在時先取 manifest 的 legacy 值、再把狀態檔讀一次），所以子行程啟動前那一
    行拿到的值與 `Popen` 之間只隔一個比較。代價寫明：一次呼叫多約 0.1 ms×2
    （本機實測；對比一次工具呼叫本身約 37.6 ms）——這個檢查曾經是一整趟
    `_scan_package`（約 0.5 ms），那不只是慢，是讀完開關之後又做了十幾次檔案操作
    才啟動子行程，落在那段裡的 `PATCH` 照樣出貨。
  - **檔案在但讀不出來 ≠ 沒設定**：截斷／不是 JSON／`enabled` 不是布林／被換成
    symlink・FIFO・目錄，一律代表「操作者的意圖不明」，該套件列成
    `valid=false`、`error=".state.json exists but is not readable"`，同時
    `enabled=false`。刻意不退回預設開啟——那等於把一個被特意關掉的工具重新交給
    模型。**修法依「壞掉的是什麼」分兩種**：
    - **一般檔案但內容壞掉**（截斷、不是 JSON、`enabled` 不是布林、超過上限）：
      再按一次開關就修好——PATCH 不讀這個檔案，直接覆蓋成乾淨的一份；或自己把
      檔案刪掉退回上面的 fallback。
    - **那個名字上不是一般檔案**（symlink、FIFO、目錄）：**按開關沒有用**。
      發佈器的 pre-write `lstat` 一律拒絕非一般檔（這是刻意的寫入邊界性質，
      `os.replace` 換掉的是連結本身、從不寫穿它），所以 PATCH 會失敗、路由回
      404，而那個 FIFO／連結／目錄原封不動。**只能自己動手把該項目移除**（`rm`／
      `rm -r` 那個 `.state.json`），之後再按開關或直接退回 fallback——手改套件
      檔案本來就是 D21 明文支援的行為，操作者按定義有 shell。
    （前端的開關在 `valid=false` 的列上是停用的，所以 UI 上一律是「自己刪檔或
    重裝」；API 則只有上面第一種情況通。）
  - **`.state.json` 與 `.ai_meta.json` 同屬後端保留名域**：builder session 不能
    出貨（promote 前一律剷除，含 `<名稱>.*.tmp` 發佈暫存檔、含各層級、比對大小寫
    不敏感），修訂複製也不帶進暫存區；但換裝是整包替換，所以換裝前後端會把**正式
    套件當下的啟用狀態**（連同該檔案的權限）重新寫進暫存區，寫不進去就拒絕換裝
    ——否則每一次修訂都會把你刻意關掉的工具靜默打開。這一步刻意排在**能排的最後
    一刻**（身分重檢的前一行），而且與 `PATCH` 互斥：讀到的開關值與換裝之間若還
    夾著別的工作，那段時間內的一次 `PATCH` 就會被換裝靜默蓋回去。
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
- **安裝後的 AI 總結與定版**（`services/tool_meta.py`，設計依據見
  `docs/web-v4-decisions.md` D40）：搬進正式目錄之後，安裝工作會再跑一次**獨立
  的**短 AI session（`workflow="tool_summary"`，與 `tool_install` 分開，
  日誌連結才不會互相認錯）讀這個套件的檔案，產生「做了什麼／原理／輸入輸出／
  限制」的說明，寫進套件內的 `.ai_meta.json`（隱藏檔，registry 掃描看不到，
  `DELETE` 時隨整個目錄一起消失）。這一步是 **best-effort**：總結失敗（LLM 未
  設定、上游錯誤、任何例外）都被就地吞掉，絕不會把已經成功的安裝翻成失敗，只
  會留下空總結的 sidecar 供之後 `regenerate`。那份佔位側檔的 `llm_log_id` **只會
  是這次呼叫自己跑出來的 session，或是 `null`**：提示是先在 threadpool 組出來、
  之後才呼叫 LLM 的，所以失敗可能發生在**還沒有任何 session** 的前半段，而那時
  「這個 workflow 最新的一筆」是**上一個工具**的總結——寧可沒有連結，也不要把別的
  套件的提示與回應當成這一次的失敗軌跡。sidecar **不是把呼叫端的 dict 直接
  序列化**，而是照固定 schema（`summary`／`status`／`updated_at`／`llm_log_id`／
  `llm_log_process`／`origin`）重建：檔案裡每一個 key 都是後端寫死的字面值，值也
  一律被收斂到約定的型別（未知 `status` → `draft`、非 int 的 `llm_log_id` →
  `null`、`origin` 只留看得懂的兩個字串欄位），手動加的多餘 key 下一次寫入就會被
  丟掉（這本來就不是契約）。`llm_log_process` 是**寫下那個 `llm_log_id` 的行程**
  的識別碼：AI 日誌的 id 是每個行程各自從 0 開始的計數器、環狀緩衝重啟即空，而
  這個檔案會把整數永久留著，所以重啟之後同一個 id 指到的是**現在**佔著它的那次
  互動（別的工具的總結，甚至別的 workflow）。`GET .../summary` 因此只在 token 等
  於當前行程時才把 `llm_log_id` 交出去，否則回 `null`（＝沒有可連的紀錄，前端本來
  就是這樣渲染的）；token 由 `store_summary_meta` 在寫下 id 的同一個動作裡蓋章，
  定版／解除定版的重寫則**原樣沿用**磁碟上的那一組，絕不重新蓋章。三個可能帶操作者／LLM 文字的**值**（`summary`、`origin.openapi_url`、
  `origin.instructions`）一律過 `redact_known_secrets` 且**遮蔽失敗就不寫**——
  而這道遮蔽是這個檔案**唯一**的防線，不是「反正後面還有一關」。（舊版本這裡寫
  「sidecar 之後會被修訂流程複製進暫存目錄接受『檔案不得內嵌秘密值』檢查」——那不
  成立：修訂的複製在**任何層級**都排除 sidecar 的保留命名空間，`_strip_builder_sidecars`
  又會在驗證**之前**把暫存區裡的 sidecar 刪掉，所以 sidecar 從來不會走到那道閘。
  寫清楚是因為誤以為下游還有一關的人，會覺得把這裡的 fail-closed 放寬成「遮不掉就
  照寫」是安全的。）`origin.openapi_url` 另外**先被收斂**成
  `scheme://host[:port]`（userinfo／path／query／fragment 只要存在任何一項就整段丟
  掉、補一個固定標記；host[:port] 另外驗證形狀——`urlsplit` parse 得出 netloc 不代表
  它是合法主機，例如 `Bearer SECRET` 這種字串也會 parse 成功；無法解析、scheme 不是
  http/https、或 host 形狀不合，都留空，絕不回傳原值）：URL 裡的憑證常常是遮蔽器沒
  登記過的（presigned 連結、路徑裡的能力型 token），或是登記了但以 percent-encoding
  出現（`abc+/` vs `abc%2B%2F`），值比對抓不到——而這個 URL 只是出處顯示、沒有任何
  流程會再抓一次，連路徑一起丟掉也不會少任何功能。收斂發生在 `run_install` 交給
  summary hook 的那一刻（原值不離開 installer），prompt 與讀回舊 sidecar 時再各收斂
  一次（涵蓋手改與此修正之前寫下的檔案，不需要資料遷移）。
  `summary` 的遮蔽／trim／截斷（`_TOOL_SUMMARY_CAP`）**在寫入端一次做完**，不在
  pydantic validator 裡：validator 跑在 event loop 上，而遮蔽會掃整個 tools 目錄。檔案大小上下限**兩邊對齊**（`_AI_META_MAX_BYTES`，
  256 KiB）：寫得進去的一定讀得回來，不會出現「寫入回報成功、之後每次讀都變成
  沒有總結」；讀取端連 JSON 巢狀過深的 `RecursionError` 都吞成「沒有 sidecar」，
  因為列表頁每一列都會讀它，一個壞檔不能拖垮整頁。sidecar 的寫入是**原子的**
  （同目錄暫存檔 → fsync → `os.replace`，比照 `cli.py` 寫 `.env` 的做法）：磁碟滿、
  配額用盡、I/O 錯誤時舊檔**一位元組都不會動**，也不會被讀到寫到一半的樣子。
  這個檔案允許你手動編輯，所以讀寫**兩端**都會把落單的 Unicode surrogate
  （`"\ud800"` 是合法 JSON，但不能編成 UTF-8）換成 U+FFFD——否則它會在
  GET 回應序列化時炸成 500。
  總結有 `draft`／`final`（定版）兩種狀態；定版後 `regenerate` 一律
  `409 tool_finalized`，要先解除定版——而且**定版後的總結永遠不會被覆寫**，連
  在 LLM 產生期間才被定版的那一次也會在寫檔前放棄（「定版」與「寫入總結」兩段
  read-modify-write 由 `tools._META_LOCK` 互斥，否則兩邊各讀到 `draft` 就會把定版
  蓋掉）。餵給模型的內容排除 `.env` 的**值**（只給 key 名）與 sidecar 自己，
  並且**先放套件本身（`tool.json`、實作檔）、後放安裝脈絡**：提示總量受
  `LLM_PROMPT_BUDGET_TOKENS` 限制，這個順序讓截斷先吃背景資訊，模型不會在小預算
  下拿到零份套件內容然後憑空編造說明。
- **依意見修訂既有工具**（`run_revise`，設計依據見 `docs/web-v4-decisions.md`
  D40）：`POST /api/tools/{name}/revise` 帶一段意見，後端開一個**和安裝同一種**
  的建造者 session（同樣的 meta-tools、同樣的 `tool_install` 工作流程名與預算），
  差別在於暫存工作區是從**既有套件複製**來的，最後以 **replace 模式**換掉正式
  目錄裡的那一包。三件事值得知道：
  - 複製時**排除套件根層的 `.env`**（後端保留名域——AI 總結 sidecar 與
    `.state.json`——則在任何層級都排除；`.state.json` 由換裝前的重新寫入補回，見
    「工具套件格式」）。根層
    `.env` 一定要排除：它的值就在「已知秘密」集合裡，複製進去必然被安裝驗證的
    「檔案不得內嵌秘密值」擋下，等於這個工具再也修訂不了。它在驗證通過之後、換裝
    之前**以 `shutil.copy2` 從正式套件逐位元組複製進暫存區**——不解碼、不重新編碼，
    位元組連同權限與 mtime 原樣過去（工具的 entry 是以套件目錄當工作目錄執行的，
    大可自己用二進位模式讀或雜湊這個檔案，所以它的位元組不是我們可以順手正規化的
    東西）。模型自己寫的 `.env` 一律被覆蓋；系統提示已明說「`.env` 由後端保管、
    不要重寫、也不要依賴改寫它」。**巢狀的 `.env`（例如 `config/.env`）是普通套件
    內容，會照常複製**；若它內嵌了已登記的秘密值，promote 會被內嵌秘密閘擋下並指出
    是哪個檔案——那是閘門在做它的工作，比靜默刪掉一個檔案再驗證通過好。**根層的
    `.ENV` 之類大小寫變體，判準是那個目錄的檔名列表**：列表裡同時有 `.env` 與 `.ENV`
    ＝兩個不同的目錄項（case-sensitive 檔案系統上即使兩者是 hard link 也一樣），變體
    就是普通套件內容，照常複製、照常出貨；列表裡只有變體那一個（case-insensitive
    檔案系統上兩個名字本來就是同一項）、而且 `<套件>/.env` 開出來確實是它時才排除
    （確認用**不跟隨**的 lstat，否則一條指向 `.env` 的 `.ENV` symlink 會被誤判成
    同一個檔案而被靜默刪掉）。要改秘密值請直接手動編輯該工具的 `.env`，但那些手寫的值
    必須是遮蔽器**遮得掉**的，否則修訂在開工前就被拒絕，兩種情形各有自己的訊息與補救：
    **長度至少要 6 字元**（遮蔽器的下限，比照安裝表單；補救是加長或移出 `.env`），
    而且**「決定該值的那一行」的右邊，必須剛好等於這套系統自己寫得出來的三種拼法之一**
    ——裸值、`'值'`、`"值"`（各自在序列化器認定安全時才算數）。像 `KEY="ab'cd\"ef"`
    這種跳脫會真的生效的寫法不算，找不到可對應的賦值行（跨行引號值、續行）也拒絕；
    看的是**那一行**、而且**後出現者勝**（最後一行指派該 key 的賦值）。**這裡是「等於」
    而不是「包含」**：只要是「包含」，旁邊的文字就能替憑證作保——整份檔案裡的一行註解
    可以（r5），**同一行的行尾註解**也可以（r6：`KEY="ab\"cd" # ab"cd` 被 dotenv 拆成
    引號值與註解兩段吃掉，「值出現在右邊」卻成立）。因此**刻意連合法但我們寫不出來的
    寫法一起拒絕**：`KEY=值 # 註解`、`KEY=兩個 詞`、`KEY=a#b` 現在都不過關，補救是把那一行
    寫成最單純的形式（原值直接寫、或整段加引號）並移除行尾註解與多餘跳脫。代價是有界的：
    **凡是這套系統自己寫出來的 `.env` 一定過得了這一關**（安裝會丟掉先前同名的行、把
    `KEY=<序列化結果>` 附在最後，而合格集合就是問同一個序列化器要來的）。理由都一樣：
    遮不掉的值會隨 `run_shell` 的輸出進到提示與 AI 日誌，不做就只剩下外洩一途。修訂
    期間，`.env` 的每個值都會被登記為「進行中的秘密」，因為換裝那一瞬間舊套件被改名成
    隱藏備份目錄，而已知秘密掃描**跳過隱藏目錄**，不補這一手就會有一段沒被遮蔽的空窗；
    這個「讀值」的動作與上面的「複製檔案」是兩件事——讀不到值（`.env` 是 symlink、
    權限錯誤、超過大小上限）就整場拒絕，因為那代表整場 session 的憑證遮蔽是瞎的。
    大小上限**以位元組計**（64 KiB），入口與換裝用的是同一把尺，所以換裝會拒的 `.env`
    不會先燒掉一整場 session。
    **上面這一整套政策，在換裝前會對「即將出貨的那些位元組」再跑一次**：`.env` 可以在
    session 中途出現或被換掉（開工時沒有 `.env` 的套件也一樣），所以複製前會**重讀來源**、
    重驗拼法／長度／大小，不合格就以同一組訊息拒絕換裝——閘門驗的是真的會發佈的東西，
    不只是開工那一刻碰巧在那裡的東西；重讀到的值也會在複製前登記成「進行中的秘密」，
    讓換裝後的總結與 sidecar 有遮蔽可用。**⚠️ 但請注意**：修訂**進行中**由你新建的憑證
    檔，builder 在那一刻仍讀得到，而 AI 日誌是**寫入當下**遮蔽、無法事後回頭補遮
    （run_shell 以本服務權限執行、v1 不做沙箱隔離——見下方「安全立場」）。因此
    **某個套件的 AI 修訂正在進行時，請不要新增或編輯該套件的 `.env`**；要換憑證就等
    修訂結束再改（完整裁決見 repo 根目錄 `裁決紀錄.md` #6）。
  - 修訂**工作**開工前會用嚴格讀取器再確認一次定版狀態：已定版拒絕（`tool_finalized`
    的同一組訊息），**sidecar 讀不出可信狀態**（壞掉、被手改成不是 JSON、讀取被拒）
    也拒絕，用的是換裝前那道閘的同一條訊息（兩種補救分開：修好／移除損壞的 sidecar
    vs. 解除定版）。同一把嚴格尺放在**工作**這一側而不是路由：換裝前那關本來就會拒同
    一種檔案，不先擋掉就是白燒一整場多輪 session、還佔住「一次只跑一個」的名額；路由
    的入口檢查刻意維持寬鬆版本（路由回答的是資源目前**已知**的狀態，猜錯的代價只是
    這個拒絕改以工作結果呈現）。
  - 模型**不能改名**：回報的 `tool_name` 必須等於被修訂的套件名，否則整次修訂作廢
    （`tool_name` 決定的是「要替換哪一包」，不是模型的自由欄位）。
  - 換裝是「舊包改名成隱藏備份 → 新包**以單一 `os.rename` 就位** → 成功刪備份／
    失敗把備份改回來」：驗證不過、原套件中途被刪、就位失敗，**都不會**動到正在
    服役的那一包（`rename` 失敗代表什麼都沒搬，名字仍然空著，備份一定回得去；
    `shutil.move` 會退化成複製，半套目錄佔住名字就連還原都做不到）。修訂成功之後
    同樣會 best-effort 重新產生總結（狀態回到 `draft`，並沿用原本 sidecar 記下的
    安裝出處）。
  修訂與安裝共用同一張工作表，**同一時間只允許一個工具工作**（任何 queued／
  running 的工作都會擋下新的），輪詢端點也是同一個；**同步的「重新產生總結」
  也佔同一個名額**（見上面該路由），所以三者彼此互斥。輪詢不到工作時的 404 訊息
  是中性的 `Tool job not found`（同一個端點服務安裝與修訂兩種工作）。
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
  （`_inject_secret_into_env`，在 `validate_package` 之後、搬移之前）。值不進任何
  提示，模型只拿得到**名字**。
- **`.staging` 殘留**：清理是 best-effort（所有清理例外都被抑制），正常結束
  時通常會清掉暫存目錄；但後端在建置途中被中斷（當掉、被砍、主機重開機），
  **或清理本身失敗**（例如 `run_shell` 改了目錄權限）時，會留下
  `<TOOLS_DIR>/.staging/<uuid>`。這個目錄名稱以 `.` 開頭，registry 掃描
  （`tools._scan_all`）會直接跳過隱藏目錄，不會被列成無效工具，可以安全地手動
  刪除。
- **同一時間只允許一個工具任務**：`start_install_job` 在同一把鎖底下檢查「名額是否
  被佔用」並建立新工作；被佔用時，第二個 submit 回 `409 install_in_progress`。
  **名額由三種動作共用**：安裝工作、AI 修訂工作（同一張 `_JOBS` 表）、以及同步的
  「重新產生總結」（`_SYNC_OPS` 的一個 token，在同一把鎖底下「檢查即取得」，見
  上方該路由）——所以擋下這次送出的，未必是另一個安裝。工作狀態是 in-memory、
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
