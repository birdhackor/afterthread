# Web App v2 總結報告

本輪自 `4f1ab55`（2026-07-15 開工點）到收尾，共 **31 個 commit、87 檔、+13898/−1596**，分 7 個 phase 完成。計畫見 `web-v2-plan.md`、決策全紀錄見 `web-v2-decisions.md`（D01–D28）。

## 使用者需求對照（原始 6 項 + 追加 5 項）

**第一則訊息 6 項需求：**
1. **總覽頁底部狀態雙向凍結** → Phase 1 修復。被動層（`client.js` 依無歧義證據回報 connectivity）＋ 主動權威 probe（`health.js` 語意判定 `status==="ok"`、generation counter 防亂序、10s timeout），30s 輪詢 + focus/online 立即 probe；reachable false→true 自動 force-reload LLM 狀態。
2. **README 網頁優先改寫** → Phase 6。根 README 全改寫（什麼是它 → uvx 啟動 → 首次設定 → 網頁功能導覽 → KB 安裝指南 → 開發模式 → CLI/OpenCode 補充）。
3. **打包成 wheel、內嵌 FE dist、uvx 一鍵啟動** → Phase 2。pre-build 複製 dist 進 package + hatch `artifacts`；FastAPI `app.frontend()` 服 SPA；cli.py 錨定 data dir（chdir）；打包端對端 `wheel_smoke.sh`（23 斷言）驗證。
4. **AI 理解公司內部術語 → 接內部 KB（有 API）；先做基礎，接法寫成指南** → Phase 5（基礎）+ Phase 6（指南）。做成**網頁安裝器**（D21，取代原靜態模板構想）：貼 OpenAPI URL + 指示 → LLM 帶 meta-tools 自建工具包放到 `<data-dir>/tools/<name>/` → 工作流自動附掛。
5. **大量 LLM 互動 logging 方便除錯** → Phase 4。in-memory ring（預設 50 筆）+ 可選 JSONL（`LLM_LOG_FILE`）+ INFO 摘要行；「AI 日誌」頁可展開每次嘗試；秘密永不入 log。
6. **內部 LLM 為 GLM5.2（1M context），檢視需調整的組態** → Phase 4。`llm_prompt_budget_chars` 32k→200k（上限 2M）、`openai_timeout_seconds` 60→120（上限 1800）、新增選用 `openai_max_output_tokens`；不動儲存上限、不引入 tokenizer（char-based 保守夠用）。

**第二則訊息 5 項追加：**
1. **subagent 依難度分配 haiku/sonnet/opus 省 token** → 全程遵行：機械/標準實作 sonnet、invariant 密集或跨檔語意重構 opus、瑣碎掃描 haiku；主代理維持 fable 只規劃/監督/裁決。
2. **KB 連接器改為網頁「安裝」功能** → Phase 5 落實（見需求 4）；基礎設施＝編輯檔案能力（write_file 限 staging jail）+ shell 執行能力（run_shell）。
3. **加 TanStack Query 重構階段** → Phase 3。頁面資料抓取與 mutations 改 useQuery/useMutation；保留 jotai 於 connectivity/health/llm（app 層級、非 React 寫入者，D22）。
4. **context7 已設定，需要時用** → Phase 2 打包研究等處使用。
5. **phase 內不 push、phase 完成才 push；per-phase review 只審該 phase；最後對起始點全量 review** → 全程遵行（D23）。

## Phase 完成順序與推送

| Phase | 內容 | 狀態 |
|---|---|---|
| P0 | 計畫 + 決策紀錄文件 | ✅ |
| P1 | BE 連線/LLM 狀態偵測修復（item 1） | ✅ |
| P2 | wheel 打包 + uvx + FastAPI 服 SPA（item 3） | ✅ |
| P3 | FE 資料層重構 TanStack Query（追加 3） | ✅ |
| P4 | LLM logging + GLM5.2 組態（items 5+6） | ✅ |
| P5 | tool-calling 基礎 + KB 網頁安裝器（item 4，D21） | ✅ 推送 f16c55a |
| P6 | README 網頁優先 + KB 安裝指南（items 2+4 文件） | ✅ 推送 d745dc7 |

## Review 過程

每個 phase 完成後做 phase-scoped codex review，合理意見即修、修完再審，直到 approve 或只剩已裁決的 won't-fix。兩個最耗輪次的 phase：

- **Phase 5（D27）**：7 輪（6 needs-changes → approve）。嚴重度單調收斂：3 High（run_shell 宣稱誠實化、dotenv `interpolate=False` 防 `${OPENAI_API_KEY}` 回注、symlink 圈禁）→ 資源上限/process 樹生命週期/TOCTOU（tool_calls count+byte cap、`os.waitid(WNOWAIT)` race-free teardown、有界讀寫 helper 封 FIFO-hang 與 symlink 越獄、日誌總量預算防平方成長、single-flight 安裝）→ approve。
- **Phase 6（D28）**：4 輪（3 needs-changes → approve），全是文件對照程式碼的準確性 over-claim。

**共同教訓（已沉澱為慣例）**：「修類不修例」——每輪只補 codex 指出的點會讓同一問題類在別處復現、review 逐輪打轉；正解是每次都對該問題類做全面 sweep + grep 驗證殘留為零。Phase 5 的 FIFO-hang class 與 Phase 6 的文件 over-claim class 都是這樣才收斂。

**裁決準繩**：安全類發現一律以「D21 信任邊界（只裝可信指示、無容器隔離）＋ 真正強制的 staging jail ＋ 不可信上游回覆」三者判定；唯一判界外者＝ancestor-symlink 主動競跑（純對抗式本地工具、修法昂貴且平台綁定），此立場已被 review 端接受。

## 最終驗證（全 gates 綠）

- backend：ruff / ruff format / ty / **pytest 565 passed**
- frontend：biome lint / **vitest 37 passed** / vite build
- e2e：**smoke 76/76**、**wheel_smoke（打包 uvx 模式）23/23**

## 最終全量 review（對起始點 `4f1ab55`）

<!-- FINAL_REVIEW_RESULT -->
（待全量 codex review verdict 落地後填入。）

## 提醒：CLAIDE.md

根目錄有一個**未追蹤的個人檔案 `CLAIDE.md`**（→ AGENTS.md 的 symlink，檔名疑似 `CLAUDE.md` 的 typo）。全程從未 commit（每次 `git add` 都以 `':!CLAIDE.md'` 排除），維持未追蹤。若這是你要保留的個人設定，無需處理；若是誤建，可自行刪除。
