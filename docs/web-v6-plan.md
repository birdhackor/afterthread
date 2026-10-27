# web-v6 計畫：設定與秘密分家、一鍵請 AI 修正工具

起始點：`main` @ `5e8bcec`（0.4.0 之後），工作分支 `claude/eager-rubin-hi6q4m`。設計本體是
`docs/web-v6-design.md`，已處置一輪 Fable design review（見它的第 14 節）。實作期間的裁決記在
`docs/web-v6-decisions.md`，從 D42 起編號。

**這份文件的定位**：沿用 v4 計畫的體例，被 review 推翻的段落就地改成實際做法，不留舊句。設計與
計畫有出入時，以設計文件為準。

## 需求對照

| 問題（設計 §1） | 階段 |
|---|---|
| A1、A2、A4、A7 | P1 |
| A3、A5、A6（部分解決，見設計 §1 對照表） | P1 |
| C3（部分解決） | P1 |
| B1、B2、B4 | P2a |
| E1 | P2b |
| C1、C2 | P3 |
| D1 | P3（提示規則）、P5（自動驗證兜底） |
| B3 | P4 |
| D2、D3、D4、D5 | P5 |

## 階段

| 階段 | 內容 | 規模 | 依賴 | 驗收的不變量（設計 §11） |
|---|---|---|---|---|
| P1 | 設定與秘密分家＋修訂入口的回饋 | 中 | — | A、B、C、D、E、O、W |
| P2a | 工具執行紀錄 | 中 | — | F、G、H、I、U、W |
| P2b | 手動 purge | 小 | P2a | J、K、W |
| P3 | 一鍵請 AI 修正（工具頁） | 中 | P1、P2a | L、M、N、R、V |
| P4 | AI 結果旁的修正入口 | 小 | P3 | Q |
| P5 | 跑一次範例與修正後自動驗證 | 中 | P3 | P、S、T |

P1 與 P2a 互不依賴，可以對調；其餘照順序。

每一階段做完都能單獨使用：

- **P1**：TTL 這類設定搬到 `config.env` 之後就能修訂；被擋時訊息會說是哪個 key，而且送出當下就看得到。
- **P2a、P2b**：失敗有紀錄可看、可清。
- **P3**：工具頁一鍵修正；builder 從第一次修正就開始累積範例。
- **P4**：在擷取等頁面看到失敗，當場就能修。
- **P5**：「修好了沒」有後端的獨立驗證。

## 流程

1. 每階段開始記下 `PHASE_BASE`，實作完跑 gates，全綠才 commit。
2. **Gates**：
   - backend（在 `backend/`）：`uv run ruff format --check .`、`uv run ruff check .`、
     `uv run ty check`、`uv run pytest`
   - frontend（在 `frontend/`）：`pnpm lint`、`pnpm build`、`pnpm test`
   - 動到啟動或打包時，加跑 `e2e/smoke.sh`、`e2e/wheel_smoke.sh`
3. **Review**：commit 後以 `PHASE_BASE..HEAD` 做一輪對抗式 code review，逐條裁決、修正到收斂。
   本機有 codex 時沿用 `AGENTS.md` 的 codex 迴圈；雲端 session 沒有 codex，改用 Fable。駁回與
   裁決記在 `docs/web-v6-decisions.md`，必要時進 `裁決紀錄.md`。
4. 收斂後 push，進下一階段。
5. 全部完成後對 `5e8bcec..HEAD` 跑 overall review 與全部 gates，再更新 `CHANGELOG.md`
   （Unreleased）、`README.md`、`backend/README.md`、`frontend/README.md`、`docs/tool-calling.md`。

## 各階段要點

### P1 設定與秘密分家＋修訂入口的回饋

**範圍**：設計 §4、§5；§8.3 的 `not_published` 結局；§9 的「設定」區。

**後端**：

- `services/tools.py`
  - 讀 `config.env`，沿用 `_read_regular_file_capped` 與 `_parse_dotenv_text`。
  - `_build_tool_env` 改成「透傳 → `TLS_NO_VERIFY` → `config.env` → `.env`」，回傳的遮蔽值仍只
    來自 `.env`。
