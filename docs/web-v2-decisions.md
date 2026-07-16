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

## D20（使用者指示 2026-07-16）：subagent 依任務難度分配模型

- **背景**：使用者指示善用 haiku/sonnet/opus 節省 token，依難度分配。
- **決定**：主代理（規劃/監督/裁決）維持 fable；實作 subagent 預設 **sonnet**（spec 已寫得極細的機械/標準實作）；invariant 密集或跨檔語意重構（llm.py recorder、tool loop、TanStack 重構、安裝器）用 **opus**；瑣碎掃描/驗證用 **haiku**；codex review 走 codex 不受影響。每次派工時在任務描述記錄所用模型。

## D21（使用者指示 2026-07-16）：KB 連接改為「網頁安裝器 + 產生式工具」架構，取代 D12 靜態模板

- **背景**：使用者要的是像 Claude Code 建 skill 的體驗：網頁安裝頁貼上 KB 的 OpenAPI JSON 網址 + 指示 prompt，系統讓 LLM 自己把可用的工具建起來放到定位。基礎設施需要檔案編輯與 shell 執行能力。**D12 的靜態 HTTP 模板 + 改 `_parse_response` 指南作廢（superseded）**。
- **架構（簡單版 v1）**：
  1. **工具包格式**（skill 的類比）：`<data-dir>/tools/<name>/` 內含 `tool.json`（name、description、參數 JSON Schema、entry 指令）+ 實作檔（如 `run.py`）+ 可選 `.env`（該工具自己的秘密，如 KB API key）。執行契約：runner 以 subprocess 執行 entry，JSON 參數走 argv/stdin，stdout 為結果（有長度上限與 timeout）。
  2. **ToolRegistry**（`services/tools.py`）：啟動/變更時掃描 tools 目錄 → 轉成 OpenAI tools 陣列；執行時 subprocess（cwd=工具目錄、strip OPENAI_* 環境變數、注入工具自身 .env、timeout、stdout cap）。
  3. **安裝器**（`services/tool_builder.py`）：POST /api/tools/install {openapi_url, instructions} → 背景 job。流程：httpx 抓 OpenAPI JSON → 以「工具建造者」system prompt 啟動 LLM tool-loop，給 meta-tools：`write_file`/`read_file`/`list_dir`（限 staging 目錄）與 `run_shell`（cwd=staging、timeout、輸出上限；可用 curl/python 實測 KB API）→ 最終回報結構化 InstallResult → 驗證 tool.json 合法 → staging 搬進 tools 目錄。GET /api/tools/install/{job_id} 輪詢進度；全程 LLM 互動進 Phase 4 的日誌（除錯即看得到）。
  4. **工作流接線**：capture/enrich/assist-update 在有已安裝工具時附掛全部啟用工具 + 一段系統提示；無工具時 prompt byte-identical（既有測試/smoke 不受影響）。
  5. **FE**：「工具」頁（清單/啟停/刪除）+「安裝」頁（URL + 指示 → 提交 → 輪詢進度 → 結果）。
- **安全立場（v1）**：這是使用者本機的個人工具，shell 能力是明確需求（同 Claude Code 性質）；防護做到：staging/tools 目錄為寫入邊界、strip 我方 LLM 憑證、timeout 與輸出上限、README 明示風險。不做容器隔離（過度工程，v1 不值）。
- **依據**：與既有 tool-loop 基礎共用同一套機制（安裝器只是「帶 meta-tools 的一次 tool-loop 呼叫」）；把「KB API 格式未知」問題交給 LLM 在安裝時解決，比靜態模板更貼需求；先簡單版，之後可進版（隔離、重試、多工具編排）。

## D22（使用者指示 2026-07-16）：新增 TanStack Query 重構階段

- **背景**：使用者裁定應善用 lib：優先 TanStack Query useQuery/useMutation，只有不適用的才手寫。
- **決定**：插入獨立重構 phase（在打包之後、logging/安裝器之前，讓之後的新 FE 頁面直接以新模式寫成）。範圍：頁面資料抓取（review/items/item detail）與所有 mutations（CRUD、progress、AI 三動作、之後的安裝器）改 useQuery/useMutation + 失效（invalidation）；requestId 手寫防護由 query key 機制取代。**保留 jotai 的部分（「不適用」清單）**：`connectivity.js`/`health.js`/`llm.js`——app 層級狀態、寫入者是非 React 模組（client.js 被動回報、probe），且是剛硬化並過三輪 review 的 item-1 修復，改寫風險大於收益。QueryClient 預設：retry: false（保留一擊語意 + 連線偵測設計）、refetchOnWindowFocus: true（與連線監測的 focus 語意一致）。
- **依據**：使用者明示偏好；query key/自動 GC 消掉手寫 stale-drop 的重複；jotai 保留範圍有明確技術理由並記錄在案。

