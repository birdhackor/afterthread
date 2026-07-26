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

### D40 附錄（P3a review r3）：sidecar 的並行、原子性與 UTF-8 邊界

r2 的「寫檔前再讀一次狀態」與「prompt 建置移到 threadpool」都只做了一半，r3 補齊：

- **互斥**：新增 `tools._META_LOCK`（比照 `llm_log._FILE_SINK_LOCK`：專責一段
  read-modify-write **檔案**序列的鎖），`set_summary_status` 與新的
  `tools.store_summary_meta` 兩個 compound 操作**全程**持鎖。原因是 r2 的
  re-check 本身是 read-then-write：兩者都跑在 threadpool worker 上，PATCH 與
  regenerate 的 store 會真的並行，各自讀到 `draft`、PATCH 寫入 `final`、store 再
  以過期的 `draft` 覆寫——定版被靜默解除，而且凍結的文字正好被它要擋的那次生成
  取代。鎖**不巢狀**（`write_tool_meta`／`read_tool_meta` 維持無鎖，只有兩個入口
  取鎖）、**不跨 await**（兩者皆為同步函式，一律由 `run_in_threadpool` 進入）。
- **不阻塞 event loop**：`regenerate_summary` 與 `generate_and_store_summary` 的
  resolve／read origin／store 三步各自走 `run_in_threadpool`，比照 `routers.tools`
  呼叫 registry 的既有慣例；只有 LLM 往返留在 loop 上。install hook 也照辦——它跑在
  背景 task，與所有 HTTP request 共用同一個 loop，且從 worker 取鎖才不會讓 loop 卡在
  某個套件的 sidecar I/O 上。
- **原子寫入**：sidecar 改為 mkstemp（同目錄）→ write+fsync → `os.replace`
  發佈，比照 `cli.py` init-env 的寫檔路徑。原本走 `_write_regular_file`，它以
  `O_TRUNC` 開**目標檔**，ENOSPC／配額／I/O 失敗時回傳 False 但舊檔已被截斷——
  對**已定版**的總結就是無可回復的資料遺失（sidecar 是 summary 與 origin 的唯一
  副本）。權限刻意不變：`mkstemp` 與原本的 `os.open(…, 0o600)` 同樣受 umask 遮罩，
  兩者在任何 umask 下產生相同 mode（0o077／0o777 皆已驗證），因此**不加** `fchmod`
  ——那會是藉原子性修正夾帶的行為變更。symlink／非 regular file 的拒絕語意以
  寫入前 `lstat` 保留。
  **`set_enabled` 的 manifest 改寫仍維持原本的 in-place 寫入**，這是本階段範圍外的
  既有樣式，屬**有意識延後**而非遺漏：manifest 只帶 `enabled` 一個布林狀態，可由使用者
  重新切換復原，與 sidecar 那份「毀了就沒有第二份」的 LLM 產物不同。
- **UTF-8 兩端防護**：`.ai_meta.json` 可被手動編輯，而 `"\ud800"` 是**合法 JSON**、
  `json.loads` 會產生一個**不可 UTF-8 編碼**的 str。寫入端把 serialize+encode+size
  檢查移進 fail-closed 的 try（原本在外面，PATCH 會 500 而非 False→404），三個文字欄位
  在 dumps 前先 surrogate 洗白（**洗白而非拒絕**：一個壞碼位不該賠掉整份總結，
  U+FFFD 本來就是讀取端會顯示的樣子）；讀取端 `read_tool_meta` 對回傳的每個字串同樣
  洗白（手改的檔案根本沒經過我們的寫入端，否則 GET 會在 Starlette 嚴格 encode 時 500）。
  helper `_utf8_safe` 與 `llm_log._utf8_safe` **重複而不共用**，理由同
  `_REDACTION_MARKER`（llm_log 是 leaf，反向 import 會造成兩邊 docstring 都禁止的耦合），
  以測試釘住兩者行為一致。
- **prompt 順序**：`_summary_user_prompt` 改為「標題→`tool.json`→實作檔→`.env` key
  名→origin/指示/builder 回報」。`llm_prompt_budget_tokens` 的**合法下限就是 4000**，
  而安裝指示可達 20000 字元：舊順序（context 在前）在這組完全合法的設定下，最後的整份
  截斷會切在 context 內，模型收到**零**套件內容，然後憑指示編造文件並被我們寫進 sidecar
  ——對一個系統提示核心規則是「只描述檔案裡看得到的事」的功能，這是最糟的失效。
  現在截斷先吃背景、最後才動到套件本身：截斷可以降低有用程度，但不能拿掉主體。

### D40 附錄（P3a review r4）：URL 憑證改「不留」、遮蔽／截斷移出 validator、alias 列不讀真包狀態

