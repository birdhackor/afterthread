# Web App v3 總結報告

本輪自 `a5ac409`（v2 收尾推送點）到收尾，共 **16 個 commit、46 檔、+4971/−336**，分 P0–P7 完成。計畫見 `web-v3-plan.md`、決策全紀錄見 `web-v3-decisions.md`（D30–D37）。流程沿 v2：主代理規劃/監督/裁決、subagent 依難度派工（haiku/sonnet/opus）、phase 內只 commit、phase 完成 codex review 收斂才 push、最後對起始點全量 review。

## 七項需求對照

| # | 需求 | 結果 |
|---|---|---|
| 1 | `CLAIDE.md` 打錯字，重做正確 symlink | ✅ `CLAUDE.md → AGENTS.md`，舊檔刪除，`CLAUDE.md` 進 `.gitignore` 防未來誤 commit（D30） |
| 2 | CLI 改 typer 取代 argparse | ✅ P1：`typer.Typer` 單指令、`envvar=` 取代手寫 int-env、行為契約 wheel_smoke 端對端釘住；typer 本已在依賴樹（零重量增加）（首輪 Approve） |
| 3 | 日誌記金鑰的簡易處理 + loguru 評估 | ✅ P3：**安裝表單秘密欄位**（值只進 run_shell env 與工具 .env、模型看不到）+ **已知值遮蔽**（三個邊界：紀錄/live 對話/job state，截斷前遮）+ JSONL rotation；**loguru 評估後不採用**（D34，唯一收益 rotation 用零依賴取得） |
| 4 | md 資訊 → zensical 文件站 + Pages/Actions 部署 + 手動清單 | ✅ P6：`website/` 七頁 zh-TW 站、Actions artifact 部署 workflow（僅 main/master 觸發）；清單見下 |
| 5 | hatchling → uv 打包（若功能等同） | ✅ P2：**維持 hatchling**（D32——uv_build 三項等同但驗證 hook 無等效，實驗重現裸 build 靜默出壞 wheel；使用者條件是「功能等同」，事實不等同） |
| 6 | tool-call 實作說明 + LangChain 評估 | ✅ P5：`docs/tool-calling.md` 淺顯說明手作迴圈；**LangChain 評估後不重構**（D35——deadline skip 需依序啟動、與 ToolNode 並行派發衝突，invariant 全得重刻無收益） |
| 7 | 動態 char↔token ratio、改用 token 卡 | ✅ P4：`services/token_budget.py` 滾動視窗學 ratio，兩個預算改 token 計價（`LLM_PROMPT_BUDGET_TOKENS`/`LLM_TOOL_CONVERSATION_BUDGET_TOKENS`）；ratio 下限 0.1 兼作絕對字元兜底 |

## Phase 完成與推送

| Phase | 內容 | review 收斂 |
|---|---|---|
| P0 | 計畫/決策文件 + symlink + gitignore | — |
| P1 | CLI typer | 1 輪 Approve |
| P2 | 打包裁決（維持 hatchling，無程式碼變更） | — |
| P3 | 金鑰遮蔽 + 秘密欄位 + rotation | 6 輪（安全敏感，見下） |
| P4 | token-ratio 預算 | 3 輪 |
| P5 | tool-call 說明文件 | 3 輪（文件準確性） |
| P6 | zensical 站 + Actions | 3 輪（文件 over-claim class） |
| P7 | 全量 review + 總結 + 清單 | 本文件 |

**P3（D36，六輪收斂）** 是本輪最重的安全 phase。發現軌跡單調收斂：round-2 live 路徑類（1H+7M：秘密經 run_shell echo 進對話/job state）→ round-3 截斷碎片與持久化類（3H+2M：截斷先於遮蔽留碎片、manifest 內嵌秘密、.env 序列化變形）→ round-4 遮蔽器順序邊角（1H+2M）→ round-5 演算法資源邊界（1M：range 記憶體放大 + observer 不變量）→ round-6 approve。核心設計：遮蔽推到「文字進入對話/狀態」的三個邊界，模型只看得到遮蔽標記；秘密欄位讓金鑰從頭到尾不進 prompt。

## 跨 phase 全量 review（對起始點 `a5ac409`）

