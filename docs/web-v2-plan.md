# Web App v2 改造計畫（2026-07-15）

對應需求（原始六項）：

1. 總覽頁下方 BE/AI 狀態不會隨後端斷線/恢復更新 → **Phase 1**
2. README 改寫為網頁版優先、CLI 補充 → **Phase 5**
3. BE 打包成 wheel、內嵌 build 好的 FE dist、`uvx` 一鍵啟動 → **Phase 2**
4. AI 目前只有單純呼叫，需要 tool-calling 基礎 + 內部知識庫（KB）串接架構與指南 → **Phase 4 + Phase 5（指南）**
5. LLM 互動需要大量 log 方便除錯（FE 顯示 + BE 紀錄） → **Phase 3**
6. 內部 LLM 為 GLM5.2（1M context），檢視需調整的組態假設 → **Phase 3**

決策細節與依據見 `docs/web-v2-decisions.md`（編號 D01–D16，隨進度追加）。

## 工作流程（每個 phase 相同）

1. 主代理擬 spec → 派 subagent 實作。
2. 主代理親自看 diff + 跑 gates（backend：`ruff check`／`ty check`／`pytest`；frontend：`biome lint`／`vite build`／（Phase 1 起）`vitest`；必要時 e2e smoke）。
3. 自我審查修正（修「類」不修「例」，做 collateral 分析）。
4. Commit。
5. codex review（比對 origin/feat/web-app 的累積 diff）→ 合理意見就修、修完再 review，直到無新問題或僅剩已裁決的 won't-fix。
6. 進下一個 phase。

## Phase 1：BE 連線／LLM 狀態偵測修復

**根因**（盤點確認）：

- 後端 badge：`HomePage.jsx` `StatusFooter` 只在 mount 時打一次 `/api/health`（空依賴 `useEffect`），此後永不更新，也沒有重試按鈕 → 雙向凍結。
- AI badge：`llmStatusAtom` 只在 RootLayout mount 載一次，`loaded` 後除 `force` 外一律 no-op；斷線時 transport error 刻意保留舊 `configured`，只設 `error`。

**設計**（初版；實作中經 D17/D18 修訂，以下為最終版）：

- 新增 `atoms/connectivity.js`：`backendStatusAtom`（`reachable: null|bool`）+ 回報用 write atoms。此模組不 import client（避免循環依賴）。
- `api/client.js` 被動層——只回報**無歧義證據**：完整交付 body 的 <500 回應（含 204）→ up；transport 失敗 → down；**5xx 不表態**（dev 的 vite proxy 會替死掉的後端回 500，真後端也會為 LLM 錯誤回 502/503）。新增 `reportConnectivity` 選項供 probe 流量退出被動層。透過 jotai `getDefaultStore()` 寫入（app 未用 Provider）。
- `api/health.js` 主動層——`probeBackendHealth()` 是**雙向權威**：`body.status === "ok"` → up，其餘一切（任何 ApiError、形狀不對的 body）→ down；generation counter 防亂序；`reportConnectivity:false` + `AbortSignal.timeout(10s)`（掛死的 server 不得累積 pending probe）。
- 新 hook（掛在 RootLayout）：每 30 秒 probe 一次 + `visibilitychange`/`focus`/`online` 事件立即 probe。StrictMode 雙 mount 安全。
- reachable 從 false→true 的轉換：自動 `loadLlmStatus({ force: true })`，讓 AI badge 一併恢復；force 可 supersede in-flight 請求（generation 轉移所有權），status 請求同樣帶 10s timeout 讓被 supersede 的請求有生命上界。
- `StatusFooter`：後端 badge 改讀共用 atom；BE 不可達時 AI badge 顯示灰色「無法確認」（不動 `configured`）。
- 引入 vitest（devDep）：測 connectivity atom 轉換、client.js 被動規則、probe 語意判定、llm atom 的 force 穿透（不含 jsdom/hook 測試）。建立 FE 單元測試地基供後續 phase 使用。

**驗收**：模擬 BE 起停（e2e 手動或 smoke 內驗證 `/api/health` 行為不變）、vitest 綠、全 gates 綠。

