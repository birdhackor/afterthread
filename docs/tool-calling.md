# 工具呼叫（Tool Calling）機制說明

## 1. 這是什麼

afterthread 的 AI 在快速捕捉、AI 補齊與 AI 進度更新時，可以呼叫已安裝工具查內部
文件或資料，再把結果帶回同一場結構化生成。這個 app **沒有聊天介面**；工具只服務
這些 per-item 操作。KB 網頁安裝器也使用工具迴圈，但拿到的是另一組用來建造工具的
meta-tools。

整套機制是 `backend/afterthread/services/llm.py` 裡的手寫迴圈，不依賴 agent
framework。原因不是排斥抽象，而是這裡的逐輪 deadline、順序執行、輸出與對話上限、
秘密遮蔽、AI 日誌格式與 strict JSON 修正都已有具體契約；換框架仍要把同一套政策
重寫在 middleware，卻多一層預設行為要核對。

## 2. 一次工具呼叫的生命週期

### 廣告工具

呼叫模型前，registry 掃描每個 package，解析 `.afterthread.meta/current`，只把
`Resolved`、內容有效、且 package state 啟用的版本組成 OpenAI-compatible `tools`
陣列。規格來自該版的 `tool.json`：

- `name`：必須等於 package 名。
- `description`：告訴模型何時呼叫與會得到什麼。
- `parameters`：輸入的 JSON Schema。
- `entry`：只供後端執行，不放進模型規格。

若沒有 valid + enabled 工具，就完全不帶 `tools` 參數；tool-less workflow 的 prompt
不因子系統存在而改變。

廣告時 handler 同時封住 `PackageRoot`、精確 `VersionRoot`、entry 與 manifest
identity。模型可能幾分鐘後才真的呼叫；若那時 `current` 已從 V 切到 N，handler
**仍然跑 V**——這讓模型看到的 schema、實際 entry 與 cwd 永遠屬於同一版，而不是拿舊
契約去跑新程式。

**但呼叫當下仍會解析一次 `current`，只是不看它指向誰。** 要求它**不是 `Unresolved`**
（也就是「這個套件現在還有可用的版本」），但**不要求它等於 V**。少了這道確認，一個
`current` 被弄壞的套件會在列表上顯示 invalid＋停用，而已廣告的 handler 照樣跑得起來
——同一個問題兩條路徑兩個答案（overall review r2-4）。具體差別：你刪掉或弄壞 `current`
而 V 本身完好時，這次呼叫得到明確拒絕，不會啟動子行程。

### 模型要求與後端執行

模型可回一批 `tool_calls`。後端逐一：

1. 把 arguments JSON 解析成物件；失敗就回固定錯誤文字，不啟動工具。
2. 登記廣告時那個 `VersionRoot` 正在執行，讓刪除與 discard 不會清掉子行程仍可能
   相對開啟的檔案。
3. 重驗該版 `tool.json` identity；操作者若在廣告後手改 manifest，這次呼叫保守拒絕。
4. 讀 package-level `.afterthread.meta/state.json`。缺席、foreign、unreadable 都
   fail closed；沒有 `tool.json.enabled` fallback。
5. 從短允許清單建立乾淨環境，再加入 package `.env` 與正規化 TLS 設定。
6. 以廣告時的 `VersionRoot` 為 cwd 啟動 entry；arguments JSON 經 stdin，stdout
   是工具結果。
7. 套用單工具 timeout、process-group 終止與輸出截斷；成功、非零 exit、timeout
   都收斂成模型可讀的結果字串。

若操作者在廣告後親手 discard V，而 V 尚未進入執行登記，handler 可能在真正呼叫時
找不到它並明確拒絕。這是明示接受的單人操作邊界；為幾秒內的人為競態引入 reservation
或 refcount，複雜度不成比例。

### 結果回到模型

工具結果先遮蔽已知秘密，再以 `role="tool"` 接回對話。模型可以給最終答案或要求下一輪
工具；達到輪數、對話 token 預算或 deadline 時，後端停止帶工具並要求它直接收斂。
最後輸出必須是符合呼叫端 Pydantic JSON Schema 的單一物件；解析或驗證失敗只給一次
帶錯誤說明的修正重試，再失敗則回固定上游錯誤。