- `services/tool_builder.py`
  - `_unmaskable_env_error` 回報 key：最多 5 個、每個截到 64 字、先遮蔽；拼法閘用 `_env_line_key`
    取 key。
  - 送出時的閘：`start_revise_job` 在 `reserve_sync_operation()` 之前，用一次 worker hop 跑同一道閘
    與 `config.env` 可讀性檢查，refusal 為 `env_policy`。`run_revise` 裡的閘保留。
  - 修訂的 `run_shell` 環境加入 `config.env`；`_revise_user_prompt` 加 `config.env` 全文與 `.env`
    key 名兩段；base 與 revise 提示補 `config.env` 的說明。
  - `config.env` 讀不到時修訂拒絕（新增一條分類式錯誤訊息）。
  - `InstallJob` 加 `outcome`（`published`／`not_published`／`failed`），`ready=false` 走
    `not_published`。
- `routers/tools.py`
  - revise 對 `env_policy` 回 409。
  - 新增 `GET /api/tools/{name}/settings`，宣告在 `/jobs/{job_id}` 之前。回傳 `.env` key 名、遮蔽
    過的 `config.env`、衝突的 key、兩種提醒，以及是不是遷移來的工具。
- `schemas.py`：`ToolJobStatus.outcome`、`ToolSettings`。

**前端**（`ToolsPage.jsx`；新內容盡量做成獨立元件檔）：

- 工具詳情的「設定」區。
- 工作卡片的 `not_published`（橘色）。
- 修訂表單顯示 `env_policy` 訊息。

**文件**：`README.md` 的工具安裝指南（`config.env` 的用途、與 `.env` 的分工、怎麼搬）、
`backend/README.md`、`docs/tool-calling.md`。

**測試**：設計 §11 的 A、B、C、D、E、O、W。釘住舊訊息的既有測試（引用
`_ERROR_REVISE_ENV_UNMASKABLE`、`_ERROR_REVISE_ENV_UNMATCHABLE`、`_unmaskable_env_error` 的約 10 處）
改成新訊息。

### P2a 工具執行紀錄

**範圍**：設計 §6；§9 的「最近執行」（唯讀＋單筆刪除）與列表的 `last_run` 文字。一鍵按鈕在 P3。

**後端**：

- `models.py`：`ToolRun`。
- 新增 `services/tool_runs.py`：寫入、列表、單筆讀取與刪除；短 busy timeout；寫入前套 `_utf8_safe`。
- `services/tools.py`
  - `_run_tool_subprocess` 改成回傳結構化結果，outcome 分類含 `output_overflow`、`refused` 與
    `truncated`。逾時與超限路徑先遮蔽再交出。
  - `_make_handler` 呼叫注入的紀錄器；新增 `set_run_recorder`。
- `services/llm.py`：用 ContextVar 提供目前 AI 日誌紀錄的 id、process token 與 workflow。
- `main.py`：接上紀錄器。
- 路由：
  - `GET /api/tools/{name}/runs`
  - `ToolSummary.last_run`（含後端算好的 `fixable`）
  - 新的 `routers/tool_runs.py`：`GET`、`DELETE /api/tool-runs/{id}`

**前端**：工具詳情的「最近執行」；列表的 `last_run` 文字。

**測試**：F、G、H、I、U、W。**F 是這一階段的驗收門檻**：每種 outcome 交給模型的字串必須逐字
不變。

### P2b 手動 purge

**範圍**：設計 §7、§6.4。

**後端**：

- `tool_runs.purge(條件, dry_run, max_id)` 與 stats（筆數、失敗數、孤兒數）。
- 路由：`POST /api/tool-runs/purge`、`GET /api/tool-runs/stats`（宣告在 `/{id}` 之前）。
- 刪除工具成功之後刪除它的紀錄，回應帶 `runs_deleted`。

**前端**：

- 每個工具的「清除紀錄」、頁首的「清除…」。
- 預覽確認框，顯示「將刪除 N 筆」。
- 刪除工具時遇到 `runs_deleted: false` 的提示。

**文件**：`README.md` 寫明存了什麼、存在哪、怎麼清，以及後端停止時手動 VACUUM 的方法。

