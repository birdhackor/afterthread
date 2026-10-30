# web-v7 計畫：接續修正

起始點：`main` @ `2c35c65`（0.5.0 之後），工作分支 `feat/web-v7`。設計本體是 `docs/web-v7-design.md`，
已處置兩輪設計審查（它的附錄 A、B）。實作期間的裁決記在 `docs/web-v7-decisions.md`，從 D48 起編號。
設計與計畫有出入時，以設計文件為準。

## 階段

| 階段 | 內容 | 規模 | 依賴 | 驗收的不變量（設計 §9） |
|---|---|---|---|---|
| P1 | 停止原因、交接欄位、提示的預算與批次驗證、卡片顯示；另修一則因 CPython 3.14.10 失效的既有測試 | 中 | — | DJ |
| P2 | builder 子行程靜止；完整的內嵌秘密掃描器，發布閘門改用它 | 中 | — | DC；DA 的掃描部分 |
| P3 | 草稿：第一輪的保存、狀態、放棄、sweep、`draft_pending`、工具列與卡片的草稿提示 | 大 | P1、P2 | DA、DB、DD、DG（放棄）、DH、DM、DN、DQ |
| P4 | 接續、從草稿發布的對帳、接續按鈕與附上新紀錄 | 大 | P3 | DD、DE、DF、DG、DI、DK、DL、DM、DO |
| P5 | 發布草稿（不經 AI） | 中 | P4 | DP |

每一階段做完都能單獨使用：

- **P1**：卡片說得出是哪一道上限截斷、要操作者做什麼；模型知道自己的預算。
- **P2**：清理與發布不再與殘留指令競爭；發布閘門看得到長檔尾段的秘密。
- **P3**：沒發布的成果會留下來，可以檢視、到路徑取回、放棄；有草稿時不會悄悄開平行修訂。
- **P4**：接著修。
- **P5**：人接手改完之後，不必再付一輪就能發布。

設計 §10 的「後續」（基底變更資訊與逐檔合併、草稿區的 diff 檢視、逾時前預留收尾）不在本計畫內，
另行裁決。

## 流程

1. 每階段開始記下 `PHASE_BASE`；實作完跑 gates，全綠才 commit。
2. **Gates**：
   - backend（在 `backend/`）：`uv run ruff format --check .`、`uv run ruff check .`、
     `uv run ty check`、`uv run pytest`
   - frontend（在 `frontend/`）：`pnpm lint`、`pnpm build`、`pnpm test`
   - 動到啟動或打包時，加跑 `e2e/smoke.sh`、`e2e/wheel_smoke.sh`
3. **Review**：commit 後以 `PHASE_BASE..HEAD` 跑對抗式 code review（phase-dev 流程），逐條裁決、修正到
   收斂；收斂前做終止複審。駁回與裁決記在 `docs/web-v7-decisions.md`。
4. 全部完成後以 `2c35c65..HEAD` 跑總體審查與全部 gates，再更新 `CHANGELOG.md`（Unreleased）、
   `README.md`、`backend/README.md`、`frontend/README.md`、`docs/tool-calling.md`、文件站。改變行為的
   階段自己更新對應的說明；總體收尾只補遺漏。
5. **新寫的註解、測試名稱與程式輸出的文字**不放會腐爛的指標：審查輪次、finding 編號、寫死的行號、
   以裁決紀錄或計畫當出處。跨檔引用用名字（識別字、檔案路徑、章節標題）。既有程式裡已有的這類指標
   不在本計畫的修改範圍。

## 各階段要點

### P1 停止原因與交接

**範圍**：設計 §5、§4.1。

- `services/llm.py`：`generate_structured` 與 `_run_structured` 加選填 `stats`；記回合數、回應次數、
  收尾觸發、最後的失敗類別（含單一請求逾時與整場期限的區分）。不傳時行為逐位元組不變。
- `services/tool_builder.py`：`ReviseResult`（`handoff`、`operator_action`，先遮蔽再截斷）；
  `run_revise` 傳入 `stats`，結果帶進 `InstallOutcome`／`InstallJob`；提示插入回合數、時間、單一指令
  逾時與對話大小上限，取代 "a couple of minutes"；批次驗證、交接、`operator_action` 的規則；遮蔽標記
  不是真的值。
- `schemas.py`：`ToolJobStatus` 新欄位。
- 前端：卡片的停止原因（含設定名稱）、「需要你動手」、收合的交接筆記；判斷抽成純函式寫 vitest。
- 另一顆 commit：`test_read_tool_meta_survives_pathological_nesting` 的前提在 CPython 3.14.10 不成立
  （大小上限內的最深巢狀可以被解析）。改成：做不出真正的堆疊耗盡就跳過並寫明原因，另加一則以
  monkeypatch 讓解析器擲 `RecursionError` 的測試，讓「降級成 None」仍有守衛。

