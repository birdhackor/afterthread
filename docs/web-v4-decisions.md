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

### D40 附錄（P3a review r10）：reserved sidecar 名稱大小寫不敏感比對

`_is_reserved_sidecar_name` 原為大小寫敏感比對。macOS 預設檔案系統
**大小寫不敏感**（本專案支援 macOS），builder 寫的 `.AI_META.JSON` 在該平台
**就是**日後 `read_tool_meta` 以 `.ai_meta.json` 開啟的同一個檔案——大小寫敏感的
剷除器會直接放行，r7-1 關掉的 choke-point bypass 在受支援平台上整個重開。比對
改為 `casefold()`（Unicode 正確的 full case folding，且這些名字只比對不顯示）：
剷除的寬鬆度必須至少等於它可能執行的最寬鬆檔案系統；在大小寫敏感的檔案系統上，
代價僅是順手刪掉一個 builder 本來就無權寫入的異體大小寫檔名。

另記：同輪的「搬移整個 tools_dir 造成信任錨漂移」屬 D21 威脅模型範圍外，已駁回，
理由見 `裁決紀錄.md` #5。

### D40 附錄（P3a review r11）：sidecar 權限下限——writer-accepts ⇒ reader-reads-back 也適用於權限

`_AI_META_MAX_BYTES` 把「寫得進去的一定讀得回來」立成 **大小** 的不變量；r11 指出
**權限** 上同一句話不成立：`mkstemp` 的 `0o600` 會被 process umask 遮罩，服務若在
會遮掉 owner 位的 umask（維運者的 `0o277`、包裝腳本的 `0o777`）下啟動，發佈出來的
sidecar 下一次 `read_tool_meta` 就打不開——`write_tool_meta` 回 True、之後每次 GET
都說「沒有總結」、每次 regenerate 都花掉一次 LLM 呼叫改寫一個它讀不回來的檔案。
r7-3 的「原樣繼承既有 mode」也會把這個狀態永久傳遞下去。

修法：新增 `_OWNER_RW = 0o600` 下限，發佈前一律 `fchmod(fd, 繼承 | _OWNER_RW)`——
維運者的 group/other 自訂完全照 r7-3 承諾保留，唯獨 owner 讀寫不可讓渡：sidecar 是
**後端自有狀態**，不是維運者內容。這也修正了 r7-3 當時「不加 fchmod」的判斷：那時
的理由是「別藉原子性修正夾帶行為變更」，現在有了明確的不變量理由，是有依據的
重新裁決而非推翻。

### D40 附錄（P3b 自我審查）：換裝前再檢一次定版

P3b 的 revise 在**入口**檢查定版（router 回 409、`run_revise` 再防一次），但一次
revise session 以**分鐘**計，使用者在期間按下定版是再普通不過的事——入口檢查看不到
它。少了最後一刻的重檢，換裝會把整包（含已定版 sidecar 的凍結文字）換掉，而且
sidecar 本來就被排除在 staging 外、換裝後才以 **draft** 重生：定版會被一次比它更早
開始的 session 靜默解除。因此 `_promote_staging_replace` 在 swap 前再讀一次
`summary_status(target)`，`final` 即拒絕——與 `store_summary_meta` 對 regenerate 做的
store-time 重檢（D40 r2 附錄）同一個模式、同一個理由，只是時間尺度大得多。入口檢查
維持不變：它的價值是讓**路由**能在不燒 LLM 呼叫的情況下回 409。

### D40 附錄（P3b review r1）：太短的 `.env` 值拒跑、換裝改純 rename、修訂全程不在 loop 上遮蔽、`.env` 保留改口徑為「文字」

- **`.env` 內有無法遮蔽的值就整場拒絕（R1-1）**：遮蔽器（`tools.redact_known_secrets`
  與 `llm_log._redact`）刻意略過 `_MIN_SECRET_LEN`（6）以下的值——遮一個 1–5 字元的值
  會把散文洗爛。安裝**表單**因此本來就擋短秘密（`schemas._SECRET_VALUE_MIN_LEN`），
  但**修訂繼承的是磁碟上的東西**：手編的 `PIN=1234` 照樣被 `register_inflight_secret`
  登記，而登記對這種值等於沒登記；接著它進了 `run_shell` 的環境，builder 一句
  `echo "$PIN"` 就讓 `1234` 原樣回到工具結果 → 下一輪提示 → AI 日誌 attempt bodies →
  甚至 `InstallResult.summary` → job 輪詢 → sidecar。**裁決：`run_revise` 在 LLM 呼叫
  之前檢查解析出來的既有 `.env` 值，只要有任何非空值短於 `_MIN_SECRET_LEN` 就回
  category-only 的 zh-TW 失敗**（`_ERROR_REVISE_ENV_UNMASKABLE`），訊息只講**條件**與
  兩條補救（加長，或把它移出 `.env`），**不提 key、不提值**——在一則因為「遮不掉」而
  拒絕的訊息裡把值印出來，就是它要防的那個洩漏。誠實的選項只有「不要跑」與「洩漏」
  兩個，這裡選前者，且這正是安裝表單那條規則套在另一個入口。空值（`KEY=`）不算秘密
  （比照 `_cached_env_values` 的既有規則，本來就不登記），所以絕不會因此把一個套件
  永久打死。**安裝路徑沒有同款風險**（已查證）：install 的 staging 是**空的**
  （`staging.mkdir`）、`_promote_staging` 又拒絕已存在的名字，所以它從不繼承既有套件的
  `.env`；它唯一寫進 `.env` 的值是表單秘密，schema 早就設過同一個下限。
- **換裝的就位改成純 `os.rename`（R1-2）**：`shutil.move` 只要 `os.rename` 丟出
  **任何** `OSError`（不只跨裝置的 EXDEV）就退化成 copytree＋刪除，所以原本的就位
  **不是原子的**：舊包已經改名進隱藏備份、複製到一半失敗，工具的名字上就留下一個
  半套目錄，接著回滾的 `os.rename(backup, target)` 因為名字被佔住而失敗——最後是
  「半換好的工具＋一個隱藏備份」，正好是 `_ERROR_REVISE_UNRECOVERABLE` 存在要讓它
  罕見的那個狀態。改成 `os.rename(staging, target)` 之後，失敗就代表**什麼都沒搬**，
  名字仍然空著，備份一定回得去。同檔案系統的前提在這裡是**結構性成立**而不是祈禱：
  staging 是 `<tools_dir>/.staging/<uuid>`、target 是 `<tools_dir>/<name>`，而
  `_verify_staging_root`（r8）已經證明 staging resolve 後確實落在同一個 `<tools_dir>`
  殼裡。就位外面那圈 `except Exception` 維持**全捕捉**：那個窗口裡工具不存在，任何形狀的
  失敗都必須回滾。**回滾刻意不先清掉 `target`**：純 rename 失敗不會留下半成品，唯一能讓
  `target` 存在的情況是有另一個持本服務 uid 的行為者在我們改名之後那一瞬間建了它，
  而對一個本函式既沒建立也沒驗證過的目錄做 `rmtree` 是拿別人的資料換一個名字——
  讓 rename 以 ENOTEMPTY 失敗、告訴操作者備份在哪裡，才是「兩個寫入者搶一個名字」的
  誠實答案。安裝路徑的 `_promote_staging` 仍用 `shutil.move`（不同前提：target 必須
  不存在），本輪不動。