## D23（使用者指示 2026-07-16）：push 與 review 政策修訂（取代 D13）

- **決定**：phase 內不 push；**phase 完成（該 phase 範圍 codex review 通過）後 push**。每個 phase 的 codex review **只審該 phase 的 commit 範圍**；全部 phase 完成後，再對**本輪開發起始點**做一次全量 review。
- **本輪起始點**：`4f1ab55`（origin/feat/web-app 於 2026-07-15 開工時的位置）。Phase 0+1（8c1be4c…194a9ca）已依此政策補推送。

## D24（Phase 2 codex review 裁決）：八項發現全修

- **背景**：Phase 2 首輪 codex review 判 needs changes（F1–F6 阻擋、F7/F8 低）。
- **裁決（全修）與方案**：
  - **F1 陌生 CWD .env 汙染 + F3 相對 DB 漂移**：cli.py 於載入設定前 `os.chdir(data_dir)`——把 packaged 模式所有 CWD 相對行為整類錨定到 data dir（選項「條件式停用 env_file」耦合 config 與 cli，棄）。.env.example 的 DATABASE_URL 改為註解掉+說明；wheel_smoke 加「data-dir .env 相對 URL 錨定」斷言。
  - **F2 banner 洩漏 DATABASE_URL**：只印自建的 db 檔路徑；環境提供時僅說 from environment，URL 永不回顯（沿 db.py 遮蔽防線）。
  - **F4 裸 uv build 靜默無前端**：hatch_build.py 驗證 hook（缺 static/index.html 即 fail、editable 豁免保 dev uv sync）；只驗證不建置，D02 保守立場不變。殘餘「舊 static」風險由 build-wheel.sh 的 rm+重拷保證，紀錄為已知界線。
  - **F5 data dir 權限**：僅新建時 chmod 0700；既存目錄不動使用者權限。
  - **F6 404 asset 被 immutable 快取**：`_cache_control_for` 增 status 參數，immutable 僅限 200；html no-cache 不看狀態。
  - **F7 特殊字元路徑**：`_sqlite_url()` 用 `sqlalchemy.URL.create` 構造 + round-trip 測試。實作時發現裁決假設的機制對 `?` 不成立（`render_as_string` 不跳脫、`make_url` 當 query 分隔符），改為「plain 形式 round-trip 驗證，失敗 fallback 到 percent-encoded SQLite URI-filename 形式」，以 engine 層 `pragma_database_list` 實證取代字面斷言（比原裁決更強）。
  - **F8 Accept 防護 false-pass**：unit 層（vitest）釘 fetch headers；wheel_smoke 過時註解修正。
- **第二輪追加（全修）**：HTML no-cache 判斷提到 `/assets/` immutable 之前（extensionless `/assets/missing` 的 SPA fallback 是 shell，被 immutable 會釘一年，原測試釘反了）；`/api/*` 掛 Accept 正規化 middleware（API 命名空間永不協商 HTML；不用 catch-all route——會把真端點的 405 誤變 404，wheel_smoke 同時釘住 bare-curl 404 與 DELETE 405 兩面）；backend README 移除易腐化的精確測試數。
- **第三輪追加（修，免第四輪）**：`startswith("/api/")` 漏掉恰好 `/api`（無斜線）→ 條件補 `path == "/api"`，wheel_smoke 加 `GET /api` bare → 404 JSON 斷言（23 斷言）。單一述詞修正且由 e2e 直接驗證，裁決不再開整輪 review；期末全量 review 覆蓋。另註：第三輪 codex 於其 sandbox 無法執行需網路 socket 的 smoke（環境限制），本機 gates 結果有效。

## D25（Phase 3 codex review 裁決）：五項發現全修

- **背景**：TanStack 重構首輪 review：Medium ×2、Low ×3、無 High。
- **裁決（全修）**：
  1. **刪除後幽靈詳情頁**（M）：`removeQueries` 精確移除 + 404 分支權威化（有舊資料也顯示找不到——404 代表列已不存在）。實作中同類延伸：分頁標題同樣以 404 優先（核可）。
  2. **編輯頁凍結舊快照**（M）：`refetchOnMount:"always"` + 該 query `refetchOnWindowFocus:false`；實作中發現快取會同步先回舊值、表單會在 fresh fetch 落地前就以舊值 mount——補 `formReadyRef` 閂鎖（render 期寫入，效果才來得及擋同一輪的分支），沒有它整個修正是裝飾（核可，屬同根因）。
  3. **非 canonical URL 身分分裂**（L）：cache 身分一律用 route param（`queryItemId` prop）；`item.id` 只用於 server 資源路徑。
  4. **clamp 過渡頁污染 lastGoodRef**（L）：抽共用 `needsPageClamp()` 述詞，兩處共享；placeholder 排除保持獨立 AND。
  5. **AI gate 起始端 race**（L）：恢復同步 bracket（動作 handler 內先報 pending 再 mutate、onSettled 收尾、conflict refresh 同款）；刪除 effect 式回報。
