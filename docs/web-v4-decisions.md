# Web App v4 設計裁決（接續 web-v3-decisions.md 的 D37）

對應計畫：[`web-v4-plan.md`](web-v4-plan.md)。起始點 `main` @ `fdff6166`。

## D38（P1）：唯讀 Checkbox 改為 Mantine List 非互動清單

快速捕捉「追問」與詳情頁「仍待補齊」原以 `<Checkbox checked={false} readOnly>` 呈現
「唯讀 checklist」——但 Mantine 的 `readOnly` Checkbox 外觀與可互動者相同，使用者會嘗試
勾選並困惑於無反應；且勾選狀態本無任何持久化位置，checkbox 是在暗示不存在的功能。

裁決：改用 Mantine `List`（`@mantine/core` 內建、零新依賴、語意 ul/li），抽成共用
`BulletList` 元件並保留既有 occurrence-based key 邏輯（AI 可能逐字重複同一條目，
bare-value key 會靜默相撞）。捨棄的替代案：
- 讓 checkbox 真的可勾（純前端 local state）——勾選狀態無處保存、重整即失，
  提供的是假功能；
- `Stack` of `Text` 手排 bullet——比語意化 `List` 差，無 a11y 益處。

## D39（P2）：AI 日誌每 attempt 記 `tools_advertised`（名稱清單，非完整 spec）

工具規格走 `chat.completions.create(tools=...)` 獨立參數、不在 messages 內，而
llm_log 只快照 messages 的 role+content——因此日誌看不出模型當輪被廣告了哪些工具，
（含預算耗盡後 tools-free finalize 輪與一般輪的差異）。

裁決：`LlmAttempt` 加 `tools_advertised: list[str] | None = None`；`begin_attempt` 以
keyword-only 預設參數收名稱清單（既有呼叫點零改動）；`None` 表「該 attempt 未送
tools 參數」、非空 list 表當輪廣告的名單。只記**名稱**不記完整 spec：parameters
schema 單顆可達 16KiB、且逐 attempt 重複記錄會放大 ring/JSONL 體積，名稱已足以還原
「模型當時看得到什麼」的除錯問題；需要 spec 細節時 tool.json 就在磁碟上。
`schemas.py:LlmLogAttempt` 必須同步加欄（pydantic 預設 `extra="ignore"` 會在 router
`model_validate` 靜默丟掉新 key），並以 router 層測試釘住。

## D40（P3/P4）：工具總結＋意見修訂＋定版——sidecar `.ai_meta.json`、共用 job 表、promote replace

需求：安裝完成後 AI 產生「做了什麼／原理」總結；使用者可提意見觸發 AI 修訂既有工具包，
迭代至定版。

儲存裁決：總結與狀態存於工具包內 sidecar `.ai_meta.json`（非 SQLite）——工具本體就是
檔案包，sidecar 隨 `delete_tool` 的 `rmtree` 同生滅、對 `_scan_package`（只讀
tool.json）不可見、隱藏檔命名循 `.staging` 的「dot＝內部」慣例。內容含
`origin{openapi_url, instructions}`（install 當下捕捉），讓後續 revise session 有
第一手脈絡（現行 install 不持久化這些）。summary 寫入前一律 `redact_known_secrets`
（fail-closed）：sidecar 會被 revise 的 staging 複製帶入 embedded-secret gate 掃描，
含密文即拒絕 promote。

Revise 裁決：
- 與 install 共用 `_JOBS` 表與 single-flight（任何 queued/running job 擋新 job）——
  單人本機工具，互斥最簡單且免掉同名 workflow 的 log 連結撞名；409 code 用新的
  `tool_job_in_progress`（不重用 `install_in_progress`，避免動既有 FE 分支與 router pin）。
- staging 自既有包 `copytree`，**排除 `.env` 與 `.ai_meta.json`**：安裝包的 `.env` 含
  真密值且該值必在 `known_secret_values` 內，複製進 staging 會被 embedded-secret gate
  必然拒絕——驗證後（validate 判的是 LLM 產物）才回填保留的 `.env` 原文，順序比照
  install 的 `_inject_secret_into_env` 槽位。builder 在 revise 中不得依賴改寫 `.env`
  （prompt 明示；v1 接受此限制，改密值走手動編輯路徑）。
- revise 全程將既有 `.env` 值逐一 `register_inflight_secret`（try/finally discard）：
  promote replace 的隱藏 backup 交換窗內，`known_secret_values` 跳過 dot 目錄，
  不補這手遮蔽會出現空窗。
- promote 增 replace 模式：舊包改名隱藏 sibling `.{name}.bak-<uuid>`（`_scan_all`
  自動不可見）→ move staging →成功刪 backup／失敗移回。與併發 delete 的 race 比照
  install 既有 accepted-risk 準繩（單人本機工具），不另立標準。
- `result.tool_name` 必須等於被修訂的包名（模型改名視同 not-ready 失敗）——
  `InstallResult` 的 `_ready_requires_valid_name` 只驗 regex，不驗身分。
- revise 沿用 workflow 名 `tool_install`（同為 builder session；AI 日誌歸同類），
  summary 生成用獨立 workflow 名 `tool_summary`（同 job 內兩個 session 先後 finish，
  `last_record_id_for_workflow` 靠名字分流才不連錯）。

Summary 生成裁決：install/revise job 內 promote 成功後 best-effort（吞
`LLMNotConfiguredError`/`LLMUpstreamError`/`Exception`——安裝／修訂成功絕不因總結
失敗翻盤）；失敗仍寫 sidecar（空 summary＋origin）供 regenerate。另設同步
`POST /api/tools/{name}/summary/regenerate`（宣告 502/503，test_ai_contract 兩個
exact-set 同步納入此 op；比照 capture 的同步 AI op 慣例）。

定版裁決：`status: "draft"|"final"` 存 sidecar；`PATCH /api/tools/{name}/summary`
雙向切換（可解除定版）；已定版時 revise 與 regenerate 皆 409 `tool_finalized`——
「定版」的意義就是凍結 AI 迭代，解除後才能再改。

路由裁決：job 輪詢改 `GET /api/tools/jobs/{job_id}`（install/revise 共用；原
`/api/tools/install/{job_id}` 移除——FE/BE 同 wheel 出貨無版本偏斜，不留 alias），
schema 改名 `ToolJobStatus` 欄位不變。summary 相關路由不宣告 503
`tools_not_configured`：tools_dir 未設時 `_resolve_package_dir` 回 None → 404 已足，
不動 503 exact-set。`GET /api/tools` 每項加 `summary_status`（列表頁免 N+1）。