- **修訂全程不在 event loop 上遮蔽／組提示（R1-3）**：`_revise_user_prompt` 與事後的
  summary 遮蔽都會呼叫 `redact_known_secrets` → `known_secret_values`，那是整個 tools
  目錄的 `iterdir`＋每包一次 `stat`、cache miss 還要讀 `.env`。修訂跑在**背景 job**、
  與所有 HTTP request 共用同一個 loop，理由與 P3a 把 `tool_meta._summary_user_prompt`
  搬進 `run_in_threadpool` 完全相同。三處全搬（提示建置、summary 遮蔽、以及模型改名
  拒絕分支裡的 `attempted` 遮蔽）——只修兩處會讓同一個類別從第三處長回來。
  fail-closed 的傳播語意不變：遮蔽器丟出的例外照樣從 `await` 冒出來，落到 `_run_job`
  的 backstop。**`run_install` 的同型別呼叫（`_builder_user_prompt`、summary 遮蔽）
  本輪不動**：那是既有面（早於 P3b），與 `裁決紀錄.md` #2 記的 `InstallResult._sanitize`
  validator 內遮蔽同一族；一併搬移屬 installer 的整體改造，記在這裡以免日後誤判為遺漏。
- **`.env` 保留的口徑從「逐位元組」改成「文字」（R1-4）**：原本的說法在字面上不成立
  ——`_read_regular_file_capped` 以 `errors="replace"` 與 universal newlines 解碼，CRLF
  的 `.env` 讀回來是 LF、非法位元組變 U+FFFD，`_write_regular_file` 再重新編碼。
  **選擇不改成 raw bytes**（(a) 案），理由不是省事：真正該問的是「有誰看得出差別」，
  而**這個檔案的每一個消費者都走同一條有損解碼**——runtime 的 `_load_tool_dotenv`、
  遮蔽器的 `_cached_env_values`、以及這裡的保留讀取——所以修訂前後工具實際載入到的值
  本來就完全相同，保留位元組買不到任何可觀測的保真度，卻要嘛複製一整套 O_NOFOLLOW／
  O_NONBLOCK／S_ISREG 的開檔加固，要嘛去動兩個最多人共用的檔案 helper 的契約（等於
  多一條寫入路徑，正是本 finding 自己劃掉的選項）。**改的是說法，不是行為**：docstring、
  README、tool-calling.md 與那個叫 byte-for-byte 的測試全部改口為「以文字保留：內容與
  行序原樣，換行正規化與非法位元組替換沿用共用受限讀取器」，並新增一個測試釘住 CRLF
  進去、LF 出來，**同時**釘住 `_load_tool_dotenv` 讀到的值不變——把 transform 寫下來，
  而不是假裝它不存在。（**r2 推翻**：見下一節 R2-1，這條的前提事實上不成立。）

### D40 附錄（P3b review r2）：`.env` 改成複製檔案、只排除根層 `.env`、stat 失敗不等於不存在、系統提示只遮動態插值

- **`.env` 保留改成「複製檔案」，推翻 r1 的文字往返（R2-1）**：r1 之所以敢留下有損
  往返，靠的是「**這個檔案的每一個消費者都走同一條有損解碼**」這個前提——而它是**假的**。
  runtime 執行工具時以**套件目錄本身**當工作目錄（`_BUILDER_SYSTEM_PROMPT` 明文的執行
  契約），所以工具自己的 entry 大可 `open(".env", "rb")` 去雜湊它、diff 它、自己處理
  CRLF；維運者也可能用 checksum 盯著這個檔案。一次只想加分頁的修訂把 CRLF 壓成 LF、
  把非法位元組換成 U+FFFD，就是在改一個沒人請它改的檔案。**修法**：`_promote_staging_replace`
  在 `validate_package` 通過、且目標的存在／symlink／定版檢查全過之後、換裝之前，用
  `shutil.copy2` 把**正式套件的 `.env` 複製進 staging**（新的 `_preserve_env_file`）。
  copy2 全程不解碼，位元組連同 mode、mtime 一起過去；方向是「正式包 → staging」，正式包
  只被**讀**，所以後面換裝失敗一點代價都沒有——原檔還是原檔，不是它的重寫版。它排在
  所有目標檢查**之後**，因為它是唯一會伸手去讀正式包的步驟。
  - **兩端都用 `lstat` 把關**，比照 `tools._write_sidecar_atomic` 的寫前 `lstat` 與共用
    檔案 helper 的 `O_NOFOLLOW`＋`S_ISREG`：**來源**只有 `FileNotFoundError` 算「沒有
    `.env`」（見 R2-3），非 regular 的來源（session 期間被換成 symlink／FIFO）拒絕而不是
    穿過去讀；**目的地**（staging 的 `.env`）ENOENT 是常態、regular file 是模型自己寫的
    那份（提示的契約就是覆蓋它），其餘一律拒絕。目的地那道**不是對稱美學**：`copy2` 會
    **跟隨**目的地，未上鎖的 `run_shell`（D21）在 `<staging>/.env` 種一條 symlink，就會
    把**正式的憑證**沿著它寫到 staging 外面（測試實測：不加這道，`_TRICKY_ENV` 的內容
    整段落到 staging 外的檔案），目的地是目錄時則會寫成 `<staging>/.env/.env`、發佈出一個
    根本沒有 `.env` 的套件。這道守的是**與被它取代的 `tools._write_regular_file` 完全同一組
    拒絕**——不多（hardlink 兩者都放行，屬 D21 同 uid 的既有殘留），不少。
  - **複製的是「換裝那一刻磁碟上的那個檔案」，不是開場讀到的快照**：維運者在 session
    中途改了 `.env`，他的修改被**保留**而不是被修訂回捲。這也不會外洩：只有開場讀到的值
    被登記／匯出到 `run_shell`，中途新增的值從來沒進過對話，換裝後 `known_secret_values`
    再掃一次就重新認得。
  - **兩件事在程式碼裡刻意分開**：`_read_env_for_values`（原 `_read_env_for_preservation`，
    一併改名）只負責**解析值**——登記 in-flight 秘密、餵 `run_shell` 的 `secret_env`——那是
    文字讀取，維持原樣；**檔案**由 `_preserve_env_file` 複製。`_promote_staging_replace` 的
    `preserved_env_text` 參數因此消失。`_ERROR_REVISE_ENV_TOO_LARGE` 的理由也跟著改成成立的
    版本：超過上限的 `.env` 解析出來是**被截斷的值集合**，尾巴那些值不會被登記，而複製過去
    的檔案照樣帶著它們——不是原本說的「寫回截斷內容會毀掉憑證」（現在根本不寫回）。