- **殘餘已知界線**：五項修正屬 UI 快取/時序行為，e2e（curl 級）無法直接驗，信心來自 lint/build + 逐場景追蹤；瀏覽器級自動化不在本輪範圍（非目標清單）。
- **第二輪追加（全修）**：(1) 編輯頁凍結契約補完——`refetchOnReconnect:false`、latch 拒絕 errored settle（warm cache + mount fetch 404/失敗時不得掛出舊表單，錯誤分支以 `!formReady` 取代 `item===undefined`、404 全面權威）、**diff 基準改為 `formBaseRef` 快照**（表單只對「使用者看到的那份」一致，reconnect refetch 覆寫 tags 的新 Medium 隨之根治）；(2) lastGoodRef 規則升級為「只存 settled、非 placeholder（borrowed）、可顯示」——clamp 的 overshoot 空頁經 keepPreviousData 帶到目標頁時不再被誤存。codex 於 sandbox 無法跑 socket 類 gates 屬環境限制，本機全綠有效。
- **第三輪追加（修，免第四輪）**：react-query 預設 `networkMode:'online'` 在 `navigator.onLine=false` 時把 fetch 變成 `paused`（isFetching false、無錯誤）——未快取詳情頁會空指標崩潰、warm 編輯頁 latch 誤收舊快照。根因裁決：**本 app 後端在 loopback，WiFi 斷線根本不代表後端不可達**，且 paused 會讓被動連線層失聲。修法：QueryClient `networkMode:'always'`（queries+mutations，附 rationale 註解）讓請求永遠實發、失敗走既有 network-error 路徑；詳情頁加 undefined-item Loader 兜底。paused 狀態從此不可能出現＝類消滅；期末全量 review 覆蓋，不再開輪。

## D26（Phase 4 codex review 裁決）：High 判 won't-fix，3 Medium + 4 Low 修

- **High「model 名與 log 檔路徑可能載有秘密」— won't-fix**：codex 的重現前提是操作者把 `OPENAI_MODEL` 設成自己的 API key、或把 `LLM_LOG_FILE` 路徑命名成 base URL——秘密被主動塞進非秘密欄位。model 名稱自 v1 起就由 `/llm/status` 公開（測試釘住的既有契約）；log 檔路徑為操作者自選設定、寫入失敗時回報路徑正是除錯所需。不變量的正確表述是「`openai_base_url`/`openai_api_key` 這兩個欄位的值永不進 log」，而非「任何可能被塞入秘密的字串都不得記錄」——後者推到極致連 workflow 名都不能記。
- **修**：(M1) LLM body 是不可信輸入：儲存邊界加 `_utf8_safe`（lone surrogate→U+FFFD，比照 memory_ai 的 `_coerce_str` UTF-8 閘）+ JSONL sink catch 由 OSError 放寬為 Exception；(M2) `finish()` 絕對不可拋——最終保護為完全靜默（觀察者唯一的錯誤結局是影響被觀察的呼叫）；(M3) FE 詳情 cache 以 `started_at` 作實例判別子 + 不符時顯示已被取代（後端重啟 id 重用）；(L2) timeout 補標當前 attempt 的安全分類；(L3) 隱私文案改為有條件（LLM_LOG_FILE）；(L4) 日誌 404 專屬文案；(L5) 清單加水平 scroll 容器。
- **L1 JSONL 半行 — won't-fix + 註解**：opt-in 除錯 sink，磁碟滿寫半行只壞該行，消費端跳過即可；交易式寫入不值。
- **另**：實作代理誤診 `except A, B:` 為「無效 Python / ruff bug」——實為 PEP 758（3.14）合法語法、ruff 按 target 正規化；監督者已改正其註解為真實理由（拆兩個子句為求 formatter 穩定與可讀性），拆分本身保留。
- **第二輪追加（3 Medium 全修，免第三輪）**：(A) `context_memory.llm` logger 在預設 uvicorn 下無 handler、有效等級 WARNING——承諾的 INFO console sink 靜默失效 → main.py import 時做一次應用層 logger 組態（INFO + stderr StreamHandler + propagate=False + 冪等 guard），以真實 uvicorn 行程驗證 INFO 行輸出且不重複；(B) usage 由「最後一次覆蓋」改為 per-attempt 記錄 + finish 時逐欄位加總（150+260=410 測試釘住），attempt 增 request_chars/response_chars；(C) 新設定 `llm_log_body_max_chars`（預設 200k，與筆數上限正交的 RAM 界限）在 `_stored_body` choke point 截斷（先 utf8-safe 後切、`…[紀錄過長已截斷]` 標記、truncated 旗標 + FE 已截斷 badge）。三項皆行為測試釘住（445 tests），期末全量 review 覆蓋，不再開輪。

