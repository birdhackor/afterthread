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
  `tool_meta._sanitized_origin_url` 收斂成 `scheme://host[:port]`——**r5 再收斂一次，
  path 也丟掉**。userinfo／path／query／fragment 只要存在任何一項就整段丟掉並補一個
  固定可見標記（`_ORIGIN_URL_TRIMMED_MARKER`——讀的人要能分辨「本來就沒有東西可丟」
  與「東西被拿掉了」）；無法解析、scheme 不是 http/https、或 host[:port] 驗不過形狀
  （見下）則退成空字串，**絕不回傳原值**。r4 當時把 path 留下，理由是「這是路由資訊、
  不是憑證」；r5 的 finding 戳破這個假設：矩陣參數（`;jsessionid=<token>`）與閘道／
  代理塞進路徑的能力型 token，跟 query 裡的 token 是同一種操作者文字，只是換了個位置
  ——值比對式遮蔽兩邊都抓不到，理由跟 r4 一致：(a) presigned 連結、閘道能力 token、
  使用者貼上但從未登記的值，遮蔽器根本不知道那個值，不管它在 query 還是 path；
  (b) **已登記**的秘密以 percent-encoding 出現在 URL 的任何部位（登記
  `abc123+/XYZ`、URL 寫 `token=abc123%2B%2FXYZ`，不論這段在 query 還是 path），exact
  substring 比對看到的都是兩個不同字串。D40 本來就裁定 revise／regenerate 都不會重新
  抓這個 URL（它是出處**顯示**用，只要認得出是哪個 host 的文件就夠），所以連 path 一起
  丟掉也不損失任何功能——這是這一類問題結構上收斂到底的終點：host 以外沒有任何使用者
  可控的東西會留下來。
  **host[:port] 本身也驗證形狀（r5，對應 R5-2）**：`urlsplit` 「parse 得成功」不等於
  「netloc 是合法主機」——它完全不檢查 netloc 的字元，所以 `https://Bearer SECRET/openapi`
  這種字串一樣會 parse 出一個非空 netloc（`"Bearer SECRET"`），照 r4 的邏輯會被原樣吐
  回去，直接違背「絕不回傳原值」的保證。現在剝掉 userinfo 之後的 host[:port] 文字必須先
  通過一個嚴格的正規表示式（IPv6 中括號形式，或是網域名／IPv4 合法字元組成的 label，
  後面可選 `:數字` 的 port）才會被吐出，不合就整個退成空字串；scheme 也統一正規化成
  小寫的 http/https，其他 scheme 一樣退成空字串。port 若存在但不是純數字，同樣視為
  「這個 netloc 沒過形狀檢查」而整個退成空字串，不再像 r4 那樣把非數字 port 當成
  「無害的怪東西」放行。
  **三個套用點，主 choke point 選在 capture**：`tool_builder.run_install` 呼叫 install
  hook 時就收斂，原始字串因此完全不離開那個函式（那份原值本來就只是拿去 fetch 的）；
  另外兩處是 defense-in-depth——`_summary_user_prompt` 的 URL 行（先收斂再遮蔽）與
  `_stored_origin` 讀回既有 sidecar 時（**r4／r5 之前寫下的**或手改的檔案可能還帶著
  原始 query 或 path，regenerate 會把它讀進 prompt、再寫回磁碟）。**不做資料遷移**：
  舊 sidecar 照常讀得出來，第一次 regenerate 就會順手把 origin 改寫成收斂後的樣子。
  重複套用維持 no-op，但 r5 換了機制才撐得住：path 還在的時候標記接在真實路徑後面，
  重新 parse 時仍落在 path 裡、原樣通過；path 整段消失後標記會直接黏在裸 host 後面、
  中間沒有 `/` 分隔，若照原邏輯重新 parse 會被當成 netloc 的一部分吃掉、通不過上面的
  host 形狀檢查——所以函式現在會先看字串結尾是不是這個標記、剝掉再解析，剝過就一定
  記得補回去，這才是「重複套用是 no-op」在 host-only 下真正成立的原因。
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

### D40 附錄（P3a review r6）：host 正規表示式再收斂、解除定版不受總結型別拖累、prompt 檔名過 UTF-8 洗白