## 3. 安全與資源邊界

- **不是沙箱**：已安裝工具與 builder `run_shell` 都以服務帳號權限執行。乾淨環境
  防止意外繼承後端 secret，但同 UID 程式仍可能從 `/proc` 或其他可讀路徑取資料。
  真正信任邊界是只安裝可信的 OpenAPI 文件與指示。
- **子行程環境從零建立**：只透傳 `PATH`、`HOME`、`LANG`、`LC_ALL`、`TMPDIR`，
  再加入 package `.env`；父行程 `OPENAI_API_KEY` 不會結構性地落入 child env。
- **單工具限制**：`LLM_TOOL_TIMEOUT_SECONDS` 控制 timeout，
  `LLM_TOOL_OUTPUT_MAX_CHARS` 控制送回模型的 stdout 長度；逾時會終止整個 process
  group，避免只殺 shell 留下孫行程。
- **每輪 tool_calls 限制，兩道不同的閘**：前 16 個才真的執行
  （`_MAX_TOOL_CALLS_PER_REPLY`），第 17 個之後每一個都收到「too many tool calls」的
  拒絕訊息——仍然回一個結果，因為 API 規定每個 `tool_calls` 都要有對應回覆，少一個
  下一輪就會被拒。但整則回應若超過 **64** 個（`_MAX_TOOL_CALLS_ACCEPTED`），代表這則
  回應本身已經不正常，後端連處理都不處理，直接當上游錯誤走 502。另外 id、name、
  arguments 與同則文字的總量上限 256 KiB，擋的是「數量合法但單一參數巨大」。
- **對話總量**：`LLM_TOOL_CONVERSATION_BUDGET_TOKENS` 以模型回報的
  `prompt_tokens` 動態估算字元／token 比，控制多輪工具結果累積。
- **deadline 不啟動新工作**：剩餘時間不超過一秒就不再啟動工具。threadpool 裡已經
  跑的 subprocess 不能由 asyncio timeout 強制打斷；這條規則把最壞延遲限制成
  deadline 加一個既有工具 timeout，而不是再加整輪工具。
- **已知秘密遮蔽**：後端 API key、所有 package `.env` 值、install/revise/tool-call
  期間登記的值，都在工具輸出進模型前 fail closed 遮蔽。短於六字元的值不遮，避免
  普通文字被洗掉，因此安裝表單也拒絕短 secret。AI 日誌是觀測者，遮蔽失敗時刻意
  fail open，不讓記錄功能打斷真正操作；所以日誌遮蔽是常態防線，不是安全隔離。
- **builder 檔案工具有 jail，shell 沒有**：`write_file`／`read_file`／`list_dir`
  拒絕 absolute path、`..` 與 symlink escape；`run_shell` 仍是真 bash。

## 4. 版本化工具套件

### 磁碟版面與所有權

```text
<TOOLS_DIR>/<name>/
    .afterthread.meta/
        state.json
        current
    versions/
        <vid>/
            .afterthread.meta/
                origin.json
                summary.json       # 可缺
            tool.json
            run.py ...
        <vid>.discarded/
    .env
```

package layer 由後端管理「現在」的狀態，`.env` 是操作者跨版本持有的秘密；工具內容
只在 version layer。版本 id 為 `YYYYMMDDTHHMMSSZ-ffffff`。

### 為什麼需要四個型別

路徑餵錯層級往往不會 exception，只會把存在的檔案讀成缺席，所以程式用型別固定入口：

- `PackageRoot`：已安裝 package；讀 state／`.env`、刪除、discard、清掃。
- `VersionRoot`：一個已安裝版本；讀 manifest／metadata、作 subprocess cwd。
- `BuildRoot`：尚未安裝的 builder 內容；只做安裝前驗證。
- `Resolution`：`Resolved(package, version, vid, previous)` 或帶原因的
  `Unresolved(package, reason)`；只有前者能交出 `VersionRoot`。

`validate_tool_content(BuildRoot, expected_name)` 與
`scan_installed(PackageRoot)` 共用 `_scan_tool_content` 的 manifest、entry 與內容規則。
前者不碰 package metadata；後者只解析一次 `current`，再由同一個 resolution 產生
規格、lineage 與總結。