- **copytree 只排除「根層」的 `.env`（R2-2）**：r1 的 ignore 在**每一層**排除所有
  casefold 等於 `.env` 的名字，於是一個工具自己會讀的 `config/.env`（它的 entry 就是以
  套件目錄為 cwd 執行的）會被一次無關的修訂**靜默刪除**，而 `validate_package` 接著對這個
  被截肢的套件**驗證通過**。根層那份仍然要排除（大小寫不敏感比對保留：case-insensitive
  檔案系統上 `.ENV` 就是那個受管檔案——**r5 修正**：改成比對 inode 而非拼法，見下方
  R5-2；**r6 再修正**：inode 是錯的鑑別特徵，改以目錄列表認定，見下方 R6-2），巢狀的則是
  **普通套件內容，必須複製**。誠實記下
  代價：巢狀檔案若內嵌了**已登記**的秘密值，`validate_package` 的內嵌秘密閘會用它既有的
  訊息擋下這次 promote——那是閘門在做它的工作（維運者因此知道自己把 live 憑證放進了第二個
  檔案），而且嚴格優於「靜默出貨一個少了檔案的套件」。sidecar 的保留命名空間**維持每一層
  都排除**（後端自有命名空間，不變）。順帶一提，巢狀 `.env` 從來就不是「已知秘密」
  （`known_secret_values` 只讀每包**根層**的 `.env`），而 builder 本來就能用未上鎖的
  `run_shell` 讀正式包裡的它——所以這條沒有打開任何新的暴露面；`tool_meta._package_files`
  也在每一層跳過 dot-開頭的名字，總結提示照樣看不到它。
- **`stat` 失敗不等於「沒有 `.env`」（R2-3）**：`_read_env_for_values` 原本 `except OSError`
  一律當成「檔案不存在」，於是磁碟 EIO、NFS ESTALE、維運者剛 chmod 出來的 EACCES 都被讀成
  **不存在**——接著整場修訂就在「工具的真實憑證沒被登記、也沒匯出」的狀態下跑完一次
  builder session。**只有 `FileNotFoundError` 代表不存在**，其餘 `OSError` 一律走既有的
  「讀不到就整場拒絕」路徑（R1 已定的規則：降級過的 `.env` 絕不出貨）。同一套判別也套用在
  R2-1 新增的複製步驟上（來源不存在＝沒有 `.env`，其餘一律拒絕）。
- **修訂的系統提示只遮「動態插值」（R2-4）**：系統提示原本整段未經遮蔽送出，而它會把
  **套件名字**插進去；`.env` 裡若有一個值剛好等於那個名字，它就這樣搭便車進了請求。修法是
  在代入前對 `name` 做 `redact_known_secrets`（且和其他兩處一樣**在 threadpool 裡**做，因為
  它是整個 tools 目錄的掃描——R1-3 的同一條規則），**而不是遮整段提示**。理由要講精確：
  我們的靜態提示文字是**後端自己寫的、開源的常數文字**，遮它既沒用也有害——沒用是因為
  「一個剛好等於已公開常數文字的值」根本不是模型從我們這裡學到的；有害是因為遮蔽器對
  `_MIN_SECRET_LEN`（6）以上的值一律比對，手編的 `TOKEN=secret` 會把我們自己那句
  「Never write a secret value into any file」洗成一排遮蔽標記，模型讀到一份有破洞的指令。
  **只有操作者／檔案系統來源的插值該遮**，這段 addendum 剛好只有一個。**連帶後果照單全收**：
  套件的**名字本身**若就是已登記的秘密值，模型被要求回報的是**被遮過的**名字，於是後面的
  同一性檢查必定失敗、整次修訂被拒——對一個「秘密同時也是公開工具名」的設定來說這是對的，
  因為那個值本來就已經在工具列表、job 輪詢與前端網址裡了。

### D40 附錄（P3b review r3）：`.env` 中途被刪不補 placeholder、換裝前的定版重檢改 fail-closed

- **開場有、換裝時沒有的 `.env`，不讓模型的 placeholder 頂替（R3-1）**：`_preserve_env_file`
  原本把「來源不存在」一律當成「沒有東西要保留」直接回成功，於是 builder 寫在
  `staging/.env` 的那份就這樣發佈出去。r2 立下的接受條款是「**從來沒有** `.env` 的套件
  可以收下 builder 寫的那份」（比照 install，寫 `.env` 本來就是指示的一部分，否則
  「把金鑰存進 .env」這種意見修訂根本做不到），它**不涵蓋**「開場明明有、session 中途被
  刪掉」的情況——後者是維運者對**正式套件**的一個刻意動作（他把憑證撤掉了），而 builder
  那份檔案是一個**叫它別寫 .env 的提示**底下產生的副產物；讓 placeholder 悄悄補上一個
  被刪掉的憑證檔案的位置，正是這條 finding 指名的失效。**修法**：把 session 開場的觀察
  一路傳下去。`run_revise` 讀值時（`_read_env_for_values`）本來就知道有沒有這個檔案
  ——回傳 `(None, None)` 就是「沒有」，空檔案仍會回 `""`——把這個 bool
  （`env_existed_at_start`）經 `_promote_staging_replace` 傳進 `_preserve_env_file`，三分支：
  開場有＋現在有 → 照舊複製；開場有＋現在沒有 → **刪掉 staging 裡 builder 留下的
  `.env`**，發佈出來的套件就沒有 `.env`，與那次刪除所要求的一致；開場沒有 → 完全不變。
  刪不掉就回 category-only 錯誤（`_ERROR_REVISE_ENV_DISCARD`）並在換裝**之前**中止——
  「我們沒辦法讓修訂結果符合維運者的要求」絕不能收斂成「那就照樣發佈 placeholder」。
  `os.remove` 不跟隨 symlink（種在那裡的連結是被 unlink 而不是被穿過去），`.env` 是**目錄**
  時則落到同一個拒絕分支：對一個本函式既沒建立也沒驗證過的路徑做破壞性遍歷，是回滾那段
  已經拒絕過的事（R1-2 同一條理由）。**參數刻意設成必填的 keyword-only**：給它預設值，
  就是替「以後有人忘了傳」預留一條走錯分支的靜默路徑。
  **鏡像情況不需要旗標、也不改**：開場沒有、中途被維運者**新增**的 `.env` 照樣被複製——
  這正是 r2「複製的是換裝那一刻磁碟上的那個檔案」的規則往另一個方向套；`existed_at_start`
  只在「結尾也沒有」時才決定事情。
