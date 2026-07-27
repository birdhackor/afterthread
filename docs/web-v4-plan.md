# Web App v4 計畫（2026-07-26 起）

起始點：`main` @ `fdff6166`（v0.3.0 之後），工作分支 `feat/web-v4`。設計裁決記錄於
[`web-v4-decisions.md`](web-v4-decisions.md)（接續 D38 起編號）。流程沿用 phase-dev：
主代理規劃／裁決，subagent 實作，每 phase commit 後跑 codex adversarial review 至收斂再
push，全部完成後對 `fdff6166..HEAD` 跑 overall review。

## 需求對照

1. 快速捕捉「追問」與詳情頁「仍待補齊」用唯讀 Checkbox，看起來可勾卻不能勾 → **P1**：
   改為誠實的非互動清單樣式。
2. AI 日誌看不出模型當下被廣告了哪些工具（tools 走 API 參數、不在 messages）→ **P2**：
   每個 attempt 記錄 `tools_advertised`。
3. 手動編輯工具即時生效——已確認為既有行為，無工作項。
4. 工具安裝完成後，AI 產生「做了什麼／原理」總結；使用者可提意見觸發 AI 修訂，迭代到
   定版 → **P3**（後端）＋ **P4**（前端）。

## Phase 順序與派工

| Phase | 內容 | 模型 | 狀態 |
| --- | --- | --- | --- |
| P1 | 唯讀 Checkbox → 非互動清單（CapturePage、ItemAiActions） | sonnet | 完成 |
| P2 | llm_log 每 attempt 記 `tools_advertised`＋日誌頁顯示 | opus | 完成 |
| P3a | 工具總結後端（一）：sidecar `.ai_meta.json`、summary 生成、定版＋regenerate API | opus | 完成 |
| P3b | 工具總結後端（二）：revise job、promote replace、job 路由改名 | opus | 完成 |
| P4 | 工具總結前端：ToolsPage 總結面板、意見迭代、定版 | sonnet | 完成 |
| Final | overall review（`fdff6166..HEAD`）＋全 gates＋總結 | — | 進行中 |

## 流程

1. 每 phase：記 `PHASE_BASE` → 派實作 subagent（scope 明確、禁 stash、STOP-and-report、
   物證回報）→ 主代理驗收＋跑 gates → commit。
2. commit 後以 `PHASE_BASE..HEAD` 發動 codex review（detached 啟動、Monitor 輪詢、
   累積式 prompt 附已修 commits 與駁回清單），逐條裁決、修復、重審至收斂。
3. phase 收斂 → `git push origin feat/web-v4` → 下一 phase。
4. 全部完成 → overall review loop → 最終 gates（backend 四項、frontend 三項、
   `e2e/smoke.sh`、`e2e/wheel_smoke.sh`）。

## 各 phase 設計要點

### P1 唯讀 Checkbox → 非互動清單

- 新共用元件 `frontend/src/components/BulletList.jsx`：Mantine `List`（`size="sm"`、
  `spacing={4}`）渲染字串清單；保留兩處現有的 Map occurrence-based key 邏輯（後端可能
  重複同字串）。Mantine `List` 是 `@mantine/core` 內建，零新依賴；語意是 ul/li，誠實
  表達「非互動清單」。
- `CapturePage.jsx` 追問區與 `ItemAiActions.jsx` `GapsChecklist` 換用之，移除不再使用
  的 `Checkbox` import；周邊標題、按鈕、空狀態分支不動。
- 無自動測試渲染這兩處（vitest node-env 無 jsdom；e2e 只驗 API JSON），gates 為
  `pnpm lint` / `pnpm test` / `pnpm build`。

### P2 `tools_advertised`

- `llm_log.LlmAttempt` 加欄位 `tools_advertised: list[str] | None = None`（None＝該
  attempt 完全沒送 `tools` 參數；非空 list＝當輪廣告的工具名）。