## Phase 2：wheel 打包 + uvx 啟動 + FastAPI 服 SPA

**依據**：研究代理實測（詳 D02/D03/D05）。

- **Commit A（純改名）**：`backend/app/` → `backend/context_memory/`，全部 `from app.` import 機械替換（約 65 處）、tests/conftest、e2e/smoke.sh、README 中 `app.main:app` 同步改。top-level 套件名 `app` 不可發佈。
- **Commit B（打包）**：
  - `pyproject.toml`：name=`context-memory`、hatchling build backend、`[project.scripts] context-memory = "context_memory.cli:main"`、wheel/sdist `artifacts = ["context_memory/static/"]`、移除 `[tool.uv] package = false`、`uvicorn` 明列 dependencies。
  - 前端建置採 pre-build step（`scripts/build-wheel.sh`：pnpm build → 複製 dist → `context_memory/static/` → 驗證 index.html 存在 → `uv build`）；`static/` 進 .gitignore。不用 force-include 指到 `../frontend/dist`（uv build 的 sdist→wheel 流程實測會 FileNotFoundError）。
  - `main.py`：在 routers 之後，`_STATIC_DIR = importlib.resources.files("context_memory") / "static"` 存在才 `app.frontend("/", directory=..., fallback="index.html")`（FastAPI 0.138+ 官方 SPA API，lock 已 0.139.0）；加 Cache-Control middleware（`/assets/*` immutable、HTML no-cache）。dev 模式無 static/ 自動略過。
  - `cli.py`：argparse `--host`（預設 127.0.0.1）/`--port`（8000）/`--data-dir`；data dir 預設 XDG（`~/.local/share/context-memory`）；載入 `<data-dir>/.env`（不覆蓋既有環境變數）；`DATABASE_URL` 未設時指到 `<data-dir>/context_memory.db`；然後 `uvicorn.run("context_memory.main:app", ...)`。
  - `config.py`：`_ENV_FILE` 改為 CWD 相對 `.env`（dev 從 backend/ 啟動行為不變；wheel 安裝後不再指向 site-packages）。
  - 新增 `e2e/wheel_smoke.sh`：build wheel → `uvx --from <wheel> context-memory` → 驗證 `/` 回 index.html、深層路由 fallback、`/api/health` 正常、`/api/nonexistent` 回 404 JSON（驗證 app.frontend 未吃掉 API 404）。
- FE 常數 `LLM_NOT_CONFIGURED_NOTICE` 措辭改為與部署方式無關的說法。

**驗收**：wheel_smoke 通過、原 smoke.sh 通過、全 gates 綠、dev 流程（vite proxy）不變。

## Phase 3：LLM 互動 logging + GLM5.2 組態

**Logging（item 5）**：

- 新 `services/llm_log.py`：每次 LLM 互動一筆結構化紀錄（id、時間、workflow 名、模型、每個 attempt 的 request/response 全文與字數、usage tokens、耗時、結局分類 ok/invalid_output/upstream_error/timeout/not_configured）。**絕不記 base_url/api_key**（沿用既有安全不變量；caplog 防洩漏測試必須續過）。
- Sink 三層：in-memory ring（預設 50 筆，含全文）→ `GET /api/llm/logs`（摘要清單）+ `GET /api/llm/logs/{id}`（全文）；可選 `LLM_LOG_FILE` JSONL 追加檔（預設關）；stdlib logging `context_memory.llm` INFO 一行摘要（無內文、無設定值）→ uvicorn console 即時可見。
- `generate_structured` 內建 recorder（含失敗路徑 finally 收尾）；`memory_ai` 各工作流傳入 workflow 名。
- FE：新路由「AI 日誌」頁（nav 進入），清單 + 展開詳情，手動重新整理；沿用既有 fetch/requestId 模式與 zh-TW 文案。

**GLM5.2（item 6）**：