- **`_HOST_PORT_RE` 中括號分支再收斂**：r5 讓中括號內容放行任何非 `]` 字元
  （`\[[^\]]+\]`），理由是「不平衡的括號已被 `urlsplit` 擋下」——但平衡的括號本身不是形狀
  檢查，CPython 的 `urlsplit` 對 RFC 3986 IPvFuture 形式（`[v1.<任意字元>]`）同樣不驗證
  內容，`https://[v1.Bearer SECRET]/openapi` 因此會 parse 成 netloc
  `"[v1.Bearer SECRET]"`，跟真 IPv6 literal 一樣通過舊分支——與 R5-2 是同一個「parse
  得成功不等於合法主機」的洞，換了字元類別重開。中括號內容改為白名單
  `[0-9A-Fa-f:.]+`（IPv6 literal 實際會出現的字元，含 IPv4-mapped 尾段），zone ID
  （`%25<zone>`）與 IPvFuture 都退成空字串——對 OpenAPI host 而言皆極罕見，安全地跟其他
  不合形狀的輸入同一種拒絕，不在同一分支枚舉第二次。既有合法案例（`[::1]`、
  `[2001:db8::1]:8443`）不受影響。
- **解除定版不再被總結欄位的型別拖垮**：手改成 `{"summary": 123, "status": "final"}`
  的 sidecar 過去無法解除定版——`write_tool_meta` 拒寫非字串 `summary`，這個 False 被
  `set_summary_status` 摺進 `"not_found"`，404 掉唯一能修復這種手改損毀的操作，API 上
  沒有第二條路。解除定版是使用者透過 API 修不了這個欄位時的最後手段，優先權高於型別
  嚴格性：rewrite payload 送進 `write_tool_meta` 前，非字串 `summary` 先摺成 `""`
  （degrade 成「尚無總結」）。此摺平只在非 final 分支生效——定版方向的空值檢查在它之前
  就已擋下並回傳 `no_meta`，型別損毀的總結因此永遠無法被定版；`write_tool_meta` 因其他
  原因（真實寫入失敗）回傳 False 時仍照舊摺進 `"not_found"`。
- **prompt 的檔名 header 補 `_utf8_safe` 洗白**：非合法 UTF-8 的 POSIX 檔名（例如
  builder 的 `run_shell` 以 raw bytes 寫出的檔案）經 `os.walk` 用作業系統自己的
  surrogateescape 慣例解碼後帶著孤立 surrogate，遮蔽器（比對已知密文子字串）碰不到，卻在
  LLM 請求做嚴格 UTF-8 序列化時炸掉——install hook 只能留佔位總結，之後每次
  regenerate 都 502，直到操作者把檔案改名。這是 r3 `_utf8_safe` 處理的同一種檔案系統
  邊界問題換了邊界重演：相對路徑 header 在既有 `redact_known_secrets` 之後、組進
  prompt 之前再過一次 `tools._utf8_safe`（順序不變，洗白只替換 surrogate，不影響比對）。
  查證兩個鄰接面：`_read_regular_file_capped` 讀檔案內容走 `errors="replace"`
  文字解碼，非法位元組當場變 U+FFFD、從不產生 surrogate，內容路徑本就安全；
  `_env_key_names` 的 key 同樣來自這條「內容」讀取路徑（`dotenv_values` 吃已解碼字串，
  非原始 bytes），同樣不帶 surrogate，兩者都不需要另外洗白。

### D40 附錄（P3a review r7）：promote 前清除偽造 sidecar、同狀態 PATCH 不寫檔、發布保留權限