## D27（Phase 5 codex review 裁決）：3 High 中兩項屬「文件與現實不符」以誠實化處理，其餘全修

- **背景**：Phase 5 首輪 codex review 判 needs changes：High ×3（run_shell 無圈禁、dotenv 插值回注 `${OPENAI_API_KEY}`、symlink 圈禁缺口）、Medium ×4、Low ×2。codex 同時確認多項既有防線（名稱 traversal、write/read/list symlink 防護、DELETE 外部 symlink、輪詢終止、CORS）未被攻破。
- **H1 run_shell 無檔案系統圈禁 — 修「宣稱」而非加圈禁**：shell 全能力是使用者的明確需求（比照 Claude Code 建 skill；D21 v1 明載不做容器隔離），bash 本身不可能只靠 cwd 圈禁——真正的缺陷是文案/系統提示宣稱「限工具目錄內」而實際只有 write_file 有強制邊界。修法：所有 overclaim 改為誠實描述（run_shell 以服務自身權限執行、staging 為工作目錄與行為慣例；write_file 的 staging jail 才是強制邊界），UI 安全註記同步改寫，信任邊界回歸 D21 的本義：只安裝你信任的指示。加真圈禁（bwrap/nsjail/chroot）判過度工程且平台綁定，v1 不做。
- **H2 dotenv 插值回注憑證 — 修**：`dotenv_values()` 預設做 POSIX 變數展開且會從父行程 os.environ 解析——工具 `.env` 寫 `${OPENAI_API_KEY}` 就把 from-scratch env 刻意排除的憑證原樣注回（codex 以假 key 實證）。修法：`interpolate=False`（所有 dotenv 讀取點），並以「假 key + `${OPENAI_API_KEY}` 字面值必須原樣出現在子行程 env」測試釘住。**附帶誠實化**：同 UID 子行程原則上可讀 `/proc/<ppid>/environ`，env 清洗防的是「意外」洩漏而非對抗性隔離——此界線寫進模組文件，與 H1 同一個信任模型。
- **H3 symlink 圈禁缺口 — 修**：(a) 套件目錄本身是 symlink → 列為 invalid（使用者看得到原因），杜絕掃描跟隨外部目錄；(b) `tool.json` 是 symlink → invalid；(c) `set_enabled` 寫入前 resolve manifest 路徑並要求落在 resolve 後的套件目錄內（否則 PATCH 可經 symlink 改寫 tools 目錄外任意檔案）。三者皆測試釘住。
- **M4 單回覆 tool_calls 無上限 — 修**：上限 16（超出者不執行、每個 id 仍回「rejected: too many」的 tool result 保持配對）；每個呼叫處理後 `await asyncio.sleep(0)` 讓出（unknown-tool 路徑原本零 await，5000 個呼叫可讓 asyncio.timeout 永遠開不了火）；messages 改就地 append 消 O(n²) 複製（recorder 在 begin_attempt 已快照，無 aliasing 風險）。
- **M5 manifest 無大小上限 — 修**：`tool.json` 讀取前 stat 上限 64 KiB、parameters schema 序列化上限 16 KiB，超限=invalid。理由：schema 會隨每一次 LLM 請求重送，無上限即 token/DoS 隱患。
- **M6 OpenAPI 抓取可被慢滴規避 — 修**：httpx 的 timeout 是「無活動」計時（每 29 秒滴 1 byte 可無限跑），比照 llm.py 的外層牆鐘模式以 `asyncio.timeout(60)` 包整段抓取。
- **M7 併發安裝無上限 — 修**：同時只允許一個安裝 job；已有 active job 時 POST 回 409 `install_in_progress`（單人本機工具，序列安裝是正確語意，整類消滅並附帶解除 _TASKS 無界成長）。
- **L8 job 404 輪詢不止 — 修**：`installJobRefetchInterval` 對 404 錯誤也停止（後端重啟後 job 消失屬終局）；其他錯誤視為暫態繼續輪詢。
- **L9 AI 日誌連結非 deep link — 修**：改 `/llm-logs?log=<id>`，日誌頁讀 search param 自動展開該筆（不在清單則靜默忽略）。
- **第二輪複審（7230e1b 後）**：九項原修正全數確認成立；另出 5 個新 Medium，裁決全修：
  1. **tool_calls 資源面未封口**：cap 只擋執行、超額 call 仍逐一建訊息（無界記憶體/prompt）→ 入口設 `_MAX_TOOL_CALLS_ACCEPTED=64`，超過即判協議濫用走既有 upstream-invalid 502 路徑，不進 tool round。
  2. **PATCH 繞過 manifest 上限**：`set_enabled` 未 stat 就整檔讀、pretty 重寫可把合法 manifest 撐超限 → 讀前 stat、寫前驗序列化大小，超限拒絕且不動檔案（上限套滿所有出入口）。
  3. **tools 根內部 symlink 別名 DELETE 刪到本體**：resolve 後仍在根內故 containment 放行、`rmtree` 砍真套件（掃描列 invalid 但 UI 可刪 → 可觸發資料遺失）→ mutation 入口先查未 resolve 路徑：DELETE 只 unlink 別名、PATCH 拒絕。round-1 修了 scan 層與 tool.json symlink，此為 mutation 層同類補洞。
  4. **FE 暫態輪詢錯誤丟失 job 追蹤**：任何錯誤都當非 active、重送先清 jobId，撞 409 後執行中 job 永不再被輪詢 → jobId 只在 404 或新 202 時替換；409 保留舊 id 續輪詢並顯示衝突提示。
  5. **輸出上限在完整緩衝後才套用**：`communicate()` 先吃整個 stdout 才截斷、builder read_file 整檔讀 → 改並行分塊讀、超限即殺 process group（比照 timeout 路徑）+ 截斷標記；read_file 讀前 stat 拒絕超大檔。