### `current` 與提交標記

`current` 最多 64 bytes，只接受合法 vid 與最多一個尾端換行。目標必須是
`versions/<vid>` 真目錄，且有合法 `.afterthread.meta/origin.json`。origin 同時是
**已提交標記**：缺或壞的 version 不可被 current 指向，也不可作 lineage 目標。

任何錯誤都得到 `Unresolved`：不猜最新版本、不依目錄排序、不 fallback。寫入以同目錄
temp file、fsync、`os.replace`、directory fsync 原子發布。

### 全新安裝

每場 builder 工作有 `<TOOLS_DIR>/.staging/<uuid>/{build,shell}`；所有出口都清完整
session root。

1. builder 在 `build/` 建立 `tool.json` 與實作並以 `run_shell` 測試。
2. 後端擷取 builder 根層 `.env` 的 KEY 名後刪檔，再剝除**根層 `.afterthread.meta/`**
   ——**只有它**。`.ai_meta.json` 與 `.afterthread-state.json` 這兩個 legacy 扁平名稱
   在**版本層是工具自己的東西**，任何深度都保留原樣：保留字描述的是**套件層**，而
   `BuildRoot` 會變成 `versions/<vid>`。這條在 overall review r3 修正過——原本連版本層
   一起剝除，導致遷移剛保住的 FOREIGN 狀態檔（工具自己的游標）在第一次修訂時被丟掉，
   工具下一次執行才壞。
3. `validate_tool_content` 驗 manifest、entry、containment、`.env` 大小與已知秘密
   內嵌政策。
4. `shell/` 組成完整 package：單一 version、`origin.json(previous=null)`、初始
   enabled state、`current`；表單 secret 由後端寫成 package `.env`。
5. 全部持久化後，以一次 `os.rename(shell, <name>)` 上線。target 只要已存在任何內容
   就拒絕，不覆蓋。
6. 上線後 best-effort 產生 summary；失敗不倒轉安裝。

### builder 不出貨 `.env`

非秘密預設屬於版本，應放在程式碼，例如
`os.environ.get("API_BASE", "https://example.invalid")`；切換 current 時預設自然跟著
程式碼走。install 與 revise 都會擷取 builder `.env` KEY 名後刪除，job 的 `env_keys`
只回報名稱、絕不回值，提醒操作者自行寫入 `<name>/.env`，或把非秘密預設移回程式碼。

安裝表單的 `secret_name`／`secret_value` 是後端受控路徑：value 不進 prompt，只注入
`run_shell` 供測試、登記供遮蔽，並在上線前由後端寫入 package `.env`。

### 修訂

修訂只新增版本，不再整包換裝：

1. 從目前 `VersionRoot` 複製工具內容到 `build/`；package `.env` 不進工作區，值只注入
   `run_shell` 並暫時登記遮蔽。
2. builder 修改後，同樣剝除 builder `.env` 與 backend 保留 sidecar，再驗證內容。
3. `shell/` 寫入工具內容與
   `origin.json(previous=<目前 vid>, feedback=<本次意見>)`。
4. 先持久化並 rename 到 `<name>/versions/<新 vid>`。
5. 最後原子發布 package `current`；**這是唯一 commit point**。
6. best-effort 產生該版 summary。

package `.env` 與 `state.json` 原地不動，舊 versions 永久保留。候選 vid 會避開
`versions/` 下所有同前綴項目，包括 `.discarded`。

修訂開始前會驗 package `.env` 可讀、至多 64 KiB、值至少六字元，且決定每個值的最後
assignment line 能被遮蔽器逐字對回；無法安全遮蔽就不花 LLM。builder shell 仍有服務
權限，因此修訂期間不要手改 `.env`：已寫入的 AI 日誌不能事後補遮。

### 啟用狀態

live toggle 只在 `<name>/.afterthread.meta/state.json`：

```json
{"afterthread": "tool-state", "enabled": true}
```

marker 是所有權證明。缺席或可讀但沒有 marker 的 foreign 檔案都視為 disabled，
**不再讀 `tool.json.enabled`**；foreign 檔不覆寫、不刪除，PATCH 回 404。這個預設
避免只發布一半、package layer 沒有 manifest 的工具因舊預設 `true` 自己打開。

