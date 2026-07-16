# e2e 煙霧測試（smoke harness）

本目錄有兩套獨立的端對端煙霧測試：`smoke.sh`（**dev 模式**，見下方
「`smoke.sh`（dev 模式全端煙霧測試）」一節）與 `wheel_smoke.sh`（**打包／
`uvx` 模式**，見文末「`wheel_smoke.sh`（打包／`uvx` 模式端對端煙霧測試）」
一節）。兩者互補、覆蓋範圍不同——`smoke.sh` 從原始碼跑 `uv run uvicorn`，從未
碰過打包（wheel 內容、`context-memory` console script、SPA 靜態檔服務、
`cli.py` 的 data-dir/`.env` 邏輯）；`wheel_smoke.sh` 才是真正跑過實際安裝
產物的那一個。

## `smoke.sh`（dev 模式全端煙霧測試）

針對 Context Memory 全端（FastAPI 後端 + Vite SPA 前端）的端到端煙霧測試。
它會啟動**真實**的後端與一個純標準函式庫的 OpenAI 相容 mock 伺服器，直接打真實
HTTP API，並在最後驗證前端 production build 能正常提供 SPA。

### 前置需求

- `uv`（啟動後端：`uv run uvicorn`）
- `pnpm`（前端 `build` / `preview`；Phase D 執行前會自動跑 `pnpm install --frozen-lockfile`，不需手動預先安裝前端相依套件）
- `python3`（mock 伺服器為純標準函式庫；腳本也用它做 JSON 斷言）

### 執行方式

從 **repo 根目錄**執行：

```bash
./e2e/smoke.sh
```

每一項斷言會印出 `PASS` / `FAIL`；只要有任何一項失敗，腳本即以非零狀態結束。
所有伺服器與暫存 SQLite 檔都放在暫存目錄，並透過 `trap` 在結束時一律清除
（關閉行程群組、釋放埠號、刪除暫存目錄）。埠號皆為動態取得，暫存 DB 一個階段一份，
彼此隔離。

### 各階段涵蓋範圍

- **Phase A — 未設定 LLM 的後端**：`/api/health`、`/api/llm/status`
  （`configured:false`）、未設定時 `capture` 回 503 `llm_not_configured`；接著完整
  CRUD 生命週期（建立 → 列表 total → 取單筆並含初始 progress → PATCH 狀態並確認
  `updated` 前進 → 追加 progress → review 分組 → 中文/emoji 標題原樣往返 → 刪除 →
  404）；以及輸入上限拒絕（標題 > 300、tags > 20 皆回 422）。
- **Phase B — 後端 + good mock**：真實 `capture`（raw_text 中文 → 201，項目寫入
  LLM 提供的標題，questions 以 `- ` 條列寫進 `open_questions`）；真實 `enrich`
  （區段合併、回傳 gaps、依 `checklist_complete` 推進 stage/status）；真實
  `assist-update`（追加 progress、`updated` 前進）；以及 supersede 檢查（改寫
  decisions 時，舊內容須保留在 `superseded` 標記之下）。
- **Phase C — 降級**：mock `garbage` 模式 → `capture` 回 502 `llm_upstream_error`
  且不寫入任何列；mock `slow` 模式搭配極小 `OPENAI_TIMEOUT_SECONDS` → 於逾時上限內
  回 502 `Timeout`，同樣不寫入任何列。
- **Phase D — 前端 build**：`pnpm build` 後 `pnpm preview` 啟動，`GET /` 與
  `GET /items`（SPA history fallback）皆回 200 的 HTML SPA 外殼，最後關閉。

### Mock 伺服器（`e2e/mock_llm.py`）

純標準函式庫的 OpenAI 相容 mock。它接受 `POST <base>/chat/completions`，讀取
**system** 訊息以判斷工作流程（capture／enrich／assist-update，依三段 system prompt
的特徵字串分流），並回傳對應模型的合法單一 JSON 物件（真實的中文內容）；無法辨識的
system prompt 則刻意回傳非 JSON 的散文（觸發 502）。enrich／update 會另外掃描
**user** 訊息中的 `#variant:<名稱>` 指示字，讓同一個 mock 能在連續呼叫回傳不同的
罐頭結果（煙霧測試用來驅動「先 seed、後 complete 並 supersede」的補充流程）。

旗標：

- `--port`：監聽埠號（預設 8900）。
- `--mode good|garbage|slow`：`good` 回合法 JSON；`garbage` 一律回散文垃圾（測 502）；
  `slow` 先睡 `--slow-seconds` 秒再回覆（測逾時降級）。
- `--slow-seconds`：`slow` 模式的延遲秒數（預設 10）。
- `--host`：綁定位址（預設 `127.0.0.1`）。