- **流程備忘**：本輪 review 實際 15 分鐘完成，但 `task --wait` 串流中繼斷裂導致結果延遲半小時才被讀到；並發現多個歷史輪次的 codex sandbox 孤兒 process（TestClient 探測在 `--unshare-net` sandbox 下掛死不返回）佔著資源，已全數清除。後續 review 改由主代理直接輪詢 companion 的 status/result 取結果，不依賴 `--wait` 串流。
- **第三輪複審（ba6e750 後）**：round-2 五項中四項確認成立（entry cap 64 + recorder 結案、manifest 讀寫 cap、內部 alias mutation 行為、FE 500/404/409 狀態流），第五項（有界管線讀）被指出生命週期縫隙；另出 4 個新 Medium。裁決全修：
  1. **`proc.wait()` 只等 leader**：背景後代繼承 stdout 時 pipe 永不 EOF，非 daemon reader thread 洩漏、shutdown 可被卡 → thread 改 daemon 作保險 + join 超時後補殺 process group（後代同組即死 → EOF → 收尾），修正「leader 退出＝EOF」的錯誤註解；setsid 雙重 fork 逃逸明載為 v1 界外。
  2. **read_file stat gate 擋不住 FIFO/增長**：非 regular file 通過 size 檢查後 `read_text` 永久阻塞（asyncio timeout 取消不了 threadpool worker）→ 要求 `is_file()` + 改 open+read(cap+1) 有界讀（stat 是廉價的第一道 gate、bounded read 是硬 gate）。
  3. **list_dir 先列舉整樹再套上限**：遍歷中計數、到頂即停 + 既有截斷標記，保留已列項目的確定性排序。
  4. **工具 .env 無上限**：`_ENV_FILE_MAX_BYTES=64KiB` 讀前 stat，超限依既有 malformed-.env 慣例降級為 `{}`；installer `validate_package` 同步把超大 .env 列 invalid（裝不進來，runtime 降級只是裝後被改壞的防線）。
  5. **紀錄體積隨輪數平方成長**：每輪記整段對話 × installer 24 輪 × 16×50k 工具結果 ≈ 單筆數百 MB → 在 begin_attempt choke point 對每次 attempt 的 request_messages 套總量預算（沿用 `llm_log_body_max_chars`，新→舊保留、超出者折疊為一則「較早 N 則訊息已省略」合成訊息 + truncated 旗標）；小對話儲存位元組不變。
