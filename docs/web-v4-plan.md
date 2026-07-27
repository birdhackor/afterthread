# Web App v4 計畫（2026-07-26 起）

起始點：`main` @ `fdff6166`（v0.3.0 之後），工作分支 `feat/web-v4`。設計裁決記錄於
[`web-v4-decisions.md`](web-v4-decisions.md)（接續 D38 起編號）。流程沿用 phase-dev：
主代理規劃／裁決，subagent 實作，每 phase commit 後跑 codex adversarial review 至收斂再
push，全部完成後對 `fdff6166..HEAD` 跑 overall review。

**這份文件的定位**：它是**計畫**，但**不是歷史草稿**——凡是 review 推翻掉的段落，一律
**就地改成實作真正的樣子，並把「為什麼原句危險」一起寫在旁邊**（沿用「規劃時寫的是
X，實作已改掉」的體例）。理由是這份文件會被當成入口讀，而被推翻的多半正是**安全性
修法**：留著原句、只在開頭掛一張「這是舊計畫」的告示，等於讓照著它重構的人把
`.env` 的寬容讀取、無條件刪除 backup 這類洞原樣裝回去，而告示救不了跳著讀的人。逐條
的最終依據仍是 [`web-v4-decisions.md`](web-v4-decisions.md) 的 D40 附錄；兩邊若有出入，
以那邊為準。

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
- 形狀（**六**個欄位）：`{"summary": str, "status": "draft"|"final", "updated_at": iso,
  "llm_log_id": int|null, "llm_log_process": str|null, "origin": {"openapi_url": str|null,
  "instructions": str|null}}`。**規劃時只寫了五個，`llm_log_process` 是 overall-r2 補上
  的**：AI 日誌的 id 是**每個行程各自從頭配發**的計數器、紀錄環也隨行程結束而消滅，而這
  個檔案把那個整數**永久**存著——重啟之後同一個 id 會解析到現在佔著它的那一筆（別的工具、
  甚至別的 workflow），下游完全分辨不出來。這個欄位是「這個 id 還是我這個行程鑄的嗎」的
  憑據，由 `tools.store_summary_meta` 與 id **在同一處**戳上（沒有 id 就沒有 token，兩個
  欄位不可能各說各話）。照原句去寫 migration、正規化器或重建寫入端的人會把它漏掉，於是
  `routers/tools.py` 的 `_summary_detail` 會把**每一個**存下來的 id 都判為外來的、一律回
  `null`——連當前行程自己還握得住的紀錄，都會失去「查看 AI 日誌」連結。
- 讀取走 `tools._read_regular_file_capped`（FIFO/symlink 硬化），JSON 損壞視同不存在；
  **寫入走 `tools._write_sidecar_atomic`**（同目錄 mkstemp → fsync → `os.replace`，
  寫前 `lstat` 保留 symlink/非 regular 的拒絕語意與既有權限位元）。**規劃時寫的是
  `_write_regular_file`，實作已改掉**：那個 helper 以 `O_TRUNC` 開**目標檔**，
  ENOSPC／配額／I/O 失敗會留下一個被截斷的 sidecar——對已定版的總結就是無可回復的
  資料遺失（D40 r3 附錄）。這裡把計畫改寫成實作，是因為留著原句等於邀請後人「改回
  就地截斷寫入」——那看起來像回到計畫，實際上是拆掉一個已定版總結所依賴的原子發布。
  **寫入前 summary 一律過 `redact_known_secrets`**
  （fail-closed：遮蔽失敗就不寫）。**規劃時給的理由（「否則之後 revise 的 staging
  複製會被 embedded-secret gate 拒絕」）並不成立**：實作的 `_revise_copy_ignore` 在
  任何層級都排除 sidecar 的保留命名空間，`_strip_builder_sidecars` 又會在
  `validate_package` 之前刪掉暫存區裡的 sidecar，所以 sidecar 從來不會走到那道閘——
  這道遮蔽是這個檔案唯一的防線（見 `write_tool_meta` docstring）。
- sidecar file I/O helpers 放 `tools.py`（與 `.env` 處理同域）；LLM 生成放新模組
  `services/tool_meta.py`（imports tools＋llm，無循環）。