- **URL 裡的憑證用「不留」解，而不是「遮得更用力」**：`origin.openapi_url` 一律先過
  `tool_meta._sanitized_origin_url` 收斂成 `scheme://host[:port]/path`，userinfo／query／
  fragment 整段丟掉並補一個固定可見標記（`_ORIGIN_URL_TRIMMED_MARKER`——讀的人要能分辨
  「本來就沒有 query」與「query 被拿掉了」）；無法解析、沒有 scheme 或沒有 host 則退成
  空字串，**絕不回傳原值**。理由是值比對式遮蔽對 URL 有兩個結構性破口，而且都不是把
  redactor 寫仔細一點能補的：(a) presigned 連結、使用者貼上但從未登記的 token——遮蔽器
  根本不知道那個值；(b) **已登記**的秘密在 URL 裡是 percent-encoded（登記
  `abc123+/XYZ`、URL 寫 `token=abc123%2B%2FXYZ`），exact substring 比對看到的是兩個不同
  字串。D40 本來就裁定 revise／regenerate 都不會重新抓這個 URL（它是出處**顯示**用），
  所以丟掉 query 與認證資訊不損失任何功能。
  **三個套用點，主 choke point 選在 capture**：`tool_builder.run_install` 呼叫 install
  hook 時就收斂，原始字串因此完全不離開那個函式（那份原值本來就只是拿去 fetch 的）；
  另外兩處是 defense-in-depth——`_summary_user_prompt` 的 URL 行（先收斂再遮蔽）與
  `_stored_origin` 讀回既有 sidecar 時（**r4 之前寫下的**或手改的檔案可能還帶著原始
  query，regenerate 會把它讀進 prompt、再寫回磁碟）。**不做資料遷移**：舊 sidecar 照常
  讀得出來，第一次 regenerate 就會順手把 origin 改寫成收斂後的樣子。重複套用是 no-op
  （標記裡不含 `?`／`#`／`@`）。
- **明說的接受邊界（不追編碼）**：安裝指示（`origin.instructions`）是自由文字，其中若出現
  **經過編碼或變形**的秘密（percent-encoding、base64、中間插空白），值比對遮蔽同樣抓不到。
  這裡**刻意不做編碼追逐**：percent／base64／雙重編碼是無底洞，而 URL 能被結構性解決，
  正是因為它有「可以整段丟掉」的部位，散文沒有。指示欄位維持既有防線（登記值的原樣比對、
  寫入端 fail-closed 遮蔽），這是已知且接受的限制。
- **LLM 輸出的遮蔽與截斷移出 pydantic validator**：`ToolSummaryResult._sanitize` 原本在
  `model_validate` 裡跑 `redact_known_secrets`——而 `generate_structured` 的 validate 跑在
  **event loop 上**，那個遮蔽器每次都要掃 tools 目錄（`iterdir`＋每包一次 `stat`，cache
  miss 還要讀 `.env`）：這是在 loop 上做阻塞 I/O，而同一個模組的其他每一步檔案操作都特地
  丟去 threadpool。現在 validator 只驗 **shape**（`_coerce_str`＋非空檢查；lone surrogate
  仍照裁決紀錄 #1 在此拒絕），redact→strip→cap 三步整組搬進 `tools.store_summary_meta`
  （本來就在 threadpool worker 上、本來就持 `_META_LOCK`、本來就是寫檔路徑），順序原封不動。
  **strip 必須一起搬**，這是關鍵而非順手：strip 排在遮蔽之後才對（登記值若自帶前後空白
  ——手改 `.env` 寫成 `KEY=" secret-token "`，python-dotenv 會原樣保留——`strip` 一吃掉邊界
  就再也比不中，剩下的值就裸奔了），把 strip 留在 validator 等於從另一邊把同一個洞打開。
  `_TOOL_SUMMARY_CAP` 常數跟著移到 `tools`（tool_meta 是 importer，留在原處會是循環 import）。
  `write_tool_meta` 對三個文字欄位的遮蔽**維持不動**，仍是其他呼叫端（`set_summary_status`
  round-trip、未來程式化寫入）的最後一道；對已遮蔽文字重跑是 no-op（標記本身沒有可比中的值）。
  遮蔽失敗維持 fail-closed：回 `("not_stored", None)` 而不是丟例外（route 是 map 這個回傳的，
  丟例外會把 404 變 500），且**排在 finalize gate 之後**——「不能做」（409 `tool_finalized`）
  不該被「做不成」（404）蓋過，而且已定版的套件連掃都不用掃。
  `InstallResult._sanitize` 有同型別的 validator 內遮蔽，**有意識延後**不在本階段處理，
  理由見 repo 根目錄 `裁決紀錄.md` #2。
- **列表頁不再從 alias 讀真包的總結狀態**：`list_tools` 原本每一列都跑
  `summary_status(scan.directory)`，而內部別名 `tools/alias -> tools/real` 的
  `scan.directory` 會被 follow——於是 alias 那一列顯示的是 **real 的**「已定版」，但所有
  by-name 總結路由（GET／PATCH／regenerate）對 alias 都回 404，這個 badge 沒有任何請求
  重現得出來，等於在描述另一個套件。改成 alias 列一律 `summary_status = None`
  （`_listed_summary_status`），與 by-name 路由的拒絕語意對齊；該列本身維持
  `valid=False`（`_scan_package` 本來就拒絕 symlink 套件目錄）不變。