- **第四輪複審（4f9965c 後）**：round-3 四項確認成立（list_dir 逐層有界、`_apply_total_budget` 切點/marker/skip-guard/`request_chars` 一致、`finish()` 不拋防線未破）；第五項（process teardown）被指出仍有縫隙，另出 3 個同源 TOCTOU 類。裁決全修（無 won't-fix），並以「共用 helper 殺整類」為原則：
  1. **killpg 逃逸 race + detached daemon 洩漏（承接 round-3 F1）**：「reader thread 存活才補殺」有兩個真漏洞——(a) 關掉 stdio 但存活的同組背景 daemon 永不被殺（reader 先 EOF），(b) leader 已被 `wait()` 回收後 PGID 可被並行新 session 重用，補殺可能誤殺。根因是 `proc.wait()` 回收 leader 就釋放了 PGID 保護。改為 **race-free 的「先殺整組、後回收」**：以 `os.waitid(P_PID, WEXITED|WNOWAIT[|WNOHANG])` 輪詢等待（不回收 → zombie leader 持續 pin 住 PGID），完成或逾時後**無條件** `os.killpg(proc.pid, SIGKILL)` 拆掉整組（一律拆組同時根治 detached daemon 洩漏），最後才 `proc.wait()` 回收真實 exit code。double-fork/setsid 逃出 group 者仍界外（D21，daemon thread flag 為誠實 backstop）。
  2. **read_file 的 FIFO 阻塞 + symlink 越獄 TOCTOU**：stat→open 之間可換成 FIFO（`open` 永久阻塞、asyncio timeout 停不了 threadpool worker）或換成 symlink（突破 staging jail）→ 改 `os.open(O_RDONLY|O_NONBLOCK|O_NOFOLLOW)` 開一次、對同一 fd `fstat` 確認 `S_ISREG`、再 bounded read(cap+1)。加固的是 D21 指定唯一強制的 staging jail，故在範圍內。
  3. **.env / tool.json 同類 stat-then-reopen TOCTOU**：FIFO .env 會讓**每次工具呼叫**永久阻塞（真實靜態危害非純 race）→ 與 read_file 併為共用「開 regular file→fstat→有界讀」helper，.env 從 fd 讀後以 stream 餵 dotenv、tool.json 從 fd bounded read 後 `json.loads`；殺整類。
  4. **tool_calls 引數大小無上限（承接 flood cap 的大小維度）**：64-call cap 只限數量，單一回覆仍可挾帶巨大 `arguments` 被完整 echo/`json.loads`/送下一輪 → 在 summarize/echo/parse 前加 per-reply 序列化位元組上限，超限走既有 flood/upstream-error 502 路徑。與 round-2 已接受的 flood cap 同一「不可信上游回覆」信任模型。
- **累積立場**：本 phase 的安全修正一律以「D21 的信任邊界（只裝可信指示、無容器隔離）＋ 真正強制的 staging jail ＋ 不可信上游回覆」三者為準繩；純粹假設本地生成工具主動 race 攻擊主機、且修法昂貴又平台綁定者才判界外。至今無此類界外項——所有發現皆有 jail 加固或真實靜態危害/洩漏的正當性。
- **第五輪複審（1d57697 後）**：round-4 修正全數成立（regular-file helper 的 fd ownership、FIFO 防阻塞、final-component O_NOFOLLOW、bounded read、dotenv stream/interpolation、recorder finish() 防線）；codex 明確把 ancestor-symlink 主動競跑列為 D21 排除的 adversarial-local-tool 情境、不列 finding（本輪 scoping 立場已被 review 端接受）。另出 2 個 Medium，皆為 round-4 修正自身的完整性缺口，全修：
  1. **teardown kill callback 與 reap 的交錯**：慢速 reader 若在主執行緒 `proc.wait()` 回收 leader 後、才因 pipe buffer 內既有的超量資料觸發 overflow kill callback，`_kill_process_group` 的 `getpgid(已回收 pid)` 可能對重用的 PID 誤殺其他 process group → 以一把 `threading.Lock` + `reaped` 旗標讓 callback 與「killpg + reap」互斥：teardown 在鎖內設旗標後才 reap，callback 在鎖內見旗標即 no-op（此時 leader 尚未 reap、pid 仍有效才會執行 kill）。持有 Popen 已排除 subprocess._cleanup 的跨執行緒 reap，此鎖再補上程式內交錯這一面。
  2. **F4 位元組上限漏算 assistant content**：byte cap 只計 id+name+arguments，但 echo 的 `_assistant_tool_call_message` 會原樣帶入未受限的 `_completion_content`——巨大 content 配小 tool call 仍過關並完整送回下一輪 → 把 content 長度一併計入 aggregate、超限走同一 upstream-invalid 502 taxonomy。
