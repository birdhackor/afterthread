# Web App v2 決策紀錄

格式：每個決策一節——背景 → 選項與優缺點 → 決定 → 依據。供事後檢查。
本輪原則（使用者指示）：平衡／保守／簡潔／易擴充；選錯會導致大改的才停下來問。

## D01：context7 不可用，改用 WebSearch/WebFetch 查官方文件

- **背景**：使用者指示用 context7 查 FastAPI 打包相關資訊，但本 session 沒有掛載 context7 MCP（ToolSearch 查無）。
- **決定**：改用 WebSearch/WebFetch 直接讀官方文件（fastapi.tiangolo.com、hatch.pypa.io、docs.astral.sh/uv），並以本機 venv 實測驗證 API 簽名。
- **依據**：資訊需求相同、來源等級相同（官方文件）；實測比文件更可靠。

## D02：wheel 內嵌前端 dist 的方式 — 複製進 package + `artifacts`

- **背景**：wheel 要含 build 好的 FE dist。三個選項。
- **選項**：
  1. **hatch `force-include` 指向 `../frontend/dist`**：不必複製。缺點：實測 `uv build` 走 sdist→wheel 兩段建置時，wheel 階段在解開的 sdist 裡找不到 `../frontend/dist`，直接 `FileNotFoundError`；sdist 永遠是壞的。
  2. **build hook／第三方插件（hatch-build-scripts、hatch-js）自動跑 pnpm build**：一鍵建置、git 直裝可用。缺點：建置機還是要有 Node（沒省環境）、第三方插件維護風險、隔離建置環境內呼叫 pnpm 失敗訊息難除錯。
  3. **pre-build step：先 pnpm build、複製 dist → `backend/context_memory/static/`（gitignored），`artifacts` 把它收進 wheel/sdist**：透明、失敗點清楚、hatch 官方 `artifacts` 就是為 gitignored 產物設計；缺點：多一個腳本步驟，忘跑會包到舊 static（腳本內加新鮮度檢查防呆）。
- **決定**：選項 3，配 `scripts/build-wheel.sh` 一鍵完成。
- **依據**：研究代理對選項 1 的失敗與選項 3 的成功都做過本機實測；符合保守/簡潔原則。代價：git+subdirectory 直裝拿不到前端（僅內部 wheel 發布，可接受，README 註明）。

## D03：SPA 服務方式 — FastAPI 0.138+ 官方 `app.frontend()`

- **選項**：
  1. **`app.frontend("/", directory=..., fallback="index.html")`**（FastAPI 0.138 新增，lock 已鎖 0.139.0）：官方一等 API；fallback 只套用於「瀏覽器導航式」GET/HEAD，缺失的 asset 仍 404、已註冊 API route 優先——正是 SPA 需要的語意。缺點：API 很新（2026-06），細節文件薄。
  2. **傳統 `StaticFiles(html=True)` + 自寫 404 fallback**：古老穩定。缺點：手寫 fallback 易錯（吃掉 /api 404、深層 asset 404 回 HTML 造成 MIME 白畫面），程式碼多。
- **決定**：選項 1，並在 `e2e/wheel_smoke.sh` 加上「`/api/nonexistent` 必須回 404 JSON、深層路由回 index.html」的斷言補平新 API 的不確定性。
- **依據**：venv 實測簽名確認可用；用 e2e 斷言覆蓋「太新」的風險，比自寫 fallback 重新發明更保守。

## D04：backend 套件改名 `app` → `context_memory`

- **背景**：wheel 發布 top-level 套件叫 `app` 必然撞名，不可接受。
- **選項**：不改名直接發（uvx 隔離環境內風險低但髒）；巢狀包一層（import 一樣要全改）；直接改名（機械替換約 65 處 import）。
- **決定**：直接改名，且獨立成一個純改名 commit（與打包 commit 分開，方便 review）。
- **依據**：機械性高、測試會抓破綻、一次到位不留技債。

## D05：資料目錄 — cli.py 注入 XDG 預設，不動 config.py 預設值

- **背景**：`database_url` 預設 CWD 相對；uvx 從任何目錄啟動，DB 會散落各處（像資料遺失）。
- **選項**：
  1. **config.py 改預設到 XDG**：影響 dev 模式與既有測試，行為全域改變。
  2. **加 `platformdirs` 依賴**：跨平台正確，但多一個依賴。
  3. **cli.py（只有打包入口走它）解析 `--data-dir`/`CONTEXT_MEMORY_DATA_DIR`，預設 `$XDG_DATA_HOME|~/.local/share`/context-memory；`DATABASE_URL` 未設才注入 `<data-dir>/context_memory.db`；dev 直接跑 uvicorn 不經 cli.py，行為不變**。