- **換裝前的定版重檢改成 fail-closed（R3-2）**：P3b 自我審查加的那道重檢是
  **fail-open** 的——`tools.summary_status()` 把「沒有 sidecar」與「有 sidecar 但讀不到／
  壞掉／EIO／被換成 symlink」全部折成同一個 `None`，而閘門只在明確等於 `"final"` 時才拒絕。
  於是：使用者在 session 中途定版、sidecar 隨後遇到一次暫時性讀取失敗 → 換裝照樣進行 →
  整包被換掉、凍結的文字連同 sidecar 消失、事後 hook 再生一份 draft。**這道閘門存在的
  理由，恰好被它自己的失敗方式繞過。** **修法**：在 `tools.py` `summary_status` 旁邊加一個
  更嚴格的讀取器 `summary_status_or_unknown`，把 `FileNotFoundError` 與其餘所有失敗分開
  （與 `_read_env_for_values` 的 R2-3 判別同一套），回 `"draft"`／`"final"`／`None`（**確定**
  沒有 sidecar）／`_SUMMARY_STATUS_UNKNOWN`（有 sidecar 但讀不出可信狀態——讀取器拒絕、
  超過上限、不是 JSON、不是物件、或 `status` 不在 `_SUMMARY_STATUSES` 內；`write_tool_meta`
  這五種都寫不出來，所以每一種都代表手改或損壞）。`_promote_staging_replace` 對
  `final` **與** `unknown` 一律拒絕。
  **`summary_status` 自己的契約刻意不動**：`list_tools`（一列 badge，壞 sidecar 該降級一列
  而不是 500 整個工具頁）與兩條 summary 路由（決定要不要燒一次 LLM 呼叫，猜「沒定版」的
  代價是一次 regenerate，而它本來就會**覆蓋**那個讀不到的檔案）都真心需要那個 total 的行為。
  新讀取器是**多一個**、更嚴格的讀取器，只給那一個猜錯會**毀資料**的呼叫端——它接下來要
  刪掉 sidecar 所在的整個套件。
  **拒絕訊息用新的一條而不是重用 `_ERROR_REVISE_FINALIZED`**：後者帶著一個**指示**
  （請先解除定版），而在「讀不到」的情況下那個指示是錯的——可能根本沒有東西被定版，
  照著做只會讓操作者一直切換一個不是問題的狀態，而每次重試都因為訊息從未講出的理由
  再拒絕一次。兩者的補救方式不同（修好／移除損壞的 sidecar vs. 解除定版），訊息就必須不同，
  這與 `_ERROR_REVISE_TARGET_MISSING`／`_ERROR_REVISE_TARGET_ALIAS` 是同一種「同一個拒絕點、
  不同的可行動原因」的拆法。
- **順帶修掉一處 docstring 與程式碼的矛盾**：`run_revise` 的 docstring 仍寫著「中途定版
  **不會**在 promote 時重檢，屬既接受的 check-then-act 殘留」——那句話在 P3b 自我審查加上
  重檢的那一刻就不成立了，修復時漏改。本輪一併改正。

### D40 附錄（P3b review r4）：定版閘真的排到最後、`.env` 複製加上限、origin 只讀一次、dotenv 解析進 threadpool

- **`.env` 複製移到定版閘之前，複製本身也加大小上限（R4-1）**：r3 把重檢改成 fail-closed，
  但它**不是最後一步**——`_preserve_env_file` 的 `copy2` 排在它後面，而那一步的**時間長度
  是外面的人決定的**（維運者可以在 session 中途把 `.env` 換成一個任意大的普通檔案）。
  於是「定版落在複製進行中」這件事，被一個**已經跑完**的閘門完全看不到，換裝照樣把
  已定版的整包蓋掉。既有裁決接受的 check-then-act 殘留講的是**兩個 syscall 之間的瞬間**，
  不是一段可以任意拉長的複製。**修法有兩半，缺一不可**：(a) **順序**——`.env` 複製提前到
  目標存在／symlink 檢查之後、定版閘**之前**。這樣做零代價，因為它寫的東西全部落在
  **staging**，沒有任何其他角色在看；閘門之後若拒絕，那份 staging `.env` 就跟著被丟棄的
  build 一起消失。它依賴的前提（`target` 是真的已安裝目錄）由留在它前面的兩道檢查提供。
  (b) **上限**——用本來就取的 `lstat` 的 `st_size`，來源大於 `tools._ENV_FILE_MAX_BYTES`
  就以 category-only 的 `_ERROR_REVISE_ENV_TOO_LARGE` 拒絕。理由不是省時間：**超過上限的
  `.env` 本來就解析不出值**（`_read_env_for_values` 開場就會拒絕、`tools._load_tool_dotenv`
  在 runtime 直接降級成沒有 env），複製它等於發佈一個系統其他部分都不接受的檔案。
  兩半合起來，換裝前剩下的窗口就只剩「`lstat` 與 `copy2` 之間」那個瞬間——與目標檢查
  帶的那些同一種、同一個尺寸，正是既有裁決接受的那一類。順帶更正 `_promote_staging_replace`
  docstring 裡「不需要再檢查大小」那句：它的前提是「複製的還是開場解析過的那個檔案」，
  而中途被換掉正是這個函式必須撐住的情況。
- **origin 改成「閘門讀到什麼就用什麼」，只讀一次（R4-2）**：`_existing_origin` 用的是
  **total** 讀取器（所有錯誤 → None），而換裝前的閘門用的是 **strict** 那個。於是一次
  暫時性 EIO 就會讓 origin 變成 None、strict 讀取隨後成功看到 `draft`、換裝照跑、舊
  sidecar 隨舊包一起死、重生的 sidecar 帶著 `origin=None`——**OpenAPI URL 與原始安裝指示
  的唯一副本就此永久消失**（沒有第二個地方存它們）。**修法採 (a) 案**：`tools.summary_status_or_unknown`
  改回傳 `(status, meta)`，把它剛剛判讀過的 meta 一併交出；`_promote_staging_replace`
  改回傳 `(origin, error)`（與 `_read_env_for_values` 同款的 `(value, error)` 形狀），
  origin 由 `_existing_origin(meta)` 從**同一次**讀取收斂出來。`_existing_origin` 因此
  改成吃 meta 的純函式。**兩個失敗答案都不交出 meta**：ENOENT 沒有東西可交，UNKNOWN 的
  整個結論就是「這個檔案不可信」，把內容交出去等於邀請呼叫端使用它剛剛拒絕相信的東西。
  **不再保留 unreadable 分支**：閘門已經對每一種讀不到的形狀拒絕過了，所以走到收斂
  origin 那一行時，`meta is None` 只可能代表**確定沒有 sidecar**（本來就沒有東西可繼承，
  照舊以 origin=None 繼續）——這一點寫進 docstring，而不是留一個沒有解釋的多餘分支。
  `summary_status` 的 total 契約**仍然不動**（list_tools 的 badge、兩條 summary 路由的
  409 判斷、`run_revise` 的入口閘），r3 的理由原封不動成立；narrow 的那三行抽成
  `_narrowed_summary_status` 由兩個讀取器共用，好讓 strict 那個能一次讀完就交出 meta。