- **sidecar 是後端產物，builder 寫的一律在 promote 前刪掉**：builder session 是半信任
  角色、握有真 shell（D21/H3——嵌入秘密閘門的存在本身就是為了它），因此可以自己組出
  一條完整的走私鏈：先寫 staging `.env` 塞一個**全新、還沒人登記過的**值
  （`known_secret_values` 只掃已安裝套件，且跳過點開頭的 `.staging`，所以整個 build 期間
  這個值都是未知的），再寫 `.ai_meta.json` = `{"summary": "<同一個值>", "status": "final"}`。
  `validate_package` 的嵌入秘密掃描比對不到不認識的值，套件過關；`_promote_staging`
  整包搬進去；接著**安裝 hook 自己的保護反過來幫了攻擊者**——`store_summary_meta` 讀到
  `final`、尊重定版、拒絕覆寫（在 hook 裡是靜默 no-op），於是這個偽造、已定版、帶秘密的
  sidecar 就成為之後每一次 GET／list 原樣吐出的內容。整條路徑完全繞過 `write_tool_meta`
  這個上鎖的 choke point，因為那個檔案根本沒經過它。
  **裁決：刪除，不是拒絕**。sidecar 唯一合法的寫入者就是 `tools.write_tool_meta`；套件本身
  可能是完全合格的作品，而 sidecar 只是裝飾——promote 後幾秒 hook 就會經由正規 choke point
  重新產生一份，所以刪掉不花操作者任何代價，因這件模型自作主張的事去 fail 整個安裝才是懲罰
  錯對象。刪除**排在 `validate_package` 之前**：驗證必須判斷**實際會出貨的東西**，不然閘門
  描述的是一個從未存在過的套件。
  **每一層都刪**（`os.walk`，小樹一趟很便宜）：巢狀的 `sub/.ai_meta.json` 對 `read_tool_meta`
  是惰性的（它只讀套件根目錄），所以那不是走私路徑——但它會跟著進到已安裝套件裡，而未來
  revise 會把整包複製進新的 staging，那份複本**會**被 `validate_package` 的嵌入秘密閘門掃到
  （屆時該值早已登記），等於用一個操作者從沒寫過的檔名把該工具的 revise 永久打死。
  順帶連 `.ai_meta.json*.tmp`（`_write_sidecar_atomic` 的 `mkstemp` 命名空間）一起清，
  那個命名空間同樣屬於後端。刪不掉（怪權限、目錄）就 **fail-closed**：整個安裝以
  category-only 的 zh-TW 錯誤取消——「刪不掉」絕不可以降級成「那就留著」。
- **同狀態 PATCH 是冪等重試，一個位元組都不寫**：`final -> final`（例如回應遺失後
  client 重送）以前會**真的重寫檔案**：繞回 `write_tool_meta`，對**今天**的秘密集合重跑一次
  遮蔽——而那個集合會長大（別的工具剛安裝、`.env` 多了新值），於是幾週前定版凍結的文字裡
  只要出現那個值就會被換成遮蔽標記，`updated_at` 也一併被刷新。這違反「定版後文字不可變」。
  現在 `final -> final` 與 `draft -> draft` **都**在 `_META_LOCK` 內、讀完之後、組 payload
  之前直接回 `"ok"`，不寫檔；兩個方向對稱處理，因為同狀態重寫本來就只能改到
  `status`（已相等）與 `updated_at`（沒人要求改的事實）。**短路排在空總結閘門之後**：
  已定版但總結為空的 sidecar 對重複定版仍回 `no_meta`（那是它今天就有的答案，該閘門本來就
  在任何寫入之前 return），這次修的是「移除一次寫入」，不是放寬閘門。
- **原子發布保留既有檔案的權限位元，read-only 語意的改變則明說接受**：`mkstemp` + `os.replace`
  會把**暫存檔**的 mode 帶上正式檔名，所以操作者 `chmod 0o640`（讓自己的 group 讀得到總結）
  的 sidecar，下一次 PATCH／regenerate 就被默默收回 `0o600`，沒有任何地方回報。現在發布前
  用**同一個** `lstat`（symlink 拒絕本來就要取的那個）拿到 `st_mode`，對暫存 fd `fchmod`
  低 9 位；只複製低 9 位、刻意不用 `S_IMODE` 的 0o7777，setuid/setgid/sticky 不是這種
  best-effort 中繼資料檔該繼承的東西。沒有既有 sidecar 時不複製、維持 mkstemp 預設——保留
  的意思是「不默默改動操作者設定的值」，替新檔案自作主張放寬會是反方向的同一個錯。
  **read-only 檔案的語意確實變了，且不打算改回來**：`_write_regular_file` 的 `O_TRUNC` open
  需要**檔案**的寫入權限，所以 `chmod 0o400` 的 sidecar 會以 EACCES 拒絕；`os.replace` 發布
  需要的是**目錄**的寫入權限，所以現在**會成功**（並把 `0o400` 帶到新檔案上）。那個 EACCES
  是 open 的附帶效果、從來不是設計出來的契約：這個子系統真正支援的保護邊界是**套件目錄**
  （`delete_tool` 整包刪的是它，所有 containment 檢查也都是對它陳述的），「可寫目錄下的
  唯讀檔案」從不在我們承諾過的範圍內。寫在這裡，免得日後被當成 regression 重新推導一次。

