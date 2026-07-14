# e2e 煙霧測試（smoke harness）

針對 Context Memory 全端（FastAPI 後端 + Vite SPA 前端）的端到端煙霧測試。
它會啟動**真實**的後端與一個純標準函式庫的 OpenAI 相容 mock 伺服器，直接打真實
HTTP API，並在最後驗證前端 production build 能正常提供 SPA。

## 前置需求

- `uv`（啟動後端：`uv run uvicorn`）
- `pnpm`（前端 `build` / `preview`）
- `python3`（mock 伺服器為純標準函式庫；腳本也用它做 JSON 斷言）

## 執行方式

從 **repo 根目錄**執行：

```bash
./e2e/smoke.sh
```

每一項斷言會印出 `PASS` / `FAIL`；只要有任何一項失敗，腳本即以非零狀態結束。
所有伺服器與暫存 SQLite 檔都放在暫存目錄，並透過 `trap` 在結束時一律清除
（關閉行程群組、釋放埠號、刪除暫存目錄）。埠號皆為動態取得，暫存 DB 一個階段一份，
彼此隔離。

## 各階段涵蓋範圍

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

## Mock 伺服器（`e2e/mock_llm.py`）

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

## 已知限制：`pnpm preview` 與 `/api` 代理

在本 repo 的 vite 8 設定下，`preview` 伺服器會**沿用** `vite.config.js` 的
`server.proxy`，因此會把 `/api/*` 轉發到**寫死的** `http://localhost:8000`，而不是本
腳本動態啟動的後端。無論如何，preview 都打不到我們的後端，所以全端煙霧測試在
Phase A–C 直接打**後端** API，Phase D 只驗證 build 出來的 SPA 外殼能被提供，不透過
preview 走任何 API 呼叫。