- **`.env` 的值解析一併進 threadpool（R4-3）**：r1 把**讀檔**搬進 `run_in_threadpool`，
  但 `python-dotenv` 的**解析**還留在 event loop 上——一份接近 64 KiB、引號很多的 `.env`
  解析起來是真的工作，而 revise 跑在背景 job、與所有 HTTP request 共用同一個 loop，
  理由與 R1-3 完全相同。`_read_env_for_values` 因此改成 `(existed, values, error)`：
  讀與解析在**同一個** worker hop 內完成，loop 上不再有任何 dotenv 工作，呼叫端拿到的
  仍然是「存在旗標＋值」這組既有契約。**`existed` 一律取自讀取、不取自解析結果**：
  只有註解的 `.env` 解析出來是 `{}`，與「根本沒有 `.env`」的 `{}` 一模一樣，但兩者在
  R3-1 的判斷裡意義相反——這條以測試釘住。（**r5 微調**：同一個 hop 另外把**原始文字**
  一併交出，供 R5-1 的拼法閘比對，見下節。）

### D40 附錄（P3b review r5）：可逆拼法的 `.env` 值拒跑、根層 `.env` 改以 inode 認定、入口大小閘改用位元組

（**下面前兩條各被 r6 修正一次**：拼法比對的對象從「整份檔案原文」收斂成「決定該值的
那一行賦值」（R6-1），根層 `.env` 的認定從 inode 改成目錄列表（R6-2）。理由與新規則見
本節下方的 r6 附錄；此處保留 r5 當時的裁決原文作為歷程。）

- **`.env` 值的拼法若「可逆」就整場拒絕（R5-1）**：登記進遮蔽器的是 python-dotenv
  **解析出來的值**，但磁碟上那一行可以用另一種拼法表達同一個值——`KEY="ab'cd\"ef"`
  解析成 `ab'cd"ef`，而 raw 文字裡根本沒有這個子字串。builder 的 `run_shell` 未上鎖
  （D21），可以直接 `cat` **正式套件**的 `.env`（它在 staging 外，但完全構得到），
  於是那段 raw 文字對現場遮蔽器與 `llm_log._redact` 都**比對不到**，一路進下一輪提示、
  AI 日誌 attempt bodies，甚至 summary → job body → sidecar。**安裝路徑早就把這件事
  當成外洩並拒絕**：`_dotenv_serialize_value` 對「單引號同時碰上 `"`／`\`」的值回 None，
  `_inject_secret_into_env` 的 round-trip 再兜底，目的就是讓 raw 那一行**逐字含有**
  原值；差別在於 install **寫**這個檔案、掌握得了拼法，而 revise **繼承**手編的結果，
  一直沒拿到同一個保證。**裁決**：解析完值之後、任何 LLM 呼叫與任何 `register_inflight_secret`
  之前，逐一確認每個要登記的值**逐字出現在 raw `.env` 文字裡**；只要有一個不是，就以
  新的 category-only zh-TW 錯誤（`_ERROR_REVISE_ENV_UNMATCHABLE`）拒絕整場修訂。方向
  與 r1 的「太短就不跑」完全一樣：誠實的選項只有「不要跑」與「洩漏」。**訊息與
  `_ERROR_REVISE_ENV_UNMASKABLE` 分開**，因為**補救不同**（那條是「加長」，這條是
  「簡化該值的引號與跳脫寫法」），這正是 R3-2 立下的「同一個拒絕點、不同的可行動原因、
  就要不同訊息」；一樣**只講條件與補救，不提 key、不提值**——在一則因為遮不掉而拒絕的
  訊息裡把值印出來，就是它要防的那個外洩。
  - **兩道入口閘合成一段政策**：`_unmaskable_env_error(text, values)` 同時做 r1 的下限
    檢查與這道拼法檢查，`run_revise` 只留一行。它們說的是同一件事（遮不掉的值不能交給
    builder session），只差在「為什麼遮不掉」。
  - **比對用的文字就是 `tools._read_regular_file_capped` 產出的那份**（utf-8
    `errors="replace"`、universal newlines），而這是**正確**的表示法而不是順手的：
    `_run_shell_subprocess` 以**完全相同**的設定解碼它 merged 的輸出，所以在這裡是
    逐字子字串的值，在 `cat` 的輸出裡也會是遮蔽器看得到的同一個字串。
  - **在 worker 上做**（R1-3 同一條規則）：一份 64 KiB 的 `.env` 可以塞進數千個值，
    每個值對全文做一次子字串搜尋是幾百毫秒的真工作，而 revise 跑在背景 job、與所有
    HTTP request 共用同一個 loop。文字由**同一次讀取**交出（`_read_env_for_values` 改回
    `(existed, values, text, error)`）——第二次讀可能讀到不同內容，那樣檢查的就不是
    被登記的那一版。
- **根層 `.env` 改以 inode 認定，不再看拼法（R5-2）**：copytree 的 ignore 原本排除
  根層所有 casefold 等於 `.env` 的名字，但 `_preserve_env_file` **只還原正好叫 `.env`
  的那個**。於是在 case-**SENSITIVE** 檔案系統上，`.ENV` 是一個**不同的、普通的**套件
  檔案（工具的 entry 以套件目錄為 cwd，大可自己讀它），卻被一次無關的修訂**靜默刪除**，
  而 `validate_package` 照樣**驗證通過**——與 R2-2 修掉的巢狀 `.env` 完全同一種失效，
  只是換個名字活在根層。**裁決**：只有在它**就是**受管的那個檔案時才排除——保留精確
  名稱比對，casefold 變體則要求與 `<root>/.env` **同一個 inode**（`st_dev`／`st_ino`），
  這在 case-insensitive 檔案系統上恰好為真、在 case-sensitive 上恰好為假。sidecar 的
  保留命名空間**每一層都排除，維持不變**。
  - **用 `os.lstat` 而不是 `os.stat`／`os.path.samefile`**：要問的是「是不是同一個
    **目錄項**」。case-insensitive 檔案系統上兩個名字就是一個目錄項，lstat 自然同 inode；
    但一條指向 `.env` 的 `.ENV` **symlink** 在跟隨式 stat 下也會判成「同一個檔案」，
    排除它就是把這個 finding 的 bug 縮小重演一次（一個不同的套件檔案被靜默刪除、
    而且永遠不會被還原）。lstat 讓連結還是連結，`copytree(symlinks=True)` 原樣帶過去，
    `_preserve_env_file` 放回 `.env` 之後它自然又解得開。
  - **兩個 stat 都有防護**：檔案在 `scandir` 與此之間消失、或根本沒有 `.env`，都**不是
    同一個檔案**，所以照樣複製。這是安全的方向（另一邊是「憑一次失敗的 stat 刪掉套件
    內容」），而受管檔案本身不可能因此漏掉——精確名稱那條分支根本不 stat。
- **入口的大小閘改用位元組（R5-3）**：入口量的是 `_read_regular_file_capped` 解碼後的
  **字元數**，而 r4 的複製閘量的是 `lstat().st_size` 的**位元組**。CJK 很多的 `.env`
  （約 3 萬字元 ≈ 90 KB）因此**過得了入口**、燒掉一整場多輪 builder session，然後才在
  promote 被拒——保證失敗的工作卻付了全額，而且每次重試都再付一次。**裁決**：入口
  沿用它本來就會做的那次 `lstat`，改用 `st_size` 套**同一把位元組尺**，讓 promote 會拒
  的 `.env` 根本不會開場。**promote 的檢查照樣留著**：檔案可以在 session 中途被換掉，
  那正是 R4-1 指出的危險，入口閘看不到它。**錯誤沿用 `_ERROR_REVISE_ENV_TOO_LARGE`
  而不新增一條**：條件是同一個檔案的同一把尺，**補救也一模一樣**（把 `.env` 縮到上限
  以下），而 R3-2 拆訊息的準繩恰恰是「補救不同才要不同訊息」——這裡不同的話反而是在
  暗示一個操作者其實沒有的動作。讀取後的**字元檢查保留**，它現在的角色是防「lstat 與
  read 之間檔案長大了」的同型 TOCTOU 殘留。**`tools._load_tool_dotenv` 自己那條以字元
  計的上限是另一件事**：那是 **runtime** 願意載入多少的既有界線（而且是降級成 no-env
  而非拒絕），本輪完全不動，docstring 裡寫明。

### D40 附錄（P3b review r6）：拼法閘只認賦值行、根層 `.env` 改以目錄列表認定、入口定版閘改用嚴格讀取器

- **可逆拼法的比對對象從「整份檔案」收斂成「決定該值的那一行賦值」（R6-1）**：r5 的閘
  是對**全文**做子字串搜尋，於是一段與該值無關的文字就能替一個可逆拼法作保——

  ```
  TOKEN="abcd\"efgh"
  # abcd"efgh
  ```

  解析出來的 `abcd"efgh` 確實出現在檔案裡（在註解上），閘門放行，而**真正持有憑證的
  那一行**仍然用一個沒有任何遮蔽器認得的形式拼寫它：builder 一句 `cat` 就把它印出來，
  照著跳脫規則反推即得原值。**裁決**：要問的從來就只有「決定這個值的那行賦值，是怎麼
  拼的」。python-dotenv 是**後出現者勝**，所以那一行就是**最後**一個 key 相符的行——
  key 的辨識沿用 repo 既有的 `_env_line_key`（`_inject_secret_into_env` 為了同一個
  last-wins 理由，本來就用它把先前同名的行全部丟掉），取第一個 `=` 之後的原始右邊
  （去除前後空白），要求解析值**逐字**出現在其中。`KEY=abcdef` 與 `KEY="abcdef"` 過，
  `KEY="ab\"cd"` 不過。
  - **找不到可對應的賦值行也是拒絕**：跨行的引號值、續行、任何以「一行」讀不出來的
    形狀。**讀得出來的才驗，讀不出來的就拒絕**——與這條路徑上每一道閘同一個方向
    （`.env` 讀不到就拒絕整場、sidecar 讀不出可信狀態就拒絕換裝）。
  - **這是對 r5 的一次刻意收斂**：r5 放行**真正跨行**的引號值（它的換行在檔案裡就是
    真的換行，整個值確實是全文的子字串）。現在拒絕，理由不是它一定會外洩，而是
    **一行永遠不可能包含換行**，本閘因此無法分辨這種拼法與「值的第一段剛好落在開頭
    那行」——無法為它作保就不放行。訊息與補救不變（簡化引號與跳脫寫法），所以**沿用
    `_ERROR_REVISE_ENV_UNMATCHABLE`**，不新增一條（R3-2 的準繩：補救不同才要不同訊息）。
  - **被蓋掉的那些行不是漏洞**：dotenv 把它們丟了，所以它們拼出來的東西根本不是已登記
    的值——那就只是套件裡的普通文字，沒有任何秘密在它後面等著被遮。
  - **安裝路徑寫得出來的每一種 `.env` 必定過關，這是構造上的保證而不是運氣**：
    `_inject_secret_into_env` 會丟掉所有先前同名的行、把自己那行**附在最後**（所以它
    就是本閘會讀的那一行），而 `_dotenv_serialize_value` 只吐出三種逐字含有原值的拼法
    （裸值、單引號、確定不會有跳脫生效的雙引號），做不到就直接拒絕安裝。以測試釘住
    整組安裝可寫的值形狀。
  - **`_unmaskable_env_error` 的參數從 values 清單改成 key→value 映射**，因為要問的是
    「這個 key 的那一行」；掃描也從「每個值對全文搜一次」變成「一次走完所有行、每個值
    只在自己那行的右邊搜一次」，仍在 worker 上（R1-3）。