### P2 子行程靜止與完整掃描

**範圍**：設計 §3.3 第 1 步、§3.3 第 7 步的掃描器、§10 P2。

- `_build_meta_tools` 的 `run_shell`：每個指令在受保護的 task 裡跑，登記 `Popen`；session 被取消時
  殺掉 process group 並等 task 結束。`run_revise`、`run_install` 在清理、保存、發布之前等這些 task。
- 完整掃描器：讀完整個檔案（分段、段間重疊至少最長秘密長度減一）、檔名、連結目標，回傳所有命中；
  `validate_tool_content` 改用它。
- 測試：逾時在 `sleep` 中途 → job 等它被殺才結束；長檔尾段、跨段邊界、檔名、連結目標的秘密 → 發布
  被拒。

### P3 草稿：保存、狀態、放棄

**範圍**：設計 §3.1、§3.3、§3.7、§3.8、§6、§7 的 `ToolSummary.draft`／`DELETE …/draft`、§8 的
草稿提示。

- 新模組 `services/tool_drafts.py`：說明檔的讀寫與形狀驗證、保存管線、狀態推導、放棄、sweep 的草稿
  部分。`tool_builder` 呼叫它；檔案操作都在 worker 上。
- `run_revise`：第一輪結束未發布時保存；`ready=true` 時發布前保存、寫 `publishing`，發布成功後刪除，
  失敗保留（P3 只有第一輪會走到這裡）。
- `start_revise_job`：名額之後的 `draft_pending`。
- 設定：草稿檔案數與總位元組上限。
- 前端：工具列的草稿提示（輪數、停止原因、「需要你動手」、路徑、沒存的檔案）、放棄、卡片的「進度已
  保存／未能保存」；有草稿時修正按鈕停用並說明；各 `draft_*` 碼的純函式與 vitest。
- 文件：README（草稿是什麼、在哪、隱私、怎麼放棄）、`backend/README.md`、文件站、v5 版面的補充。

### P4 接續

**範圍**：設計 §3.2、§3.4、§3.5、§4.2、§7 的 revise 欄位、§8 的接續按鈕。

- 路由：兩次讀說明檔；寬鬆重讀已附的紀錄，嚴格檢查新附的；名額之後的判定表（設計 §3.4）。
- job：
  - `running` 與一輪的收尾；
  - 複製進暫存工作區，用保存的同一條管線；
  - 接續提示；
  - 結束時的保存與 `kept`；
  - `publishing` 與對帳。
- 保存與刪除的授權照 D56：
  - 保存交回三件事：檔案是否存成、這一輪是否已記、說明檔是否確認落盤；
  - 成果沒有人接住，就不清暫存；
  - 上一份檔案目錄先點名，三個條件都確認才刪；
  - sweep 不收沒被點名的 `files-*`；
  - 秘密清單不完整時，說明檔保持原樣；
  - 要發布的保存一律複製。
- 前端：
  - 「接續修正」「加上說明後接續…」「接續修正（附上這筆）」；
  - 草稿目錄與保留下來的舊目錄看得到。
- 前端：卡片上對目前草稿的宣稱與按鈕，只在兩個條件都成立時出現：
  - 清單的草稿與 job 的 `draft_id`、generation 相同；
  - 清單在觀察到 job 結束之後成功重讀過。

  這一輪自己的結果一律顯示。

### P5 發布草稿

**範圍**：設計 §3.9、§7 的 `POST …/draft/publish`。

- 新 job kind `publish_draft`；不呼叫 LLM；發布後照常總結與跑範例。
- 前端：「發布草稿」與確認框，卡片沿用修訂的呈現（含驗證結果）。
- 設計 §6〈跑一次範例〉的卡片提示：草稿有改檔時對應「發布草稿」，沒改檔、只要求動手時提示先跑一次
  範例。後端要給出草稿是否有改檔；P3 的 `ToolDraft` 與 job 的 `draft` 都沒有這項。

## 風險

- **檔案系統的保存管線**是新的 FS 程式，落在全專案審最多輪的區域旁邊：複製、fsync、rename、說明檔
  換新、刪除舊的。P3 的驗收門檻是設計 §9 的 DD、DM（注入失敗、落盤未確認、壞說明檔）。
- **`ToolsPage.jsx` 已超過 2,300 行**：新內容做成獨立元件檔與純函式，既有元件只留掛載點。
- **前端沒有 jsdom**：判斷邏輯一律抽成純函式寫 vitest。
- **P2 改動發布閘門**：長檔尾段含秘密的工具包，從此會被拒絕；這是修正，但要寫進 CHANGELOG。