- `begin_attempt(self, messages, *, tools_advertised=None)` keyword-only 預設 None——
  既有 6 個呼叫點（tests 含 `_fake_generate`）零改動。
- `llm.py:_run_structured`：`tool_specs` 建好後一次抽出名稱（比照 `_index_tools` 的
  取名方式），每輪傳 `names if advertise_tools else None`。**不碰 `_create_completion`
  kwargs**——`tools`/`max_tokens` 缺席的 byte-identity pin 不受影響。
- `_record_detail` 曝露該欄；`_record_summary` 不動（本來就只有 count）。
- **`schemas.py:LlmLogAttempt` 必須同步加欄**——pydantic 預設 `extra="ignore"` 會把
  新 key 靜默丟掉（router `model_validate` 路徑），必加 router 層測試釘住。
- 前端 `LlmLogsPage` `AttemptCard`：stats 行與「要求訊息」之間插入「廣告工具」區塊，
  用 `TagList` 式 outline Badge 呈現；`tools_advertised` 為 null 時整塊不渲染（舊紀錄
  顯示不變）。
- 文件：`docs/tool-calling.md` §5 加註記；backend README 若列了 attempt 欄位則同步。

### P3 工具總結後端

**拆分為 P3a / P3b 兩個 commit＋review 範圍**（規模與風險面不同，分開審較收斂）：
P3a＝sidecar 檔案 I/O、summary 生成（install hook）、定版 `PATCH`、同步
`regenerate`、列表 `summary_status`；P3b＝revise job、promote replace 模式、
`GET /api/tools/install/{job_id}` → `/api/tools/jobs/{job_id}` 改名與對應 FE 一行。
以下設計要點依此分屬兩個 phase（revise 相關全屬 P3b）。

儲存（sidecar，隨 package 生滅）：

- 每個工具包內新增 `.ai_meta.json`（隱藏檔慣例；`_scan_package` 只讀 `tool.json`，
  sidecar 對 registry 掃描不可見；`delete_tool` 的 `rmtree` 自動帶走）。
- 形狀：`{"summary": str, "status": "draft"|"final", "updated_at": iso, "llm_log_id":
  int|null, "origin": {"openapi_url": str|null, "instructions": str|null}}`。
- I/O 走 `tools._write_regular_file` / `_read_regular_file_capped`（FIFO/symlink 硬化），
  讀取 JSON 損壞視同不存在。**寫入前 summary 一律過 `redact_known_secrets`**（fail-closed：
  遮蔽失敗就不寫）——否則之後 revise 的 staging 複製會被 embedded-secret gate 拒絕。
- sidecar file I/O helpers 放 `tools.py`（與 `.env` 處理同域）；LLM 生成放新模組
  `services/tool_meta.py`（imports tools＋llm，無循環）。

Summary 生成（workflow 名 `tool_summary`，與 `tool_install` 分開，避免
`last_record_id_for_workflow` 撞名連錯）：

- 輸入：tool.json＋實作檔內容（排除 `.env` 與 `.ai_meta.json`；`.env` 只給 key 名）＋
  origin instructions＋builder 自己的 InstallResult.summary。逐檔 redact-then-cap，總量
  受 `token_budget.char_allowance(llm_prompt_budget_tokens)`。
- 輸出模型 `ToolSummaryResult{summary}`：redact→strip→cap（8000 chars）、非空。
- 時機：install job 內 promote 成功後 best-effort 執行（`LLMNotConfiguredError`／
  `LLMUpstreamError`／`Exception` 就地吞掉——安裝成功絕不因總結失敗翻盤）；失敗時
  sidecar 仍寫入（summary 空字串＋origin），供之後 regenerate。

Revise job（與 install 共用 `_JOBS`／single-flight——任何 queued/running job 擋新 job）：