- `llm_prompt_budget_chars`：上限 `le=200000` → `le=2_000_000`；預設 32000 → 200000（item 各節有 20k/60k 儲存上限，200k 已能整項不截斷；1M-token 模型下 CJK 也安全）。
- `openai_timeout_seconds`：預設 60 → 120、上限 600 → 1800（長 context 生成較慢；仍是「首次+corrective retry」總預算）。
- 新增 `openai_max_output_tokens: int | None`（預設 None＝不送，維持現行為；設定時送 `max_tokens`）。
- 不動 per-section 20k／history 60k 儲存上限與 FE 鏡像（那是內容尺寸決策，非 context 限制；避免 FE/BE 連動與 merge 邏輯翻攪）。不引入 tokenizer（char-based 保守夠用，文件說明換算）。
- `.env.example` 加 GLM5.2 建議值區塊；config 註解更新（「200k 對任何 context window 都夠」已不成立）。

**驗收**：新舊 pytest 全綠（含 caplog 防洩漏）、logs API + FE 頁可用、smoke 不變。

## Phase 4：tool-calling 基礎 + 內部 KB 架構

- `llm.py`：`generate_structured` 增加選用 `tools` 參數（None 時行為 byte-identical，既有測試不動）。tool 迴圈：assistant 回 `tool_calls`（content=None 不再誤判 502）→ 執行 handler → 附加 tool 結果 → 續跑；上限 `llm_tool_rounds_max`（預設 4）；最終非 tool 回覆走既有 strict JSON 解析＋corrective retry；全程仍在同一個 asyncio.timeout 內。
- 新 `services/kb.py`：`kb_configured()`（比照 `llm_configured` 的 URL 驗證紀律）、`kb_search(query, top_k)`（httpx AsyncClient）。設定：`kb_base_url`／`kb_api_key`／`kb_search_path`（預設 `/search`）／`kb_timeout_seconds`／`kb_top_k`。回應解析集中在單一 `_parse_response()` 適配點（公司 KB API 格式未知，做成模板，指南教改這一個函式）。**KB 失敗降級為空結果 + log，絕不讓工作流 502**；kb_api_key/base_url 不進 log/錯誤訊息。
- 接線：KB 已設定時，三個 AI 工作流掛上 `kb_search` 工具 + 附加一段系統提示（可查內部術語）；**KB 未設定時 prompt byte-identical**（test_ai_prompts 與 mock_llm 標記路由、smoke 全部不受影響）。
- `/api/llm/status` 增 `kb_configured` 欄位；FE footer 增「知識庫」badge。
- Logging（Phase 3 的 recorder）延伸記錄 tool 回合。
- 測試：pytest 覆蓋 tool 迴圈（stub client 回 tool_calls）、KB adapter（httpx mock）、降級行為；mock_llm.py 加 tool_calls 變體支援（e2e 覆蓋度視風險再決定，見 D14）。

**驗收**：KB 關閉時全部既有測試/smoke 原樣通過；KB 開啟路徑有 pytest 覆蓋；全 gates 綠。

## Phase 5：文件（README 網頁優先 + 指南）

- 根 README 改寫（zh-TW）：這是什麼 → 安裝與啟動（`uvx` 主軸、含升級說明）→ 首次設定（data dir、`.env`、GLM5.2 建議值）→ 網頁功能導覽（總覽/捕捉/清單/詳情/AI 功能/AI 日誌）→ 內部 KB 串接指南（改 `_parse_response` 的步驟、驗證方式、用 AI 日誌除錯）→ 開發模式 → CLI 與 OpenCode 補充 → 方法論連結。
- AGENTS.md 修正過時規則（file-based MVP 優先等），與新方向一致。
- backend/frontend/e2e README 同步；重新驗證並更新「已驗證」宣稱。
- 收尾：全分支 diff 最終 codex review + 全 gates + 總結報告。

## 非目標（本輪不做）

- 不新增 chat UI/chat endpoint（item 4 是 tool 基礎與 KB 架構，非聊天室）。
- 不動 per-section 20k 儲存上限、不引入 tokenizer。
- 不上 PyPI／不架私有 index（提供 wheel 檔即可 `uvx --from`）。
- 不做 SSE/WebSocket 即時推播（輪詢已滿足 item 1）。