- **決定**：選項 3，手寫 XDG（Linux/macOS 夠用，不加依賴）。
- **依據**：關注點分離——「打包模式的預設值」屬於打包入口的職責；dev/test 零影響；顯式 `DATABASE_URL` 永遠優先。

## D06：.env 解析 — config 改 CWD 相對；cli.py 額外載入 `<data-dir>/.env`

- **背景**：`_ENV_FILE = Path(__file__)...parent.parent/".env"` 裝進 wheel 後指向 site-packages/.env，永遠讀不到使用者設定。
- **決定**：`config.py` 的 env_file 改成 CWD 相對 `".env"`（dev 從 `backend/` 啟動時等同現行為）；`cli.py` 啟動時先把 `<data-dir>/.env` 載入環境（不覆蓋已存在的環境變數）。
- **依據**：環境變數優先權不變（env > .env）；使用者只需記得「設定放 data dir 的 .env」一條規則。

## D07：item 1 修法 — 被動回報 + 主動輪詢混合，引入 vitest

- **背景**：後端 badge 一次性 local state、AI badge loaded 後 no-op，雙向凍結（盤點確認兩個獨立機制都壞）。
- **選項**：
  1. 只加 setInterval 輪詢：夠簡單，但使用者操作失敗（如提交表單斷線）與 badge 更新之間有最長 30 秒空窗。
  2. 引入 TanStack Query（refetchInterval/refetchOnWindowFocus）：機制現成，但整個 repo 是手寫 fetch + requestId + jotai 的既定模式，為一個 badge 引入整套資料層違反倉庫慣例。
  3. **被動（client.js 每次請求結果回報 connectivity atom；拿到任何 HTTP 回應=up、network error=down）+ 主動（30 秒輪詢 `/api/health` + focus/online/visibilitychange 立即 ping）**：即時性與覆蓋率兼得，符合既有模式。
- **決定**：選項 3。連帶決策：reachable false→true 時自動 `loadLlmStatus({force:true})`（尊重 generation counter）；vitest 只測純邏輯（atom 轉換、client 回報），不引入 jsdom。
- **依據**：修「類」不修「例」——connectivity 是 app 層級狀態，任何頁面都受益；vitest 為後續 phase 建立 FE 測試地基。

## D08：BE 不可達時 AI badge 顯示「無法確認」，不改 `configured`

- **背景**：atom 語意刻意在 transport 失敗時保留舊 `configured`（避免斷線把「已知未設定」洗回樂觀 true）。
- **決定**：顯示層在 `reachable === false` 時讓 AI badge 呈灰色「無法確認」；atom 資料不動。
- **依據**：斷線時 LLM 狀態本來就是未知，顯示「無法確認」是誠實的；保留 atom 語意避免翻攪既有 race 防護。

## D09：LLM logging 設計 — in-memory ring + 可選 JSONL + INFO 摘要

- **背景**：backend 目前零 logging；既有硬性不變量：base_url/api_key 絕不得出現在 log（有 caplog 測試釘住）。
- **選項**：只寫檔（FE 看不到）；只做 FE 顯示（重啟就沒了、BE console 沒得看）；上 structlog/loguru（多依賴）。
- **決定**：三層——(1) in-memory ring（預設 50 筆，含每個 attempt 的完整 prompt/response 與 usage tokens）+ `GET /api/llm/logs`（摘要）/`GET /api/llm/logs/{id}`（全文）+ FE「AI 日誌」頁；(2) 可選 `LLM_LOG_FILE` JSONL 追加（預設關，記錄含個人內容，落磁碟應為 opt-in）；(3) stdlib logging INFO 一行摘要（workflow/結局/attempt 數/耗時/tokens，無內文無設定值）。
- **依據**：stdlib logging 零依賴；ring+API 讓 FE/BE 都能除錯；預設不落地尊重內容隱私；摘要行絕不含內文與設定值，caplog 防洩漏測試自然續過。

## D10：GLM5.2 調整範圍 — 只動 prompt budget、timeout、max_output_tokens

- **背景**：config 註解自述為 small-context model 防護：budget 預設 32k、上限 200k chars；timeout 60s/上限 600；不送 max_tokens；一切 char-based 無 tokenizer。
- **決定**：
  - `llm_prompt_budget_chars`：上限 `le=200000` → `le=2_000_000`，預設 32000 → 200000。
  - `openai_timeout_seconds`：預設 60 → 120，上限 600 → 1800。
  - 新增 `openai_max_output_tokens: int | None = None`（None＝不送參數，維持現行為）。
  - **不動** per-section 20k／history 60k 儲存上限與 FE 鏡像；**不引入** tokenizer。