Summary 生成（workflow 名 `tool_summary`，與 `tool_install` 分開，避免
`last_record_id_for_workflow` 撞名連錯）：

- 輸入：tool.json＋實作檔內容（排除 `.env` 與 `.ai_meta.json`；`.env` 只給 key 名）＋
  origin instructions＋builder 自己的 InstallResult.summary。逐檔 redact-then-cap，總量
  受 `token_budget.char_allowance(llm_prompt_budget_tokens)`。
- 輸出模型 `ToolSummaryResult{summary}`：validator 只驗**形狀**（`_coerce_str`＋非空）。
  **redact→strip→cap（8000 chars）不在模型上，實作已搬到儲存邊界**
  （`tools.store_summary_meta`，順序不變；D40 r4 附錄）：`generate_structured` 的
  validate 跑在 event loop 上，而遮蔽要掃整個 tools 目錄（`iterdir`＋每包一次 `stat`，
  cache miss 還要讀 `.env`）。`strip` 必須跟著搬——它排在遮蔽之後才對，留在 validator
  等於把同一個洞從另一邊打開（登記值自帶前後空白時，先 strip 就再也比不中）。
- 時機：install job 內 promote 成功後 best-effort 執行（`LLMNotConfiguredError`／
  `LLMUpstreamError`／`Exception` 就地吞掉——安裝成功絕不因總結失敗翻盤）；失敗時
  sidecar 仍寫入（summary 空字串＋origin），供之後 regenerate。

Revise job（與 install 共用 `_JOBS`／single-flight——任何 queued/running job 擋新 job）：

- `POST /api/tools/{name}/revise` body `{feedback: 1..20000}` → 202 `{job_id}`；
  404（工具不存在／tools_dir 未設）；409 `tool_job_in_progress`（不重用
  `install_in_progress`，避免動既有 pin 與 FE 分支）；409 `tool_finalized`（已定版須先
  解除）。**不宣告 502/503**（LLM 失敗封在 job.state，比照 install——test_ai_contract
  的 exact-set 不受影響）。
- 執行：`_resolve_package_dir_no_alias(name)` 解析 → 取既有 `.env` 值，逐值
  `register_inflight_secret`（try/finally discard；補 hidden-backup 交換窗的遮蔽
  覆蓋）→ `copytree` 到 `.staging/<uuid>` → meta-tools 重用
  `_build_meta_tools(staging, secret_env=既有 env 值)` → revise 專用 system prompt
  （既有包＋使用者意見框架；聲明 `.env` 由系統保留、勿依賴改寫）＋ feedback user
  prompt（不重抓 OpenAPI）→ `generate_structured(InstallResult,
  workflow="tool_install"（沿用）, tool_install 的 rounds/timeout)`。
  **規劃時寫的解析器是 `_resolve_package_dir`（會跟隨 alias 的那個），實作刻意改用
  `_resolve_package_dir_no_alias`**：`tools/alias -> tools/real` 這種內部符號連結
  **解析之後仍然落在 tools root 之內**，所以寬容的那個解析器的 resolve-then-contain
  檢查會**通過**。照原句重構的人，會讓一次「對著 alias 發動的修訂」讀到**真包**的內容、
  把它整包餵進一整場 builder session，最後才在換裝的 symlink 閘被拒——一次付了全額又
  丟掉的修訂，而且把「以名字操作卻打到另一個套件」這組語意整個搬回來。拒絕 alias 的
  那個解析器是所有 by-name 路徑（總結讀寫、定版、同步重新產生、修訂）共用的，
  「每一條 by-name 路徑都拒絕內部 alias」因此是由構造保證，不是靠逐處檢查。
  **規劃時寫的 `_load_tool_dotenv`（執行期那個寬容讀取器：讀不到就當沒有 env）
  不是實作採用的入口**：修訂改走 `_read_env_for_values` 這個嚴格讀取器——非
  regular file、超過位元組上限、解析失敗、或值遮不掉（長度下限＋逐行拼法閘，
  D40 r1／r5／r6／r7／r8／r9）一律**入口即拒**，因為修訂會把這個檔案原樣**出貨**，
  而寬容讀取器的「當作沒有」在這裡等於「把一份我們遮不掉的憑證帶進提示與日誌」。
  照原句改回寬容讀取，就是把那組閘門整組拆掉。
  **排除的也不是「所有 `.env`」**：`_revise_copy_ignore` 只排除**根層**的 `.env`
  （而且以目錄列表＋不跟隨的同一實體比對認定，不是比拼法；D40 r5／r6），sidecar 的
  保留命名空間才是**任何層級**都排除。巢狀 `.env` 是套件自己的內容、照複製——工具以
  套件目錄為 cwd，`open("config/.env")` 完全合法，一次「加上分頁」的修訂靜默刪掉它、
  再把殘缺的套件驗證成沒問題，比原規則想擋的事更糟（D40 r2 附錄 R2-2）。