- **根層 `.env` 的認定改以目錄列表為準（R6-2）**：r5 的 inode 判準挑錯了鑑別特徵——在
  case-**SENSITIVE** 檔案系統上，`.env` 與 `.ENV` 可以是 **hard link**：兩個不同的目錄項、
  同一個 `st_dev`/`st_ino`。於是兩個名字都被排除在複製之外，而 `_preserve_env_file` **只
  還原正好叫 `.env` 的那一個**，換裝成功、備份刪掉之後，`.ENV` 就這樣消失了——正是 R5-2
  要修掉的那個失效，換由它挑的那個測試放進來。**裁決**：用 ignore callback **本來就會
  收到的那份目錄列表**當鑑別特徵——精確的 `.env` 永遠排除；casefold 變體**只有在**該列表
  裡**沒有**精確的 `.env`、**且**它與 `<root>/.env` 是同一個目錄項時才排除。
  case-insensitive 檔案系統放不下兩個只差大小寫的目錄項，所以它的列表只會有一個名字
  （以它被存起來的大小寫），而 `<root>/.env` 開出來就是它——排除，正確；case-sensitive
  且兩者都在，列表就同時帶著兩個名字（不論是不是 hard link），變體是普通內容——複製，
  正確；只有變體、沒有 `.env`，就沒有東西可以「是同一項」，照普通內容複製，該套件也就
  真的沒有受管的 `.env`（`tools._load_tool_dotenv` 開精確名稱時看到的正是這件事）。
  - **同一項的確認仍用 `os.lstat`（不跟隨）**：R5-2 的理由原封不動成立——一條指向 `.env`
    的 `.ENV` symlink 在跟隨式 stat 下會被判成「同一個檔案」，排除一個永遠不會被還原的
    獨立目錄項，就是這個 finding 的縮小版重演。列表決定大小寫那一題之後，這個 stat 剩下
    的工作是確認「`<root>/.env`——系統其他每個地方開的就是這個路徑——真的解到這一項」。
  - **`exact_present` 刻意是必填的 keyword-only 參數**，理由同 R3-1 的 `existed_at_start`：
    給它預設值就是替「以後有人忘了傳」預留一條靜默走錯分支的路。列表比對**每個目錄只做
    一次**（`names` 是 list）。
  - **既有的 hard link 測試被強化**：它原本只斷言「兩個名字都沒進 staging」，正好把這個
    bug 當成正確行為釘住；現在斷言 `.ENV` 進得了 staging、也出現在**發佈後的套件**裡、
    位元組不變，`.env` 則被逐位元組還原。另補一個直接測 `_is_preserved_env_name` 的單元
    測試，因為 case-insensitive 那一支（列表只有一個名字）在本機 case-sensitive 的
    filesystem 上，end-to-end 根本產生不出來。