- **依據**：item 內容上限（≤19 節 × 20k + history 60k）使實務最大序列化 ≈ 50 萬 chars，200k 預設已能讓絕大多數 item 整項進 prompt 而不截斷；對 1M-token 模型即使 CJK ≈ 1 char/token 也綽綽有餘。儲存上限是「內容尺寸」決策而非 context 限制，動它會翻攪 FE/BE 鏡像與 merge 邏輯，收益近零。tokenizer 依賴（GLM 分詞器）不穩定，char-based 保守夠用。

## D11：tool-calling — 擴充 `generate_structured` 而非新函式

- **選項**：新函式 `generate_with_tools`（兩套維護）；改造成 chat/agent 迴圈（超出需求）；**在 `generate_structured` 加選用 `tools` 參數**。
- **決定**：加選用參數。`tools=None` 時行為 byte-identical（既有測試不動）；有 tools 時：assistant 回 `tool_calls` → 執行 async handler → 附加結果 → 續跑，上限 `llm_tool_rounds_max`（預設 4）；最終回覆仍走 strict JSON + corrective retry；共用同一個 asyncio.timeout 總預算（KB 開啟時建議調高 timeout，文件註明）。
- **依據**：單一 LLM 邊界是這個 codebase 的核心設計，維持它最簡潔；`_extract_content` 目前把 content=None（tool_calls 形狀）誤判為 502，此修正屬於同一件事。

## D12：KB 連接器 — 通用 HTTP 模板 + 單一適配點，預設關閉

- **背景**：公司 KB 有 API 但格式未知；需求是「基礎做好 + 指南」。
- **選項**：做死某假想格式（一定要重寫）；做成 plugin/entry-point 系統（過度設計）；**通用 REST 模板：`POST {kb_base_url}{kb_search_path}`，body `{"query", "top_k"}`，回應解析集中在單一 `_parse_response()`，指南教使用者改這一個函式對接真實格式**。
- **決定**：通用模板 + 單一適配點。`kb_base_url` 未設＝功能完全關閉，三個工作流 prompt byte-identical（smoke、prompt 測試、mock_llm 標記路由全部不受影響）。**KB 呼叫失敗一律降級為空結果 + log，絕不讓工作流 502**。kb_api_key/base_url 進入與 LLM 相同的不洩漏紀律。
- **依據**：在「格式未知」前提下，唯一不會白做的形狀就是把未知集中在一個函式；降級策略保證 KB 故障不影響主功能（保守）。

## D13：codex review 基準 — 累積 diff 對 origin/feat/web-app

- **背景**：使用者指示「codex review (compare to origin)」；工作期間不 push，origin 不動。
- **決定**：每個 phase 完成後 review `origin/feat/web-app...HEAD` 累積 diff；已裁決的 won't-fix 記錄於本文件（D17 起追加），並在後續 review prompt 中告知，避免重複打轉。收尾再做一次全量 review。
- **依據**：忠於指示；累積 review 能抓跨 phase 交互問題；won't-fix 清單防止迴圈。

## D14：phase 4 的 e2e 覆蓋 — pytest 為主，mock_llm 加 tool_calls 變體，smoke 不強制加 KB 場景

- **背景**：smoke.sh 以精確字串斷言 + mock_llm 靠 prompt 標記路由，非常脆；KB 預設關閉時 e2e 已涵蓋「不退化」。
- **決定**：tool 迴圈與 KB adapter 以 pytest（stub client / httpx mock）完整覆蓋；mock_llm.py 加 tool_calls 變體能力（供手動/未來使用）；不在 smoke.sh 強制加 KB 場景（除非 phase 4 實作後評估風險值得）。
- **依據**：脆弱 e2e 的維護成本高於其增量信心；pytest 對迴圈邏輯的覆蓋更精確。

## D15：版號維持 0.1.0

- **依據**：uvx 對本地 wheel 以檔案 mtime 做快取鍵（實測同版號重建即生效），內部 wheel 發布不需要版號驅動升級；等使用者決定發布節奏再 bump。

## D16：AGENTS.md 於 Phase 5 修正；CLAIDE.md 不動

- **背景**：AGENTS.md:18「file-based MVP 驗證前不得 app 化」與本輪方向牴觸；根目錄另有未追蹤的 `CLAIDE.md`（內容同源，疑似 `CLAUDE.md` 的 typo）。
- **決定**：AGENTS.md 在 Phase 5 隨 README 一起改寫成與現況一致；`CLAIDE.md` 是使用者個人未追蹤檔案，不代動，於總結報告中提醒（含檔名疑似 typo）。
- **依據**：tracked 專案文件屬於本輪範圍；使用者私人檔案未經要求不碰。