unreadable、非一般檔、超過 4 KiB，或 marker 正確但 `enabled` 不是 bool，會列成
invalid + disabled。一般檔內容壞掉可 PATCH 修復；symlink／FIFO／目錄必須由操作者先
移除。單純刪 state 只會回到 disabled。

toggle 只改 package state；修訂只新增 version 並切 current，兩者不需要發布鎖，
所以 revise／regenerate 進行中仍可切換。

### origin、summary 與 summary GET

每版 `origin.json` 在提交前寫好，後端之後不改，保存 source、去憑證的 OpenAPI
provenance、安裝指示、修訂 feedback 與 previous。來源 URL 只留
`scheme://host[:port]`；userinfo/path/query/fragment 可能帶未知 token，而 provenance
不需要它們。

`summary.json` 可整份原子替換，保存 summary、updated time 與 AI log identity。
兩份都走 typed、bounded、redacted、fail-closed、atomic publisher。summary 缺席或壞掉
只代表「尚無總結」，不會讓 version invalid；重新產生失敗也不清掉舊 summary。

summary prompt 先放工具內容，再放安裝脈絡；`.env` 只列 key 名。小預算因此先截背景，
不會拿零份程式碼要求模型編說明。

`GET /api/tools/{name}/summary` 按名稱定址，但回應帶它實際讀到的 `current_vid`。前端
若與展開列不符就 throw，不能把 P 的 payload 寫進 V 的 cache key。

### lineage 與 discard

previous 只來自 `origin.json.previous`，不依目錄排序：

- `sole`：previous 是 null；丟掉等同刪整包，走重量級確認。
- `usable`：previous 是另一個合法已提交版本；可退回。
- `broken`：previous 缺失、無效或自指；不提供 discard，只能修檔或整包刪除。

`DELETE /api/tools/{name}/versions/{vid}` 精確定址畫面那版。後端取得全域名額後先比對
current，再發布 `current=P`；pointer 發布就是 discard 完成。只有 directory fsync
確認 pointer 持久後才盡力刪 V；V 在執行就改名 `<vid>.discarded` 交給清掃。無法確認
持久化時保留 V 但仍回成功，因為多留未指向版本比斷電後留下懸空 current 安全。

### API 身分與衝突

revise／regenerate body 帶 `expected_vid`，discard path 帶 vid。前端 key 只保護本分頁，
另一分頁或終端機仍可改 current，所以後端在取得 single-flight 名額後、花 LLM／寫 pointer
前比對。

- `version_mismatch`：畫面過期；前端重抓列表，新的 vid remount 列。
- `job_busy`：全域名額被占；保留輸入或 confirmation，稍後重試，不 refetch。
- `lineage_unavailable`：previous 在載入後壞掉；不重試、不 refetch，顯示整包刪除。

沒有 usable current 的 package 仍列出，`description`／`current_vid` 為 null、
`lineage=broken`，只允許整包刪除。留下這列是恢復路徑，不是把壞 package 藏起來。

## 5. 一次性 web-v5 遷移

舊扁平 package 不會被 runtime fallback 讀取。停掉 afterthread，確認 `TOOLS_DIR` 後：

```bash
cd backend
uv run python -m afterthread.migrate_tools_v5 --dry-run
uv run python -m afterthread.migrate_tools_v5
# 同一份計畫已人工審過、需要非互動執行時：
uv run python -m afterthread.migrate_tools_v5 --yes
```

`--dry-run` 唯讀預檢並列出 legacy state／`.ai_meta.json` 分類、enabled、`.env` **key
名稱**與計畫，永不列 value。一般執行在第一次寫入前最後詢問；只有明確 `y`／`yes`
繼續。拒絕、closed 或 piped stdin 都是 no，**套件與其內容一個 byte 都不會變**。

**一個誠實的例外**：互斥用的 `.afterthread-tools.lck` 會在預檢**之前**就被建立（腳本要先
確認沒有東西握著鎖才開始看），所以在一個還沒有鎖檔的舊 tools 目錄上第一次跑 `--dry-run`，
**會多出這個檔案**。「什麼都不寫」指的是套件內容，不含這個鎖檔。如果你用雜湊或備份工具
核對唯讀性，會看到它；如果整個目錄是唯讀的，腳本會在預檢前就因為建不出鎖檔而停下。