- **入口的定版閘改用嚴格讀取器（R6-3）**：`run_revise` 的入口仍用 **total** 的
  `summary_status`，於是一個**請求進來之前就已經壞掉/讀不出來**的 sidecar 照樣過閘、燒掉
  一整場多輪 builder session（全程佔住全域 single-flight），最後必然被 promote 的嚴格讀取器
  拒絕——拒絕從來不是問題，**帳單**才是，而且每次重試都再付一次。**裁決**：入口改用
  `summary_status_or_unknown`，`final` **與** unknown 一律拒絕，訊息用 promote 那兩條**同樣的**
  常數，讓操作者不論在哪一端被拒都看到同一組可區分的補救（修好／移除損壞的 sidecar vs.
  解除定版）。
  - **路由那道便宜的入口檢查刻意維持 total**：路由回答的是「這個資源目前**已知**的狀態」，
    猜錯的代價只是這個拒絕改以工作結果呈現、而不是當下的 409；會毀東西的那次判斷（換裝）
    與會**花掉一整場 session** 的那次判斷（開工）才需要 fail-closed。此決定寫在 `run_revise`
    的註解裡而不是路由檔——本輪的可改檔案不含 `routers/tools.py`，而那裡**不需要改任何程式碼**。
  - **入口讀到的 meta 刻意丟掉**：origin 一定要來自**換裝前**那次讀取（R4-2），那才是換裝
    真正據以行動的答案；把入口這份帶下去，等於把「兩次讀取可能不一致」的 bug 用更長的
    時間尺度重新引進來。

### D40 附錄（P3b review r7）：拼法閘改為「只認我們寫得出來的拼法」、換裝前重驗要出貨的位元組、同步 regenerate 取真正的名額、工作 404 訊息改中性

- **拼法比對從「包含」改成「等於我們自己寫得出來的三種拼法」（R7-1）**：r6 把比對對象
  從整份檔案收斂到那一行賦值的右邊，但**比對方式仍然是子字串**——於是同一招原封不動
  搬到**同一行**就又贏了：

  ```
  TOKEN="abcd\"efgh" # abcd"efgh"
  ```

  python-dotenv 把引號值與行尾註解**分開吃**，解析出 `abcd"efgh`；而 `_env_assignment_rhs`
  交出的是 `=` 之後的**整段**，註解也在裡面，於是「值出現在右邊」成立、閘門放行，
  真正持有憑證的那半段卻仍然用跳脫拼寫它。**部分比對已經被同一招破了兩次**（r5 整份檔案、
  r6 同一行），結論不是再收斂一次範圍，而是**範圍本身不是正確的軸**：一條規則若能被
  「旁邊的文字」作保，就永遠有下一個更靠近的旁邊。**裁決**：右邊（去前後空白後）必須
  **等於**這套系統自己會寫出來的拼法之一——裸值、`'值'`、`"值"`，而且每一種是否算數，
  由 `_dotenv_serialize_value` **自己的判準**決定（新的 `_dotenv_safe_spellings` 直接呼叫
  它，而不是把它的規則抄一遍）：裸值只有在**序列化器本來就會寫成裸值**時才算數
  （`serialize(v) == v`，因為裸值是它的第一順位分支），`'值'` 要求值裡沒有單引號，
  `"值"` 要求值裡沒有 `"` 也沒有 `\`（跳脫不可能生效）。序列化器回 None 的值
  （單引號同時碰上 `"`／`\`），這個集合就是**空的**——與安裝路徑「寫不出來就拒絕」同一個答案。
  - **這是一次刻意收緊，而且明說代價**：`KEY=value # comment` 是合法的 dotenv，現在被拒；
    `KEY=兩個 詞`、`KEY=a#b` 這種「序列化器本來就會加引號」的裸值也被拒。它們都不是外洩，
    但**沒有任何一條短於「重新實作 dotenv tokenizer」的規則**能把它們與上面那行分開——
    而重新實作 tokenizer 正是「防禦性功能的失敗模式比它解決的問題難」那條教訓要避開的。
    補救只有一步（把那一行寫成最單純的形式），而且**不會有任何本系統產生的套件因此
    修不了**：安裝路徑會丟掉所有先前同名的行、把 `KEY=<序列化結果>` 附在最後，那一行
    必定命中這個集合——因為集合就是問同一個序列化器要來的。以測試釘住整組安裝可寫的值形狀
    （10 種）在**入口與換裝兩端**都通過。
  - **訊息隨之改口徑**：`_ERROR_REVISE_ENV_UNMATCHABLE` 沿用（同一個拒絕點、同一個補救——
    把那一行寫單純），但補救文字從「簡化引號與跳脫」擴為「寫成最單純的形式、移除行尾註解
    與多餘跳脫」，因為現在被拒的形狀有一部分**引號根本沒問題**，只講引號會把操作者送去找
    不存在的東西。訊息一樣**只講條件與補救，不提 key、不提值**——測試也繼續釘住這點
    （寫訊息時撞到「值裡的 KEY 三個字」這個真實陷阱，正是那條斷言擋下來的）。