- **第六輪複審（df20e6e 後）**：round-5 兩項確認正確完整（teardown kill/reap 互斥、join 在鎖外、`reaped` closure 可見性、F4 content 計量與 echo 同源）。另出 2 個 Medium，**皆為 round-4 有界讀 class-fix 漏掉的呼叫點**（同一 FIFO 永久阻塞類，codex 明標「無需 race」——真實 liveness bug，非對抗式 race）：`set_enabled()` 的 tool.json 仍 stat+read_text（round-4 只轉了 scan、漏了 mutation 讀取路徑）；builder `write_file` 對既存 FIFO 呼叫 write_text 永久阻塞、卡住 single-flight。
- **裁決：不只補這兩處，做完整 sweep 終結此 class**。理由：round-4 的 sweep 不夠徹底才會漏兩個 site，只補報告的兩處等於再賭一次 straggler。做法：(a) `set_enabled` 讀取改走 `_read_regular_file_capped`（None→False，沿用「did not happen」契約）；(b) 新增對稱的寫入側 helper——`os.open(O_WRONLY|O_CREAT|O_TRUNC|O_NONBLOCK|O_NOFOLLOW)`：對無 reader 的 FIFO write-only nonblock 立即回 ENXIO 而非阻塞、O_NOFOLLOW 拒 symlink leaf（比照讀側加固 staging jail），供 `write_file` 使用；(c) 對 tools.py / tool_builder.py 全部 `read_text`/`write_text`/`open`/`stat` 呼叫點做完整清查，凡是存取「可被工具或 run_shell 影響的路徑」者一律走有界/驗證 regular file 的 helper，同批修掉任何殘餘。此後此 class 應為空。

## D27 結案（Phase 5 review 迴圈收斂，2026-07-16）

- **第七輪複審（7e5e300 後）：Approve，無 High/Medium finding**。codex 獨立重跑 sweep 確認 FIFO/檔案開啟類已空、寫入 helper 語意正確、set_enabled 讀寫轉換無回歸。
- **迴圈全貌**：7 輪（前 6 輪 needs-changes → 第 7 輪 approve），嚴重度單調收斂——round-1（3 High：run_shell 宣稱/dotenv 插值/symlink 圈禁）→ round-2/3/4（各 4-5 Medium：資源上限、process 樹生命週期、TOCTOU）→ round-5（2 Medium：修正自身收尾）→ round-6（2 Medium：同一 class 漏網 site）→ round-7 approve。共 6 個修正 commit（`7230e1b`→`7e5e300`）疊在 2 個實作 commit（`f0dc649` tool loop、`5aea7e6` installer）之上。
- **裁決原則回顧**：全程以「D21 信任邊界（只裝可信指示、無容器隔離）＋ 真正強制的 staging jail ＋ 不可信上游回覆」三準繩判定；唯一判界外者為 round-5 的 ancestor-symlink 主動競跑（純對抗式本地工具、修法昂貴且平台綁定），且此界外立場已被 review 端接受。其餘全修。
- **本機 gates（最終）**：pytest 565、e2e smoke 76/76、vitest 37、ruff/format/ty 全過。依 D23 phase 通過即推送。

## D28（Phase 6 文件 codex review 裁決）：文件準確性全修，4 輪收斂 Approve

- **背景**：Phase 6 是網頁優先文件改寫（README/AGENTS.md/backend·frontend·e2e README），review 標準＝**對照程式碼的事實準確性**（非文筆）。
- **迴圈**：4 輪（前 3 needs-changes → 第 4 approve），全部是「文件承諾超過程式碼實際」的 over-claim，且呈現與 Phase 5 相同的「修例不修類」教訓——每輪修掉 codex 指出的行，同一宣稱類卻在別處復現，逐輪才靠**全面 grep sweep** 清完：
  - round-1（5 Medium）：安裝失敗前無 AI 摘要/日誌、日誌「完整」其實有 200 字元 preview + body 截斷、mutation gate 不含刪除、EXIT trap 不清 build 產物、`.staging` 非只有中斷才殘留。
  - round-2（3 Medium）：同三個 class 在未改到的行復現（feature-tour 日誌行、`GET /logs/{id}`、root README `.staging`、摘要與連結耦合）→ 改為全 class sweep。
  - round-3（1 Medium）：instructions 文字仍稱「完整記錄」，但 instructions（≤20000 字元）同受 `LLM_LOG_BODY_MAX_CHARS`（下限 1000）截斷 → 掃掉所有「原文/原樣/完整記錄」措辭，改為「受 body cap 截斷；一般長度 key/token 短於上限故仍完整記到」（安全警告不變）。
  - round-4：Approve。