`--yes` 表示計畫已先審過，不是預設同意。

確認後才建立 tools 目錄的完整兄弟 backup，再發布
`<TOOLS_DIR>/.afterthread-migration.json` write-ahead journal。任何 legacy package
預檢失敗都整場不開始；中斷後用同一指令重跑，journal 會續做、回復或清理，不靠
`.at-*` 名字猜所有權。既有 journal 已帶著前一次確認的 authority，真實重跑會直接
對帳、不再詢問；這時 `--dry-run` 只報 journal status。

每個 flat package 變成單一初始 version；manifest legacy `enabled` 被移除，owned
legacy state 搬到 package state，foreign state 原 bytes 留在 version content，
`.ai_meta.json` 拆成 origin/summary，`.env` 逐 byte 與 mode 留在 package layer。
所有 package active 後才持久記 `committed`：commit 前錯誤回復全部名稱；commit 後
清理失敗只待重試，不再 rollback。不要手動刪 journal 或 migration siblings。

## 6. AI 日誌怎麼配合除錯

AI 日誌記錄 capture、enrich、assist-update、tool_install 與 tool_summary 的每個 attempt：

- 每則 body 受 `LLM_LOG_BODY_MAX_CHARS` 截斷；單筆互動過大時，較早訊息摺疊成省略標記。
- tool-call 摘要只存 arguments 前 200 字元；完整 arguments 不在事後日誌。
- `tools_advertised` 記當輪工具名稱；`null` 表示該輪沒有送 tools。名稱也走同一個
  遮蔽／編碼／截斷收口。
- 日誌不保存當時完整 manifest，但版本目錄永久保留，所以只要該版未被 discard，仍可
  依記錄脈絡與 vid 到磁碟核對。
- install／revise job 提供 builder session 的 log id；summary sidecar 的 log id 另帶
  process token。id 是 process-local counter，後端重啟會重用數字；token 不符時前端
  拒絕把舊 deep link 指到新行程的無關紀錄。

## 7. 為什麼不用 LangChain

目前手寫迴圈的每條欄杆都是 app-specific：多大的輸出算洪水、哪些秘密要遮、何時停止
啟動工具、如何收斂 strict JSON。框架不會自動知道這些規則；尤其主流 agent 對同輪
calls 常預設平行派發，而本系統為了每個 call 前重查 deadline 必須依序執行。保留政策
就得替換框架的核心 executor，等於把現有邏輯塞進另一層殼。

本 app 也不需要多供應商熱插拔、跨行程 checkpoint、human-in-the-loop 或逐步 streaming。
若需求改變可以重評；目前直接保有可閱讀、可測試的 loop，比引入無法消除自訂政策的
framework 更簡單。

## 8. 相關設定速查

實際值與邊界以 `backend/afterthread/config.py` 為準，範本在
`backend/afterthread/env.example`：

| 設定 | 預設值 | 說明 |
| --- | --- | --- |
| `LLM_TOOL_ROUNDS_MAX` | `8` | 一般 AI workflow 最多工具輪數；用完後強制收斂。 |
| `LLM_TOOL_TIMEOUT_SECONDS` | `60` | 單一已安裝工具 subprocess timeout。 |
| `LLM_TOOL_OUTPUT_MAX_CHARS` | `50000` | 工具 stdout 餵回模型前的字元上限。 |
| `LLM_TOOL_CONVERSATION_BUDGET_TOKENS` | `500000` | 即時多輪對話的 token 預算。 |
| `LLM_PROMPT_BUDGET_TOKENS` | `200000` | item snapshot／OpenAPI／summary prompt 的 token 預算。 |
| `TOOLS_DIR` | 空 | package 根目錄；空值關閉工具功能。 |
| `TOOL_INSTALL_MAX_ROUNDS` | `24` | install／revise builder session 的最大工具輪數。 |
| `TOOL_INSTALL_TIMEOUT_SECONDS` | `900` | 整場 builder session timeout。 |
| `TOOL_INSTALL_SHELL_TIMEOUT_SECONDS` | `120` | builder 單一 `run_shell` timeout。 |