### D40 附錄（P3a review r8）：promote 前重驗 staging 根、sidecar 剷除器的 os.walk 改 fail-closed

r7 的剷除器留了兩個更底層的洞，同樣在 `_promote_staging`／`_strip_builder_sidecars`：

- **staging 根本身可被 builder 換掉**：`run_shell` 是唯一不設 jail 的 meta-tool（D21），
  可以把 staging 整個搬走，再在**原本的 staging 路徑**種一個指向 `<tools_dir>` 本身的
  symlink——r7 的剷除器只修剪走訪**途中**遇到的 symlink，從未查過自己的**根**，於是會直接
  走進真正的 tools 目錄，刪光每個已裝套件的 `.ai_meta.json`（毀掉已定版總結、它們唯一的
  原始副本），而且發生在 `validate_package` 之前。裁決：`_promote_staging` 進場第一件事
  改成 `_verify_staging_root`——staging 不得是 symlink（葉節點 `is_symlink()`，不
  resolve），**且** resolve 後仍須落在 resolve 後的 `<base>/.staging` 之內（沿用
  `tools._is_within`，比照 `_resolve_package_dir_no_alias` 的 resolve-then-contain
  慣例）。兩層缺一不可：葉節點檢查擋的是「原地種 symlink」，contain 檢查擋的是葉節點看不
  出來的「祖先目錄被換掉」（`.staging` 本身變成 symlink，底下卻是普通目錄，`is_symlink()`
  看到的是 False）——兩個攻擊形狀與剷除器的 mutation-verify 都各自寫了測試釘住。這與既有
  check-then-move 殘留風險（`_promote_staging` docstring 本來就承認的同名併發安裝那類）
  是同一種折衷，只是往前挪一步：關的是 check-to-walk 之間的瞬間窗，不是整條 builder
  session 的窗。
- **`os.walk` 預設吞掉 scandir 錯誤，剷除器的 fail-closed 因此只顧到一半**：外層
  `except OSError` 只抓得到刪除失敗——子樹讀不到時 `os.walk` 的預設 `onerror=None` 讓它
  靜默跳過、從不進 except。builder 建好巢狀 sidecar 後把父目錄 chmod 000，走訪直接跳過
  整個子樹卻回報成功，偽造的巢狀 sidecar 照樣出貨（mutation-verify 實測：停用這個修復後，
  package 真的被搬進 tools_dir，被鎖住的子樹與裡面的偽造 sidecar 一起出貨）。裁決：
  `os.walk` 傳入一個會**重新拋出**的 `onerror`，讓任何掃不完整的走訪都併入既有的
  fail-closed 路徑——讀不到的子樹不論是蓄意破壞還是單純損毀，兩者都足以中止安裝，沉默才是
  唯一錯誤的選擇。

### D40 附錄（P3a review r9）：cleanup 同樣只在驗證過的 root 上做破壞性刪除

r8 把「破壞性遍歷只能作用於已驗證、containment 約束內的 root」帶進 promote，但
`run_install` 的 `finally` 對 `_cleanup_staging` 的呼叫是無條件的——包括 promote
剛因竄改而拒絕的那條路。CPython rmtree 只拒絕「路徑自身是 symlink」；`.staging`
**祖先**被換成指向外部目錄的 symlink、而外部真有同名 `<uuid>` 目錄時，rmtree 的
leaf 是普通目錄，會穿透連結把外部目錄整個刪掉。修法：`_cleanup_staging` 套用與
promote 同一個 `_verify_staging_root` 閘，拒絕時整個工作區（含 parent shell）原地
保留——被竄改的 workspace 是證據不是垃圾；留下的孤兒與 r8 已釘住的 refused-cleanup
孤兒 symlink 屬同一接受殘餘類。check-to-rmtree 的瞬間窗口沿用既有 check-then-act
accepted residual 準繩。