- 收尾檢查：`result.tool_name == name`（防模型改名越權）→ `validate_package` →
  回填保留的 `.env`（覆蓋 builder 寫的；出貨前對**要複製的那些位元組**再跑一次同一套
  `.env` 政策，D40 r7／overall O-1）→ 換裝前**最後一道**是「還是同一個套件嗎」的
  manifest 身分重驗（D40 r10／r11／r12）→ **promote replace 模式**：舊包改名為隱藏
  sibling `.{name}.bak-<uuid>` → move staging 進位 → 失敗 rollback（backup 移回）。
  **成功後不是無條件 `rmtree` backup**：**還有子行程在執行那包的檔案時，backup 改名
  進延後名域 `.{name}.stale-<token>`，由 `_sweep_stale_backups` 之後收**（D40 overall
  r3 O3-1；`delete_tool` 走同一條路，overall r4 O4-2）。實測（Linux/ext4）：改名對
  cwd 停在該目錄的子行程完全無感，`rmtree` 則讓之後每一次相對開檔變成 ENOENT——照原句
  改回無條件刪除，就是把一次進行中的工具呼叫打斷，而模型收到的是「這個動作失敗了」。
  「有沒有人在執行」以**目錄身分**（`tools.directory_identity`）為 key，不是 manifest
  身分（overall r5 O5-1）。與 delete 併發的 race 比照 install 既有 accepted-risk（單人
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
  （POST revise → 沿用 `toolInstall.js` 的純函式輪詢 `GET /api/tools/jobs/{job_id}`）。
  **規劃時寫的 `installJobRefetchInterval`／`isInstallJobActive` 這兩個名字已不存在**：
  安裝與 AI 修訂共用同一張 job 表（`GET /api/tools/jobs/{job_id}`）之後，實作把它們改成
  `toolJobRefetchInterval`／`isToolJobActive`（另有 `TOOL_JOB_POLL_MS`／
  `isTerminalToolJobState`／`toolJobQueryEnabled`）。名字裡的 install 是錯的資訊，不是
  拼字問題——它會讓人以為修訂另有一套輪詢。
- **同步重新產生總結不在那張表裡**（原句把它和安裝、修訂並列成「共用一張 job 表」，是
  錯的）：它是**同步 mutation**，只向同一個**准入**單一飛行取一個名額
  （`tool_builder.reserve_sync_operation`，與 `_admit_job` 共用 `_JOBS_LOCK`／`_SYNC_OPS`），
  **不建立 job**、回應是 `ToolSummaryDetail`（沒有 `job_id`）、也永遠不進 `toolJob*` 那套
  輪詢。共用的是**名額**，不是 job 表——照原句去實作 regenerate 客戶端的人，會去等一個
  永遠不會來的 job id。
- revise job 進行中：該 row 控制項與 install 表單同受既有 gate 管制（同一 job 表
  single-flight，FE 呈現一致）。
- 成功後 invalidate `["tools"]`；install 輪詢 URL 換到 `/api/tools/jobs/{job_id}`。
- 新純邏輯（如狀態→badge 對映）抽到可測純函式，比照 `toolInstall.js` 討論的
  no-jsdom 測試慣例。
- 文件：frontend README 頁面表、root README `/tools` 行、`docs/tool-calling.md`
  §4 加「總結與修訂迭代」小節。