- **修正另發現並改正監督者 spec 的兩處錯誤**：(1) 無 PyPI 發佈計畫（D15），故啟動指令為 `uvx --from backend/dist/*.whl context-memory`（對齊 wheel_smoke.sh）而非 bare `uvx context-memory`；(2) AI 動作 UI 名稱為「AI 進度更新」非「AI 協助更新」。另新增重要風險揭露：貼進安裝器的第三方 key 會同時進工具 `.env` 與 AI 日誌。
- **backend/frontend/e2e README 過時宣稱同步修正**：OPENAI_TIMEOUT_SECONDS 60→120、LLM_PROMPT_BUDGET_CHARS 32000→200000、補齊 LLM_LOG_*/TOOLS_DIR/LLM_TOOL_*/TOOL_INSTALL_* 鍵與 /api/tools* 路由、補 /tools 與 /llm-logs 頁、修正 mutation-gate 與 AI 動作識別名、補 wheel_smoke.sh 章節。
- **教訓沉澱**：「修類不修例」不只適用程式碼，文件的重複宣稱同樣要 grep 全掃 + 驗證殘留為零，否則 review 會逐行打轉。已成慣例。

## D29（最終全量 codex review 裁決，對起始點 4f1ab55）：2 個跨 phase Medium 全修

- **背景**：整輪收尾對 `4f1ab55..HEAD`（31 commits）做全量 review，抓 phase-scoped review 結構上看不到的跨 phase 接縫。判 needs changes，2 Medium，皆為 phase 4（LLM 邊界/recorder/config 界限）與 phase 5（工具迴圈/安裝器）之間的整合缺陷，全修。
- **F1 即時對話無總量上限（llm.py，工具迴圈送出路徑）**：round-3（D27）只給了 **recorder** 總量預算（`_apply_total_budget`），但**實際送給模型的 `messages`** 沒有。每輪最多 16 個工具結果 × 各 `llm_tool_output_max_chars`（預設 50k）× `llm_tool_rounds_max`/`tool_install_max_rounds`（8/24）輪，最壞 6.4M/19.2M 字元（配置拉滿達 ~512M），遠超 `llm_prompt_budget_chars`（200k）→ context-length 失敗或大量記憶體。**修**：工具迴圈在每次 `create()` 前檢查累積對話序列化大小，超過 budget 即停止 advertise tools、走既有 finalize 路徑（附 finalize nudge、最後一次 tools-free 完成），把即時對話有界化在 budget + 一輪成長內。這是 recorder 總量預算在「真正送出路徑」上的對稱補完。
- **F2 asyncio.timeout 在工具 subprocess 期間非真 deadline（llm.py:803）**：外層 `asyncio.timeout` 宣稱涵蓋整個互動，但工具執行走 Starlette `run_in_threadpool`，AnyIO 預設在 worker 完成前忽略 host cancellation；且一輪內工具是**循序** await，一輪最多 16 個各跑到 `llm_tool_timeout_seconds`（預設 60、可設 600），最壞可讓請求 overrun 到 16×內層 timeout。**修（採 per-call skip，不動 LlmTool 協定/tools.py）**：(a) 迴圈開始以 `loop.time()`（與 asyncio.timeout 同時鐘）算一次 monotonic deadline；(b) 工具執行 for 迴圈**每個呼叫執行前**檢查剩餘期限，若 `remaining <= _TOOL_DEADLINE_FLOOR_SECONDS(1.0)` 就**不啟動**該工具、改回 `_TOOL_DEADLINE_REACHED` rejection（保 id 配對）——把殘餘 overrun 有界化到「至多一個**已在執行中**的工具的自我 timeout」而非 16× 之；(c) 修正 docstring 誠實描述（外層 deadline 對 in-flight threadpool 工具不可搶占，最壞牆鐘 ≈ 外層 + 一個 `llm_tool_timeout_seconds`）。
- **F2 方案選擇備註**：本 D29 初稿曾寫「把工具有效 timeout 夾到 `min(config, remaining)`」，但實作時改採更保守的 **per-call skip**：夾 timeout 需把剩餘期限一路穿到 `LlmTool.handler` 協定與 tools.py subprocess，動到跨檔協定；skip 只在 llm.py 迴圈內加一個「過期就不啟動」檢查，同樣把總工具時間有界化在 deadline + 一個進行中工具，surface 小得多、風險低，符合簡潔/易擴充原則。夾 timeout 只會再收緊「最後啟動那個工具」的殘餘，對單人本機工具收益有限——若日後要更緊可另立變更、走協定改動。
- **依據**：兩者皆為真實資源邊界缺陷、且正是「一個 phase 設的假設被另一個 phase 依賴/違反」的跨 phase 問題（F1：recorder 有界但送出路徑無界；F2：phase4 外層 deadline vs phase5 threadpool 工具）——全量 review 的價值所在。修法沿用既有 finalize/rejection 機制、保守簡潔，不做中段訊息 elision（會動 OpenAI 訊息序列有效性與 tool_call_id 配對，過度工程）。