- **換裝前重驗「即將出貨的那些位元組」，並就地登記其中的值（R7-2 可封的那一半）**：
  拼法閘／長度下限／位元組上限原本只驗**開工那一刻的快照**。但 `.env` 可以在 session
  中途出現或被換掉（R4-1 已經指出這件事，只用在大小上），於是一個開工時**沒有 `.env`**
  （或有一份合規的）的套件，可以在中途獲得一份不合規的，而 `_preserve_env_file` 會把那份
  **從沒被驗過**的檔案原樣複製進即將發佈的套件。**裁決**：`_preserve_env_file` 在複製前
  **重讀它即將複製的那個來源**，對那些位元組重跑**同一套政策**（`_unmaskable_env_error`
  ＋位元組上限），不過就以同一組 category-only 錯誤拒絕換裝——閘門從此驗的是**真的會出貨的
  東西**，而不只是 session 開場時碰巧在那裡的東西。同時把讀到的值**在複製之前**
  `register_inflight_secret`，讓換裝後的總結／job body／sidecar 路徑有遮蔽可用
  （換裝窗內舊包在隱藏備份裡，`known_secret_values` 跳過隱藏目錄）。
  - **登記與記帳是同一個動作**：新值 append 進 `run_revise` 自己那份 `registered` 清單
    （所以參數是**往下傳一個可變清單**，不是把值回傳上來）——回傳的話，任何一條不走到
    return 的路徑都會留下一個沒人負責 discard 的登記；就地 append 則讓「登記」與「記帳」
    在同一行完成，`finally` 必定清乾淨。以測試釘住：中途新增的合規 `.env` 在 `copy2` 那一刻
    已經在 `_INFLIGHT_SECRETS` 裡，而 `run_revise` 回來之後它是空的。
  - **殘留的視窗誠實寫明**：這次讀取與 `copy2` 是兩次 open，中間隔一個瞬間——與 `lstat`
    和 `copy2` 之間本來就有的那個同型、同尺寸，屬既有裁決接受的 check-then-act 殘留。
    不能靠「讀一次然後把讀到的文字寫出去」消掉它：那份文字是 `errors="replace"` 解碼過的，
    寫回去就不再是位元組複製（R2-1）。
  - **不可封的那一半另外記錄**：session **進行中**的那次讀取（builder `cat` 掉操作者中途
    新建的憑證檔）在 D21 的信任邊界下不可防，且 `llm_log` 是寫入當下遮蔽、無法回頭補遮。
    完整情境、為什麼不可防、已封住什麼、以及對操作者的指引（**某個套件的修訂進行中，
    不要新增或編輯該套件的 `.env`**），記在 `裁決紀錄.md` #6。
- **同步 regenerate 改為「取名額」，不再只是「問一下有沒有人在跑」（R7-3）**：路由原本
  讀一次 `any_job_active()` 就往下走，接著 `await` 一整趟 LLM 往返——這是一個**寬度等於
  一次 LLM 呼叫**的 check-then-act。窗內被放行的 revise 會換掉整包、寫下**新的** sidecar，
  然後較舊的那次 regenerate 用**已經被換掉的**套件內容產生的總結把它蓋回去：新的、正確的
  sidecar 輸給舊的。**裁決**：新增由 `_JOBS_LOCK` 守著的同步保留機制（`_SYNC_OPS` 一組
  token），`reserve_sync_operation()` 在**同一次上鎖**內完成「有沒有人佔著」與「佔起來」
  兩件事，比照 `_admit_job` 本來的作法；持有期間 `any_job_active()` 為真、`_admit_job()`
  一律拒絕（所以 install／revise 送出得到既有的 409），路由在 `finally` 釋放。
  - **判斷式抽成一個 `_single_flight_held()`，並明文標「呼叫前必須持鎖」**：三個使用者
    （`_admit_job`、`reserve_sync_operation`、`any_job_active`）各自在自己的臨界區內呼叫它，
    原本「兩份三個字的複製是原子性的誠實代價」那段註解因此作廢——現在是一份定義、三個
    臨界區，而且 `_JOBS_LOCK` 是非重入鎖這件事仍然被尊重（helper 自己不上鎖）。
  - **不可能死鎖、不可能漏掉**：取與放各自只在函式內短暫持鎖，**沒有任何 await 在鎖內**；
    釋放認 token（丟別人的名額是不可能的）且冪等；路由的 `finally` 覆蓋成功、502、503、
    409、404 每一條路徑。以測試釘住成功與例外兩端都有還回去。
  - **`any_job_active()` 保留但退出請求路徑**：它現在是「單一名額被佔用」這件事的**定義
    所在**（兩半都在這裡具名），會動手的兩個入口都改走「檢查即取得」。測試也因此改成
    釘住「保留與工作同等份量」。
  - **測試單例重置一併補上**：`_reset_jobs_for_tests()` 同時清 `_JOBS` 與 `_SYNC_OPS`——
    少清一半，一個在保留中結束的測試會讓下一個測試完全無法開工。
- **工作輪詢的 404 訊息改中性（R7-4）**：`GET /api/tools/jobs/{job_id}` 從 D40 起同時服務
  install 與 revise，訊息卻還寫著 `Install job not found`——一個從沒送出安裝的使用者被告知
  他的「安裝」不見了。改為 `Tool job not found`；常數名 `_JOB_NOT_FOUND` 本來就中性，不動。
  前端不看這個字串（`ToolsPage` 只看 404 狀態碼），文件也沒有引用它，所以這是純後端改名。

### D40 附錄（P3b review r8）：每一行賦值都要驗拼法，不只決定值的那一行

r6/r7 只驗「決定該 key 值的那一行」，理由是「被覆蓋的行不是 dotenv 解析出來的值，
所以不帶已登記密值」。這個推理是錯的——**被覆蓋的行可以用另一種拼法編碼同一個
現行密值**：

```dotenv
TOKEN="abcd\"efgh"    # 被覆蓋，但這就是現行憑證
TOKEN='abcd"efgh'     # dotenv 取這行；是我們接受的安全拼法
```

登記值是 `abcd"efgh`，`cat` 會同時吐出兩行，遮蔽器只遮得到第二行，第一行原樣送進
模型。修法：`_env_assignment_rhs_all` 取出**每一行**賦值的 RHS，逐行做 `_plainly_spelled`
的**形狀**檢查——「這個拼法是否逐字持有它自己的內容」這個問題不需要知道該行原本
想拼哪個值，因此對被覆蓋行同樣可答。純文字寫的舊值（最常見的手改歷史，如
`KEY=oldvalue` 上面留著）照樣通過，測試釘住雙向。

### D40 附錄（P3b review r9）：形狀檢查改由真正的 parser 判讀拼法

r8 的 `_plainly_spelled` 自己剝掉成對引號、把剩下的內文當字面看。這對 POSIX shell
的單引號成立，對 **python-dotenv 不成立**——實測（非臆測）確認它在**單引號內**同樣
會把 `\\` 解碼成一個反斜線：

```dotenv
TOKEN='abc\\defghi'   # 被覆蓋；磁碟上兩個反斜線，值裡一個
TOKEN='abc\defghi'    # 同一個現行值，純文字拼法
```

被覆蓋那行因此被誤判為「純文字」而放行，卻正好以遮蔽器比不中的形式拼出勝出行的
憑證。修法：不再自行解讀拼法——把 RHS 交給**真正的 parser**（以一支固定的探針 key
組成賦值）問「這行是什麼意思」，再拿結果問我們自己的序列化器「這個拼法安全嗎」。
兩個問題各由擁有它的那一方回答，本模組不重新實作任何一邊；parser 讀不出值的形狀
一律拒絕。真的含反斜線的值（例如 Windows 路徑）只要照序列化器的寫法寫就照常可修訂，
測試雙向釘住。