每一筆請求都會輸出一行日誌到 stderr。

### 已知限制：`pnpm preview` 與 `/api` 代理

在本 repo 的 vite 8 設定下，`preview` 伺服器會**沿用** `vite.config.js` 的
`server.proxy`，因此會把 `/api/*` 轉發到**寫死的** `http://localhost:8000`，而不是本
腳本動態啟動的後端。無論如何，preview 都打不到我們的後端，所以全端煙霧測試在
Phase A–C 直接打**後端** API，Phase D 只驗證 build 出來的 SPA 外殼能被提供，不透過
preview 走任何 API 呼叫。

## `wheel_smoke.sh`（打包／`uvx` 模式端對端煙霧測試）

`smoke.sh` 從原始碼跑 `uv run uvicorn`，從未真正碰過打包這件事本身。
`wheel_smoke.sh` 補上這一塊：它會先用 `scripts/build-wheel.sh` 建出真正的
wheel，再用 `uvx --from <wheel> context-memory` 啟動——跟一般使用者照著根目錄
README 安裝的方式完全一樣——然後用 `curl` 打這個打包後的實例，是唯一真正跑過
「安裝產物」本身（wheel 內容、`context-memory` console script、SPA 靜態檔
服務、`cli.py` 的 data-dir/`.env` 邏輯）的測試。

### 前置需求

- `uv`（提供 `uvx`）
- `pnpm`（前端 build，`scripts/build-wheel.sh` 會呼叫）
- 在**同一台機器第一次**執行時需要網路：`uvx` 要把 wheel 的依賴解析、下載進它
  自己管理的 tool venv；uv 套件快取是熱的（例如剛在 `backend/` 跑過
  `uv sync`）就會很快，且不需要網路，因為版本都對得上 `uv.lock`。

### 執行方式

從 **repo 根目錄**執行：

```bash
bash e2e/wheel_smoke.sh
```

同樣是每項斷言印 `PASS`/`FAIL`，任何一項失敗就以非零狀態結束。伺服器與暫存
目錄（wheel 的建置輸出、一個獨立的 `--data-dir`）都在 `EXIT` trap 裡清乾淨；
埠號用 `--port 0` 讓 uvicorn 自己選，再從啟動 log 解析出來，避免「先偵測空
埠、後綁定」中間的 TOCTOU 空窗。

### 涵蓋範圍

- **建置**：實際跑一次 `scripts/build-wheel.sh`，失敗就整個 fail-fast。
- **啟動**：`uvx --from <wheel> context-memory --host 127.0.0.1 --port 0
  --data-dir <tmp>`；`OPENAI_*` 三個環境變數強制清空，確保
  `configured:false` 的斷言不受執行環境影響。
- **`.env` 載入 + 相對路徑錨定**：在啟動前於 `--data-dir` 裡預先寫入
  一份帶**相對路徑** `DATABASE_URL` 的 `.env`；之後驗證資料庫檔案真的落在
  `--data-dir` 底下（而不是預設檔名、也不是啟動時的 CWD），證明
  `cli.py` 的 `<data-dir>/.env` 載入與「先 chdir 進 data dir 再解析相對路徑」
  兩件事都確實生效。
- **SPA 服務**：`GET /` 回 200 的 SPA 外殼（`text/html`、`Cache-Control:
  no-cache`）；`GET /items/123` 這種深連結一樣回 200 的 SPA 外殼（client-side
  routing 的 fallback）；從 `index.html` 抽出的一個真實 `/assets/*.js` 回
  `Cache-Control: public, max-age=31536000, immutable`。
- **`/api` 命名空間不對 HTML 內容協商**：`GET /api/nonexistent` 無論帶
  `Accept: application/json` 還是像裸 `curl` 那樣預設 `Accept: */*`，都回 404
  JSON（不是 200 的 SPA 外殼）；沒有結尾斜線的 `GET /api` 本身也回 404
  JSON；對一個真實端點用錯 method（`DELETE /api/health`）仍正確回 405，而不是
  被誤判成 404。
- **AI 未設定**：`GET /api/llm/status` 回 `configured:false`。
- **CRUD 落地位置**：`POST /api/items` 成功建立項目，並確認資料庫檔案的位置
  （見上方「`.env` 載入 + 相對路徑錨定」）。

`smoke.sh` 與 `wheel_smoke.sh` 的行程/暫存清理都遵循同一套 hygiene：
`setsid` 起一個獨立 process group，`EXIT` trap 對整個群組送信號，不管中間
`uvx`/`uvicorn` 疊了幾層 subprocess 都會一起回收。