對 `a5ac409..HEAD`（16 commits）做全量 codex review，專攻 phase-scoped review 看不到的跨 phase 接縫與全系統不變量：P3 遮蔽與 P4 token-ratio 都改到 llm.py 的 completion 處理（chars_sent 觀測 vs 儲存體遮蔽是否互不干擾）、typer CLI 是否仍注入工具子系統與 token 預算所依賴的 TOOLS_DIR/DATABASE_URL、改名的 token 設定有無殘留舊字元鍵、from-scratch 子行程 env 加了 P3 安裝秘密 allowlist 後 OPENAI_* 是否仍結構性缺席、遮蔽的 observer 方向不對稱（llm_log fail-open / live path fail-closed）是否一致、Actions workflow 在 feat/web-app 是否真的不部署。

**結果：Approve，無 High/Medium 的跨 phase 或全系統問題。**

## 最終 gates（全綠）

- backend：ruff / format / ty / **pytest 681**
- e2e：**smoke 76/76**、**wheel_smoke（打包 uvx 模式）23/23**
- frontend：lint / **vitest 44** / build

## ⚠️ 需要你手動設定的項目清單

### A. 文件站發佈到 GitHub Pages（P6，若你要發佈）

文件站的 GitHub Actions workflow（`.github/workflows/docs.yml`）**只在 push 到 `main`/`master` 時觸發**——目前在 `feat/web-app` 上，它不會做任何事。要真正發佈，需要你在 GitHub 網頁介面手動完成：

1. **Settings → Pages → Build and deployment → Source**：選 **「GitHub Actions」**（不是預設的「Deploy from a branch」）。
2. **⚠️ 這個 repo 目前是 private**：GitHub Pages 對 private repo 需要**付費方案**（Pro/Team/Enterprise）才能啟用，且**一旦啟用，發佈出去的站台內容就是對外公開的**——等同把文件站裡所有技術說明公開。merge 到 main 之前，請確認這是你要的（若不想公開，就不要開 Pages，文件站仍可在本機用 `uvx zensical build` 建置後本地瀏覽）。
3. **Workflow 權限**：workflow 內已宣告 `id-token: write` 等最小權限；若你的 repo/org 在 Settings → Actions → General 把 workflow 權限收緊了，需手動放行。
4. **`github-pages` environment**：首次成功部署時 GitHub 通常自動建立；若要加保護規則（限定分支、需審核）可自行設定，非必要。
5. **實際發佈時機由你決定**：把 `feat/web-app` merge 到 `main`（或改 workflow 觸發分支）後，push 才會觸發首次建置與部署。

### B. 已知限制（zensical alpha）

- **中文搜尋弱**：zensical 0.0.50 的搜尋引擎（Disco）不做中文分詞——**已實測確認**（比對產生的 `search.json`），中文散文段落裡的中段關鍵字（連「記憶項目」這類核心術語）大機率搜不到；技術頁因混了英文/半形符號會「意外」搜到一些詞，但不是真的支援中文。要等官方 Disco 獨立版是否補上分詞。文件站本身（導覽、內容、mermaid、明暗主題、zh-Hant UI）都正常。
- zensical 是 alpha（0.0.x），採用是你指名的選擇（D33）；CI 刻意不做 build cache（官方建議 + #641 non-deterministic builds 未解）。

### C. 舊 .env 設定鍵改名（P4）

若你既有的 `<data-dir>/.env` 裡有這兩個舊鍵，它們會被**靜默忽略**（pydantic `extra="ignore"`），需手動改名才會生效：
- `LLM_PROMPT_BUDGET_CHARS` → `LLM_PROMPT_BUDGET_TOKENS`（改用 token 計價，預設 200000）
- `LLM_TOOL_CONVERSATION_BUDGET_CHARS` → `LLM_TOOL_CONVERSATION_BUDGET_TOKENS`（預設 500000）

### D. 個人檔案

- `CLAUDE.md`（→ AGENTS.md 的 symlink，取代打錯字的 `CLAIDE.md`）維持未追蹤、已進 `.gitignore`；若不需要可自行刪除。

## 留待你決定

- **未併 main**（v2 + v3 都在 `feat/web-app`）——合併時機由你定。
- 文件站是否發佈（見 A，牽涉 private repo 付費 + 內容公開）。