**測試**：J、K、W。

### P3 一鍵請 AI 修正（工具頁）

**範圍**：設計 §8 的工具列表與「最近執行」兩個入口、`/tools?job=<id>`；§10 的提示規則（builder
維護 `examples.json`、修訂時全部重跑）。

**後端**：

- `schemas.py`：`ToolReviseRequest` 加 `run_ids`（最多 3 筆）；有 `run_ids` 時 `feedback` 可省略。
- 修訂送出：
  - `run_ids` 的驗證與快照在取得名額之前。
  - `expected_vid` 的比對維持在名額之後。
  - 快照交給 `run_revise`。
- 修訂提示：Failing runs 段、除錯程序、範例規則；意見空白時的預設句。
- `origin.feedback`：附上紀錄的摘要（不含 stderr 與參數）。
- builder base 提示：安裝時把測過的輸入寫進 `examples.json`，只放唯讀輸入。

**前端**：

- 工具列表與「最近執行」的一鍵按鈕，依後端算好的 `fixable` 顯示。
- 工具詳情的「加上說明…」。
- `toolsRoute` 加 `validateSearch`；`/tools?job=<id>` 的獨立卡片模式。

**測試**：L、M、N、R、V。可修判斷與工作卡片狀態的前端邏輯抽成純函式，寫 vitest。

### P4 AI 結果旁的修正入口

**範圍**：設計 §8.2 的第三個入口。

**後端**：

- `routers/ai.py` 的 `_advertised_tools_lock` 放請求收集器。
- `CaptureResponse`、`EnrichResponse`、`AssistUpdateResponse` 加 `tool_failures`（含 `vid` 與
  `recorded`）。

**前端**：

- `CapturePage.jsx` 與 `ItemAiActions.jsx` 顯示提醒與按鈕。
- 按下時讀工具列表取 `current_vid`，處理三種 409。

**測試**：Q；前端判斷邏輯同樣抽成純函式。

### P5 跑一次範例與修正後自動驗證

**範圍**：設計 §10 的後端執行、§8.3 的自動驗證、§9 的驗證狀態。

**後端**：

- 執行函式：共享鎖加上 `enabled_llm_tools()` 的 handler，帶 `check_id`、總預算與短路。
- `health_check` job：新的 job 種類，占用全域名額。
- 修訂發布後的自動驗證；`InstallJob.verification`。
- `examples.json` 的形狀驗證與數量上限。
- 新設定值：驗證總預算（例如 `TOOL_CHECK_BUDGET_SECONDS`，預設 300）。

**前端**：

- 「跑一次範例」與驗證狀態。
- 工作卡片的驗證結果與「丟掉這一版」。

**測試**：P、S、T。

## 風險

- **遮蔽核心**：P2a 要改 `_run_tool_subprocess` 的回傳型別，這是全專案審最多輪的模組。不變量 F
  （交給模型的字串逐字不變）與 G（存下的內文已遮蔽）是這一階段的驗收門檻，review 也要特別看這段。
- **`ToolsPage.jsx` 已 1,820 行**，版本身分與快取由 v5 不變量 H、J 守著。新功能盡量做成獨立元件檔
  （設定區、最近執行、purge 對話框、驗證狀態），既有元件只留掛載點。
- **前端沒有 jsdom**：prop 拼錯沒有任何閘門擋得住（`frontend/README.md:251`）。判斷邏輯一律抽成
  純函式寫 vitest，元件只負責接線。
- **e2e 不涵蓋工具**：smoke 的 `TOOLS_DIR` 未設定，整合面只靠單元測試。P3 起考慮在 smoke 加一個
  最小工具套件（一個會失敗的 `run.py`），把「失敗 → 紀錄 → 一鍵修正被受理」串起來。
- **D09 例外**：失敗執行的參數與 stderr 會落地 SQLite。`README.md` 要寫明存了什麼、存在哪、怎麼清。
- **釘死字串的測試**：P1 改寫閘訊息會動到既有測試。P2a 若不小心改到交給模型的字串，會有大量既有
  測試轉紅，那正是不變量 F 要的效果。