- `POST /api/tools/{name}/revise` body `{feedback: 1..20000}` → 202 `{job_id}`；
  404（工具不存在／tools_dir 未設）；409 `tool_job_in_progress`（不重用
  `install_in_progress`，避免動既有 pin 與 FE 分支）；409 `tool_finalized`（已定版須先
  解除）。**不宣告 502/503**（LLM 失敗封在 job.state，比照 install——test_ai_contract
  的 exact-set 不受影響）。
- 執行：`_resolve_package_dir(name)` 解析 → `_load_tool_dotenv` 取既有 `.env` 值，
  逐值 `register_inflight_secret`（try/finally discard；補 hidden-backup 交換窗的遮蔽
  覆蓋）→ `copytree` 到 `.staging/<uuid>`，**排除 `.env` 與 `.ai_meta.json`**（`.env`
  含真值會觸發 embedded-secret gate）→ meta-tools 重用 `_build_meta_tools(staging,
  secret_env=既有 env 值)` → revise 專用 system prompt（既有包＋使用者意見框架；
  聲明 `.env` 由系統保留、勿依賴改寫）＋ feedback user prompt（不重抓 OpenAPI）→
  `generate_structured(InstallResult, workflow="tool_install"（沿用）, tool_install 的
  rounds/timeout)`。
- 收尾檢查：`result.tool_name == name`（防模型改名越權）→ `validate_package` →
  回填保留的 `.env`（覆蓋 builder 寫的）→ **promote replace 模式**：舊包改名為隱藏
  sibling `.{name}.bak-<uuid>` → move staging 進位 → 失敗 rollback（backup 移回）、
  成功 rmtree backup。與 delete 併發的 race 比照 install 既有 accepted-risk（單人
  本機工具）。
- 成功後同樣 best-effort 重生 summary sidecar（origin 保留原值）。

其他 API（皆不宣告 503 tools_not_configured——tools_dir 未設時走 404）：

- `GET /api/tools/{name}/summary` → 200（sidecar 缺 → 全 null 欄位）；404 工具不存在。
- `POST /api/tools/{name}/summary/regenerate`（同步 LLM，宣告 502/503——
  test_ai_contract 兩個 exact-set 測試同步加入此 op）；409 `tool_finalized`；409
  `tool_job_in_progress`（防與 promote 交換窗互踩）。
- `PATCH /api/tools/{name}/summary` body `{status: "draft"|"final"}` → 200；409
  `summary_missing`（無 sidecar 可定版）。
- `GET /api/tools/install/{job_id}` 改名 `GET /api/tools/jobs/{job_id}`（install／revise
  共用輪詢；schema `ToolInstallJobStatus` 改名 `ToolJobStatus`，欄位不變；FE、README、
  router tests 同步）。
- `GET /api/tools` 每項加 `summary_status: "draft"|"final"|null`（更新
  `test_router_list_tools` exact-dict pin）。

### P4 工具總結前端

- `ToolRow` 加「總結」展開區（沿用頁內既有慣例：inline 展開）：summary 文字
  （`whiteSpace: pre-wrap`）、狀態 badge（草稿／已定版／尚無總結）、「重新產生」
  （sync mutation）、「定版／解除定版」（PATCH）、意見 Textarea＋「送出修訂」
  （POST revise → 沿用 `toolInstall.js` 的 `installJobRefetchInterval`／
  `isInstallJobActive` 純函式輪詢 `GET /api/tools/jobs/{job_id}`）。
- revise job 進行中：該 row 控制項與 install 表單同受既有 gate 管制（同一 job 表
  single-flight，FE 呈現一致）。
- 成功後 invalidate `["tools"]`；install 輪詢 URL 換到 `/api/tools/jobs/{job_id}`。
- 新純邏輯（如狀態→badge 對映）抽到可測純函式，比照 `toolInstall.js` 討論的
  no-jsdom 測試慣例。
- 文件：frontend README 頁面表、root README `/tools` 行、`docs/tool-calling.md`
  §4 加「總結與修訂迭代」小節。