---

（以下隨各 phase 進行追加：won't-fix 裁決、實作中的新決策）

## D17（Phase 1 實作中）：被動訊號限縮為「無歧義證據」，health probe 語意判定成為雙向權威

- **背景**：D07 初版規則「任何 HTTP 回應＝可達」在 dev 模式有假陽性——後端死掉時 vite proxy 自己回 500，fetch 有 resolve，badge 會誤報「連線正常」。使用者回報 bug 的場景正是 dev 模式；且「一開始就斷線」情境下新碼比舊碼（檢查 `data.status === "ok"`）更糟，屬退步，必修。
- **選項**：
  1. 嗅探 proxy 錯誤回應的特徵（content-type/body 文字）：脆弱，綁 vite 實作細節。
  2. 只修 health probe、被動規則不動：真後端 LLM 502/503 與 proxy 500 在被動路徑仍不可分，錯誤分類殘留。
  3. **被動 up 只認 status < 500（<500 證明我們的應用真的處理了請求）；5xx 不表態（可能出自替死 upstream 代答的中介層）；/api/health 由 probe 語意判定（body.status === "ok" → up，其餘一切 → down），雙向權威；probe 加 generation counter 防兩個 in-flight 亂序覆蓋**。
- **決定**：選項 3。
- **依據**：以「證據強度」分類訊號而非以來源特判，dev（proxy 500→down）與生產（同源、斷線是真 network error）兩種部署都正確；trivial 的 /api/health 對正常後端永遠 ok，對它的 5xx 必然代表「使用者視角的無法連線」。代價：真後端 5xx 不再被動報 up——不表態不等於報 down，poller 每 30 秒會校正，可接受。

## D18（Phase 1 codex review 裁決）：三項發現全修 + 兩項 won't-fix

- **背景**：第一輪 codex review 判定 Safe with caveats：Medium ×2、Low ×1、測試盲點若干。
- **裁決（修）**：
  1. **force 穿透 in-flight**（Medium）：`loadLlmStatusAtom` 原本 `loading` 中丟棄一切呼叫，恢復觸發的 force 會啞火且再無 false→true 轉換可補救——自動恢復承諾破功，必修。修法用既有 generation 機制轉移所有權，不新增狀態。
  2. **被動 up 證據升級 + probe 繞過被動層**（Medium）：headers 先到、body 才失敗（或語意不符）的單一請求會發出 up→down 矛盾判定，reachable=false 時 up flash 會誤觸 LLM 恢復重載。修法：up 改為「body 成功交付」才算證據；probe 流量 `reportConnectivity:false` 完全繞過被動層，「語意判定唯一權威」從結構上成立（消滅類，非縮小例）。
  3. **probe timeout**（Low）：`AbortSignal.timeout(10s)`——掛死 server 下 pending probe 以每 30 秒一個累積，會吃滿瀏覽器同源連線上限，屬無上界資源累積類，值得 10 行修掉。
- **裁決（won't-fix）**：
  - hook 生命週期單元測試：需引入 jsdom/React 測試環境，D07 已刻意界定 vitest 只測純邏輯；hook 行為由 code review + StrictMode 手動追蹤覆蓋。
  - 主動/被動整合競態測試：probe 繞過被動層後，兩層再無交互寫入，單元層分別覆蓋已足。

## D19（Phase 1 codex review 第二輪裁決）：status 請求加 timeout；主動取消不採用

- **背景**：第二輪 review 確認三項修正無誤，新增 Low ×2：(1) force 穿透後被 supersede 的 `/api/llm/status` 請求無 timeout、不取消，極端 flapping + 掛死 endpoint 下 pending 無上界累積（與剛修的 probe 同類）；(2) plan 文件仍描述被 D17/D18 推翻的初版設計。
- **選項（針對 1）**：AbortController 主動取消被 supersede 的請求（取消會走 network-error 路徑，需防被動層把「刻意取消」誤報成 backend down，複雜）；**只加 timeout（與 health probe 共用 `PROBE_TIMEOUT_MS = 10s`，常數移到 client.js 共享）**——不取消但每個請求生命有上界，累積自然排空。
- **決定**：timeout 方案；status 請求維持被動回報（它是真實證據；timeout 造成的 down 會被 30 秒內的權威 probe 校正）。plan 文件 Phase 1 節改寫為最終設計並標注 D17/D18 修訂。
- **依據**：一致性（probe 同款 deadline）、簡潔（無取消協調）、無誤報疑慮。trivial endpoint 十秒答不出來，讀作 down 在語意上誠實。
