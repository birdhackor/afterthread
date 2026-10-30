# afterthread Frontend

`backend/` FastAPI API 的 SPA 前端：把 file-based MVP 的快速捕捉／全面補充／回顧
方法論，做成可點擊操作的網頁介面。與 repo 根目錄既有的 file-based MVP 並存，兩者
互不依賴。

## 技術棧

- **React 19** + **Vite 8**（`@vitejs/plugin-react`）
- **@tanstack/react-router** — code-based（非 file-based）路由
- **@mantine/core** / **@mantine/hooks** / **@mantine/notifications** — UI 元件庫
- **jotai** — 全域狀態（atoms）
- **react-hook-form** — 表單狀態與驗證
- **Biome** — 格式化 + lint（取代 ESLint/Prettier）
- **pnpm** — 套件管理

## 指令

全部從 `frontend/` 目錄執行：

```bash
pnpm install      # 安裝依賴
pnpm dev           # 開發伺服器，http://localhost:5173，/api 代理到 http://localhost:8000
pnpm build         # production build 到 dist/
pnpm preview       # 本機預覽 production build（見下方「已知限制」）
pnpm lint          # biome check .
pnpm format        # biome check --write .（自動修正）
pnpm test          # vitest run（單元測試，node 環境、無 jsdom）
```

以上指令皆已在本機實際執行過並確認通過：`pnpm install`、`pnpm lint`（無錯誤，
精確檔案數隨程式碼增減而變，以指令實際輸出為準）、`pnpm build`（成功，僅有
chunk size 提示，非錯誤）、`pnpm test`（vitest，全數通過）；`pnpm dev` 啟動後
`http://localhost:5173/` 回 200，並實測 `GET /api/health`／`GET /api/llm/status`
經由 `/api` 代理正確打到本機 8000 埠的後端。

**已知限制（`pnpm preview` 與 `/api` 代理）**：本 repo 的 vite 8 設定下，
`preview` 伺服器會沿用 `vite.config.js` 的 `server.proxy`，把 `/api/*` 轉發到
**寫死的** `http://localhost:8000`，而不是依情境動態啟動的後端（`e2e/smoke.sh`
即因此在 preview 階段只驗證 SPA 外殼有被正確提供，不透過 preview 打任何 API；細節
見 `e2e/README.md`）。開發時請用 `pnpm dev`，其代理設定與 `pnpm preview` 相同但通
常搭配本機真的跑在 8000 埠的後端。

## 頁面總覽

路由定義集中在 `src/router.jsx`（見下方「慣例」）。

| 路徑 | 元件 | 用途 |
| --- | --- | --- |
| `/` | `HomePage` | 總覽／回顧儀表板：呼叫 `GET /api/review`，把非終態項目分成待補齊／進行中／等待中／擱置四組；同時是後端連線狀態／LLM 設定狀態的顯示位置（`StatusFooter`）。 |
| `/capture` | `CapturePage` | 快速捕捉：貼上一段原始討論文字，呼叫 `POST /api/capture`，顯示 AI 產出的項目與追問問題；LLM 未設定時顯示提示、AI 動作停用。 |
| `/items` | `ItemsListPage` | 項目列表：狀態／stage／tag／關鍵字篩選 + 分頁（篩選與分頁狀態存在 `atoms/filters.js`）。 |
| `/items/new` | `ItemNewPage` | 手動新增項目的表單頁（沿用 `ItemForm` 元件）。 |
| `/items/$itemId` | `ItemDetailPage` | 單筆項目詳情：完整欄位、progress 歷史（可追加一筆）、狀態/階段快速修改、`ItemAiActions` 提供的「AI 補齊」（enrich）／「AI 進度更新」（assist-update）兩個操作、刪除。 |
| `/items/$itemId/edit` | `ItemEditPage` | 手動編輯項目的表單頁（沿用 `ItemForm` 元件）。 |
| `/tools` | `ToolsPage` | 「已安裝工具」（清單／啟停／刪除，每列可展開讀取／重新產生 AI 總結、提意見送出 AI 修訂、唯讀的「設定」與「最近執行」；列上顯示最近一次執行的結果，可修的失敗有「請 AI 修正」；`?job=<id>` 接上其他頁面發起的修正工作；修訂沒有發布時，這一輪的成果會存成草稿，該列顯示草稿提示（狀態、輪數、上一輪怎麼結束、需要你動手的事、檔案路徑）並可「接續修正」「加上說明後接續…」或「放棄」，修訂工作卡片也說明成果存到哪裡，並在草稿仍是那一份時提供接續）與「安裝新工具」（貼 OpenAPI JSON 網址 + 指示，AI 背景建置、輪詢進度）兩個分頁；細節見根目錄 README「KB 工具安裝指南」。 |
| `/llm-logs` | `LlmLogsPage` | AI 日誌：呼叫 `GET /api/llm/logs` 列出最近的 LLM 互動，每筆可展開讀取 `GET /api/llm/logs/{id}` 取得的請求/回應內容（每則受 `LLM_LOG_BODY_MAX_CHARS` 截斷）；支援 `?log=<id>` 深連結（`工具` 頁的安裝／修訂結果會連過來）：在清單裡就自動展開該列，不在清單裡（比最近 50 筆更舊）就直接向詳情端點取那一筆、單獨顯示在清單上方，真的被擠出保留區才說明它已經不在；連結若標明自己來自**另一個**後端行程（`?logProcess=`，見下方慣例）則一律不展開，只說明編號已被重新配發。 |
| （其他） | `NotFoundPage` | 404 fallback（router 的 `defaultNotFoundComponent`）。 |

## 慣例

- **Code-based routes**：路由不是檔案系統慣例（無 `routes/` 目錄約定），而是在
  `src/router.jsx` 用 TanStack Router 的 `createRootRoute` / `createRoute` /
  `createRouter` 明確組出 route tree。`/items/$itemId`、`/items/$itemId/edit`
  兩個 route 額外把元件包一層、以 `itemId` 當 React `key`：TanStack Router 在只
  有路徑參數變化時會重用同一個元件實例，強制 remount 才能讓頁面內以 ref 保存的
  in-flight request／mounted 狀態不會誤用到舊的 `itemId`（細節見該檔案內註解）。
- **jotai atoms**：全域狀態拆成細顆粒 atom（`atoms/filters.js` 的篩選/分頁、
  `atoms/llm.js` 的 LLM 設定狀態），搭配 write-only 的 action atom（例如
  `loadLlmStatusAtom`、`resetFiltersAtom`）封裝副作用。`atoms/llm.js` 另外用一個
  模組層級的世代（generation）計數器擋掉過期的非同步寫入——避免一個較舊的
  in-flight 請求在較新的狀態變更之後才 resolve，反而把畫面狀態覆寫回舊的。
- **RHF + Controller**：表單一律用 `react-hook-form` 管理，Mantine 元件（非原生
  `<input>`）一律透過 `Controller` 包裝，不用非受控的 `register` 直接綁定。共用
  的 `ItemForm` 元件（`components/ItemForm.jsx`）供新增／編輯兩個頁面共用同一份
  欄位定義、驗證規則與 dirty-tracking 邏輯。
- **zh-TW 文案**：所有面向使用者的文字（標籤、按鈕、通知、錯誤訊息）一律使用正體
  中文，狀態／階段的顯示文字集中在 `constants/labels.js`（`STATUS_META` /
  `STAGE_META`）避免各處重覆定義；API 錯誤訊息的 zh-TW 映射集中在
  `api/client.js` 的 `messageFor`。
- **Code-point 長度計數**：任何鏡射後端長度上限的前端驗證，一律用
  `utils/text.js` 的 `codePointLength`，而不是 JS 原生的 `.length` /
  `maxLength`。原生 `.length` 數的是 UTF-16 code unit，多數 emoji 與部分 CJK
  擴充字元會被算成 2；後端的 Pydantic 長度限制數的是 Python `len(str)`（Unicode
  code point），兩者必須用同一種數法比對，否則會提前擋下合法輸入，或用原生
  `maxLength` 在使用者輸入到上限前就把字元從中間切斷。
- **Mutation gates**：`ItemDetailPage` 用衍生旗標 `mutationPending`
  （`pagePending` = `patchMutation.isPending || progressMutation.isPending`，
  再疊加 `ItemAiActions` 透過 `onPendingChange` 同步回報的 `aiPending`）同一
  時間只允許一個會改動該項目的操作進行中（狀態/階段快速修改、追加 progress
  note、AI 補齊、AI 進度更新皆共用這個 gate）；任一操作進行中時，其餘**共用此
  gate** 的控制項才會停用，避免兩個併發的 mutation 互相覆蓋對方剛寫入的結果。
  刪除**刻意不納入**這個 gate（它走獨立的確認 Modal，且結果是離開此頁而非改寫
  欄位），所以刪除按鈕不受 `mutationPending` 影響、可與 PATCH／progress／AI
  操作重疊。
  `onPendingChange` 在動作送出／結束當下同步呼叫（不是透過 effect），gate 的
  開關才會跟對應的 mutation 落在同一個 render，不晚一拍。（`@mantine/hooks` 的
  `useDisclosure` 在這個頁面上是用來控制刪除確認 Modal 的開關，與這個
  mutation gate 是兩回事。）
- **`ToolsPage` 的跨分頁 busy gate（D40）**：後端的工具任務（安裝、AI
  修訂）與同步的「重新產生總結」共用同一個全域 single-flight，任一個進行中都會
  讓其他兩者收到 409。`ToolsPage` 的兩個分頁（`InstalledToolsPanel`／
  `InstallPanel`）常駐掛載且維持 effect 存活：`Tabs` 除了 Mantine 預設就開的
  `keepMounted`，還額外指定 `keepMountedMode="display-none"`——Mantine 另一個
  預設值 `"activity"` 會把非現用分頁的內容包進 React 的 `Activity`
  （`mode="hidden"`），隱藏時保留元件 state 但拆掉 effect，等於讓被切走那一
  分頁的 react-query 輪詢與這裡的 `onBusyChange` effect 一起靜默停擺（細節與
  Mantine 原始碼引用見 `ToolsPage` 該 prop 上方的註解）；`display-none` 改用
  純 CSS `display: none` 隱藏未啟用分頁，兩分頁的 effect 因此永遠跟現用分頁
  一樣持續運作。兩分頁各自把自己算出的忙碌旗標經
  `onBusyChange` 回報給 `ToolsPage`，再以 `externalBusy` 傳回給對方，讓一個分頁
  的任務進行中時，另一分頁的送出控制項也會停用——純粹是本地端對後端那個
  single-flight 的樂觀鏡像（只涵蓋這個分頁實例自己送出/得知的任務），後端仍是
  權威，鏡像沒接住的競態（例如另一個瀏覽分頁送出的任務）一樣會用既有的
  結構化 409（版本寫入為 `job_busy`；安裝送出維持 `install_in_progress`）接住。
  **分頁往上報的只能是它第一手知道的
  忙碌**（`ownSummaryBusy`），絕不可把收到的 `externalBusy` 折進去再報回對方——那
  會讓鏡像回聲，安裝表單自己送出的那段窗口會被自己指控成「另一分頁有 AI
  任務正在進行中」；本地的閘（自己的 ＋ 對方的）才是疊加的那一層。每個工具列的
  AI 總結面板走 inline
  展開（`@mantine/core` 的 `Collapse` + `useDisclosure`，零新依賴，比照 D38
  選用 Mantine 內建元件的理由；每列獨立展開，不像 `LlmLogsPage` 的 Accordion
  同時間只開一項；展開 prop 是 `Collapse` 自己的 `expanded`，**不是** React
  Transition Group 的 `in`——寫錯只會被靜默吞進 `...others`，見下面「沒有 jsdom」
  那條），總結內容以 `enabled: expanded` 延遲讀取（比照
  `LlmLogsPage.LogDetailPanel`，收合的列從不打 API）。
- **`ToolsPage` 的 per-row 閘：啟用開關 ⇄ 進行中的修訂／重新產生——web-v5 P1
  之後已經拆掉**。這一條留著是因為它同時記著「當初為什麼需要」與「現在為什麼不
  需要」，照舊文重構的人才不會把鎖裝回去。舊理由是 per-row 的檔案系統競態：
  `PATCH /api/tools/{name}` 會原地改寫該套件的 `tool.json`，而修訂 session 與
  同步的「重新產生總結」都是以 `tool.json` 的 `(st_dev, st_ino, st_ctime_ns)`
  當「還是同一個套件嗎」的身分、在一整趟 LLM 往返之後才重驗——所以一次切換會讓
  整場修訂作廢（「原工具在修訂期間被改動或重新安裝」）、或讓一趟總結往返換到
  404。web-v5 把 `enabled` 搬進套件自己的 `.afterthread.meta/state.json`，
  `tools.set_enabled` 從此**完全不開任何版本的 `tool.json`**，而修訂只新增
  `versions/<vid>` 再切換 `current`，不再替換套件層。因此 state 根本不需要
  搬運或發布鎖：toggle 與版本發布各改自己的名字，
  互不覆蓋。因此：**該列的 `Switch` 不再看修訂／重新產生，兩個 AI 寫入也不再看該列的
  toggle**（`onRegenerate` 裡那個重檢一起拿掉——`disabled` 只是渲染，閘與重檢必須
  同進同退）。使用者拿回來的是：修訂／重新產生跑到一半才決定「這個工具該關掉」時，
  現在按得下去。`Switch` 還留著的兩個條件跟身分無關、各自成立：`!tool.valid`
  （無效套件本來就不會被端給模型，開關對它沒有意義）與 `mutating`（同一列已有
  PATCH／DELETE 在飛，那是重複送出的問題）。**刪除**一樣不受修訂管制（由後端的
  目標不存在拒絕回答，且它走自己的確認 Modal）。
- **`ToolsPage` 的兩個工作查詢在終局後會關掉自己**：`enabled` 用
  `toolJobQueryEnabled(jobId)`（函式型 `enabled`），跟停止輪詢的
  `toolJobRefetchInterval` 共用同一條規則。只停輪詢不夠：app 的 query 預設是
  `refetchOnWindowFocus: true` ＋ `staleTime: 5s`，`enabled` 留著 true 的查詢會在
  每次視窗對焦重打，而後端工作表是行程內有界的，於是幾分鐘後一張「修訂完成」會被
  換成 404 錯誤卡。
- **真實版本身分取代 `name + description` 代理**：`GET /api/tools` 現在提供
  `current_vid`，所以 `toolInstanceKey(name, currentVid)` 與
  `toolSummaryQueryKey(name, currentVid)` 不再猜測。舊代理在 discard 前後兩版
  description 相同時不會 remount，寫給 V 的修訂草稿就可能被送給 P；vid 直接表達
  畫面所描述的版本，才關得掉這個單一分頁內就能發生的錯誤。
- **版本身分的三個消費端一起移動**：`toolIdentityConsumers` 從同一把
  `name + current_vid` 身分一次產生列的 React key、summary query key 與修訂工作卡
  歸屬。身分一變就 remount、清掉列內草稿；總結不跨版本重用；工作卡留在送出時的
  版本，若該列消失就移到 panel 層顯示。這裡是**三個**完整消費端：以前所稱的第四個
  是「定版」寫入排序 ledger，已隨功能移除，不能為了維持數字留一份無人讀的 state。
  重新讀取（invalidate／removeQueries）用名稱前綴——它只是叫伺服器再答一次，
  只可能拿到當下的答案，涵蓋得寬鬆才是保守方向。
  按名稱比對只剩 loading 指示；決定「卡片屬於哪一列」一律用實例身分。
- **name-addressed summary GET 也要驗版本**：query key 含 vid 還不夠，因為請求送出
  時 V 是 current、後端真正解析時可能已切到 P。`ToolSummaryDetail.current_vid`
  是後端實際讀到的版本；`acceptSummaryForVersion` 不符就 throw，不把 payload
  交給 TanStack Query，避免 P 的內容寫進 V 的 cache。
- **所有版本寫入都帶 optimistic identity**：revise 與 regenerate body 帶
  `expected_vid`，discard path 帶 vid。前端 key 只保護本分頁的畫面，保護不了另一
  分頁或終端機改動 `current`；後端在取得 global slot 後比對，才保證不會對已不是
  畫面那一版的程式花掉一場 LLM 或切錯 pointer。`versionWritesBlocked` 同時管
  regenerate／revise／discard，在 list 已知 stale、正在 refetch、single-flight busy
  或 job 終局重讀尚未落地時停用；讀取、toggle 與整包刪除仍可用。
- **lineage 控制是三態**：`sole` 顯示「丟掉這一版」但走既有整包刪除確認；
  `usable` 提供輕量的「丟掉並退回」確認；`broken` 不送 discard，顯示前一版缺失／
  無效／自指與整包刪除的補救。若 `current_vid=null`，那是 unresolved row：
  顯示後端原因、隱藏 toggle／summary／revise／regenerate／discard，只留整包刪除；
  `description` 也可能為 null，不能拿它當身分或假定一定可 render。
- **權威回應寫進快取前，一定要先取消同一把鍵上在飛的讀**：`setQueryData` 不會動
  in-flight 的 fetch，所以一個在 mutation 之前因視窗對焦發出、讀到舊值的 GET
  可以在寫入之後才落地，把畫面翻回舊資料，而且**不會有任何錯誤提示**（那個 GET
  是成功的）。共用的寫入路徑一律先 `await cancelQueries({queryKey})` 再
  `setQueryData`。`removeQueries` 不需要這道手續：`queryCache.remove()` 會
  `query.destroy()` → `cancel({silent: true})`，本來就取消得掉。
- **寫進快取只能「改已存在的項」，不能「建出新的項」**：`removeQueries` 攔不住
  已經在飛的請求，所以刪除之後才落地的 regenerate 回應會把剛清掉的項**重建**
  出來，之後同名同描述的重裝一展開就撞到它。用 query-core 自己的規則解：
  `setQueryData` 的 updater 回傳 `undefined` 時，它會在 `queryCache.build()` **之前**
  就 return（`build/modern/queryClient.js` 第 99-101 行），等於「只在已存在時寫」。
  `writeSummaryDetailIfPresent` 就是這個 updater。
- **「還有東西看不見」是這頁的一類 bug，不是個案**：清單背景 refetch 失敗時列會
  留在畫面上（react-query 保留 `data` 只翻 status），必須用非阻擋的橘色 Alert
  講明清單可能過期；修訂中的工具被刪掉／被同名重裝換掉時，列內的進度卡會跟著
  消失但輪詢與忙碌閘還在，所以沒有任何一列擁有那個工作時，改由面板層渲染同一張
  卡（兩個條件是同一把鍵上的互補，卡片永遠恰好顯示一次）。
- **警告不等於保證：清單已知過期時，版本寫入要真的停用**：清單背景
  refetch 失敗時列的實例身分跟列本身一樣是過期的，列不會 remount、未送出的修訂
  意見留著。後端 `expected_vid` 會拒絕錯版，但畫面既然無法知道目前版本，仍不應
  提供必然可能失敗的動作。所以 `versionWritesBlocked`
  是**一個值、一個 prop**（`writesBlocked`），同時管「重新產生」與「送出修訂」的
  `disabled` **和**送出處的提前 return，並延伸到 discard（`disabled` 只是渲染，不是
  閘），Alert 也要寫明控制項已停用。**讀取不受管制**（展開面板只會 GET）。啟用開關與刪除不納入（意圖本來
  就是「叫這個名字的工具」，可還原或有確認 Modal，也沒有夾帶為某個實例寫的內容）。
- **「對著畫面上的內容寫意見」要求畫面是最新的**，所以
  `displayedMayBeStale` 同時管**修訂意見輸入框與「送出修訂」**。收合的
  列 query 是 disabled，所以修訂結束時那次 invalidation 沒發出任何 GET 就 resolve 了、
  settling 閘照樣解除；使用者稍後展開，看到的是**修訂前**的快取總結（有 data、所以
  沒有 Loader），在那份文字底下寫的意見會被 AI 套到**已經改過**的程式碼上，而那要花掉
  好幾分鐘的 LLM 重寫。
  輸入框跟著一起停用而不是只停按鈕：讓人打完一整段才發現按鈕是死的，是同一個拒絕更
  糟的版本。
- **三種結構化 409 是三個不同動作**：`version_mismatch` 只重抓列表，讓新的
  `current_vid` remount 列並清掉草稿；`job_busy` 保留輸入與 discard confirmation，
  讓操作者稍後重試，不 refetch；`lineage_unavailable` 不重試、不 refetch，把該列
  本地標成 broken 並顯示整包刪除。分支只看結構化 code，不解析 zh-TW 訊息。
  其他錯誤中，`404`（工具已不叫這個名字）才重新讀總結與清單；
  `llm_not_configured`／502／5xx／傳輸失敗沒有證據證明 server state 已移動，刻意
  不重讀。規則分別在 `versionWriteConflictReaction` 與
  `summaryErrorRevalidates` 並逐條測試。修訂**工作**是唯一的例外：
  `ToolJobStatus` 沒有結構化原因、只有一段
  後端每輪都在改寫的 zh-TW `error` 字串，字串比對是會靜默失效的閘，所以改成「終局
  轉換（成功或失敗）就重讀」——那是每個工作最多一次、且發生在數分鐘工作之後，不是
  「每個錯誤都重抓」。
- **`utils/toolInstall.js` 與 `utils/toolSummary.js` 的分工**：前者是安裝表單
  驗證（URL／秘密名稱與值）＋工具任務輪詢共用的純函式
  （`isToolJobActive`／`toolJobRefetchInterval`／`isTerminalToolJobState`——
  D40 之前只服務安裝工作，現在安裝與修訂共用同一張後端 job 表與同一個輪詢
  路由，因此改用不含「install」字樣的名稱，並更新了每個呼叫點與測試）；後者
  是 AI 總結網域的純邏輯（快取鍵 `toolSummaryQueryKey`／`toolSummaryKeyPrefix`／
  列的 `toolInstanceKey`、面板自己的忙碌旗標 `ownSummaryBusy`、詳情的「只在已存在
  時寫」updater `writeSummaryDetailIfPresent`、失敗要不要重讀的判準
  `summaryErrorRevalidates`），因為那與「安裝」無關，硬塞
  進前者的檔名只會誤導之後的讀者——這個專案的 vitest 在 node 環境跑、沒有
  jsdom，元件本身測不到，抽出的純函式是唯一能自動化驗證的介面，所以新邏輯一律
  先問「這算安裝、總結，還是套件設定」再決定放哪個檔案。第三個檔案是 web-v6 的
  `utils/toolSettings.js`：套件層 `.env`／`config.env` 唯讀「設定」區的措辭與旗標
  （`configEntryFlags`、`settingsNotices`、`occurrenceKeys`），它的元件是
  `components/ToolSettingsSection.jsx`——那份資料屬於套件、跨版本存在，不屬於某一版的
  總結。第四個是 `utils/toolRuns.js`：執行紀錄（web-v6 §6）的措辭與列上的提示
  （`lastRunHint`、`runOutcomeMeta`、`vidStateMeta`、`runDetailSections`），元件是
  `components/ToolRunsSection.jsx`；**一鍵修正能不能給，只看後端算好的 `fixable`**，
  這裡不重算。一鍵修正就是一次附上 `run_ids` 的修訂：同一個 `reviseMutation`、同一個寫入閘與
  工作卡片（`buildReviseRequest` 帶 `runIds`、空白意見不送）；修訂卡片一律顯示在那一列上，不放在
  AI 總結裡（總結讀不到時整個面板會提早返回，卡片會跟著消失）。「最近執行」的修正按鈕在清單
  重新讀取中或讀取失敗時停用：`fixable` 是讀取當下的判斷。purge 的請求內容與措辭在 `utils/toolRunsPurge.js`，元件是 `ToolRunsPurgeDialog`／
  `ToolRunStatsBar`。AI 結果旁的工具失敗提醒（擷取、補齊、協助更新）在
  `components/ToolFailureNotice.jsx`，分組、「能不能送出」的判斷、按下後的流程（`requestFix`）與
  提醒的畫面（`failureNoticeView`）在 `utils/toolFailures.js`；
  `/tools?job=<id>` 的驗證（`toolsJobSearch`）也在那裡，工具頁以 `useSearch` 讀取。範例的驗證
  （「最近執行」的驗證狀態、工作卡片上的逐個結果、可以附上的失敗紀錄）在 `utils/toolChecks.js`；
  「跑一次範例」是一種 job，沿用修訂的工作追蹤與寫入閘。工作流的中文名稱 `WORKFLOW_LABELS` 在 `constants/labels.js`，時間與耗時的
  格式在 `utils/format.js`，`AI 日誌` 頁與執行紀錄共用。修訂工作的終局呈現 `toolJobOutcomeView` 在 `toolInstall.js`（job 輪詢那一組），
  送出被 `env_policy` 擋下時的通知 `reviseRefusalNotification` 在 `toolSummary.js`
  （修訂請求那一組）。**唯一的例外寫在
  `toolSummary.js` 的最後一節**：AI 日誌深連結的**兩半**（`工具` 頁產生連結的
  `logLinkSearch`、`AI 日誌` 頁解讀連結的 `deepLinkTarget`）刻意放在同一個檔案，
  因為它們是同一份約定；拆開的話，發出主張的那一頁和依主張行動的那一頁會各自漂移，
  而且只有其中一半會有測試（`LlmLogsPage` 因此從這裡 import 它那一半）。
- **`?log=` 深連結帶著它的行程 token**：AI 日誌的 id 是**每個後端行程各自從 0 開始**
  的計數器，而 `工具` 頁的工作卡片在工作終局後就停止輪詢、快取無限期留著——分頁跨過
  一次後端重啟，那張卡片的「查看 AI 日誌」還在，`llm_log_id` 卻已經被重新配發給
  另一次互動。所以工作回應多帶一個 `llm_log_process`，連結變成
  `?log=<id>&logProcess=<token>`（`logProcess` 不在 route 的 `validateSearch` 裡，
  但 TanStack Router 會把解析到的其他 search 併進 match，`useSearch({strict:false})`
  讀得到——已對安裝版 1.170.17 實測）；`AI 日誌` 頁拿清單回應的 `process_token` 比對，
  **不符就不展開任何列**，改在清單上方說明這個連結來自先前的後端執行。**沒有帶
  token 的連結維持原本行為**（`工具` 頁總結面板的連結就是這種：後端在**回應當下**
  就把非本行程的 `llm_log_id` 改成 `null`，所以它不需要也無從提出主張；使用者自己
  存下來的網址同理——「說不出來」不可以被講成「我確定它過期了」）。
- **工具的草稿（沒發布的修訂）**：修訂沒有發布時，後端把這一輪的成果存成一份草稿
  （格式與狀態見 `docs/web-v7-design.md`），`GET /api/tools` 的每一列帶 `draft`；可接續的
  草稿可以再送一輪修訂接著做。前端的判斷全在純函式 `utils/toolDraft.js`，元件只照它畫：
  - `draftView(draft, { currentVid })` 把草稿轉成畫面：標題、說明句、`buttons`、輪數、
    上一輪怎麼結束、模型寫的「需要你動手」與總結摘錄、路徑（草稿目前的檔案、整個草稿目錄
    `draft_dir`、保留下來的舊檔案目錄 `kept`，各附「複製路徑」，並說明草稿目錄裡可能還有
    沒列出的目錄）。元件是 `components/DraftNotice.jsx`，掛在該列的修訂工作卡片之下。
    **`buttons`**：可接續（`open`）給「接續修正」「加上說明後接續…」與「放棄」；處理中
    （`running`）與發布中（`publishing`）不給按鈕；其餘狀態（已過期、無法使用、已發布、
    結果未知）只能放棄。「發布草稿」還沒有 API，所以任何文案都不能叫人按它。「已發布」的
    說明不說放棄就好：發布後草稿留下來，可能是沒能刪掉，也可能是後端發現它和發布前存的那份
    不同（被改過或多了東西）、或無法確認，所以說明句請使用者先到路徑取回需要的內容再放棄。
    後端 `ToolDraft.state` 的每個值都要有文案：`toolDraft.test.js` 手抄一份後端的狀態清單，
    斷言 `KNOWN_DRAFT_STATES` 與它相同；後端新增狀態時，要先補這份清單，測試才會要求補文案。
  - **文字不說草稿的檔案是哪一輪的**：保存沒存成的一輪也算一輪，操作者也可能手改過檔案，
    所以草稿提示只說輪數（嘗試的次數），被中斷的一輪說「上一輪沒有正常結束，它的進度沒有
    存進草稿」；說明檔記成 `interrupted` 的一輪（`last.finished_at` 是 null）照樣顯示，不帶
    結束時間。唯一說出輪次的是剛保存成功的那張卡片「進度已保存（第 N 輪）」。
  - **接續**：`draftContinuation(draft)` 從清單的草稿取出 `draft_id`、`generation`、
    `base_vid`，只有 `open` 而且三者都在才有；`utils/toolSummary.js` 的
    `buildReviseRequest({ ..., continuation })` 把它送成 `draft_id`、`draft_generation`，
    `expected_vid` 用草稿的 `base_vid`，`feedback`（這一輪的說明）與 `run_ids` 都可以省略。
    用的一律是清單的身分、不是 job 的：草稿在讀到之後變了，後端回 `draft_changed`，不會接續
    沒看過的那一份。「接續修正」「加上說明後接續…」在 `components/DraftContinueButtons.jsx`
    （草稿提示與工作卡片共用）；說明的上限與其他 AI 輸入一樣是 20,000 個 code point
    （`continuationNote`），可以留空。接續是一次版本寫入，所以按鈕跟「送出修訂」一樣受
    `versionWritesBlocked` 管，頁面的 handler（`continueDraft`）按下時再查一次。
  - **附上紀錄的修正**：草稿可接續時，該列上一筆失敗與「最近執行」每一筆的「請 AI 修正」
    換成「接續修正（附上這筆）」，旁邊的「加上說明…」也是接續（`recordFixView`）；其他狀態
    照舊停用並說明原因。範例驗證失敗的「請 AI 修正」不接續，有草稿時照舊停用。
  - **其他頁面不接續**：`ToolFailureNotice` 的「請 AI 修正」按下時讀一次清單，工具有草稿
    （不分狀態，判準同 `reviseBlockedByDraft`）就不送出，只說明工具有一份草稿、請到工具頁接續
    或放棄（`fixDecision` 回 `draft`），旁邊就是「到工具頁查看」；沒有草稿才送第一輪，不帶
    草稿身分。那一頁看不到草稿「需要你動手」的事，從那裡接續，操作者可能在改設定之前就付了一輪
    的錢。
  - **「無法使用」有兩種形狀**：說明檔讀不出來時，`draft_id`、`generation`、
    `attempts`、`base_vid`、`last` 都是 null；說明檔讀得出來、檔案目錄卻不見時，這些
    欄位有值。兩種的 `path` 都是草稿目錄，`draftView` 兩種都讀得了。
  - **放棄的請求**（`buildDraftDiscardRequest`）：可讀的草稿帶 `draft_id` 與
    `generation`，無法使用的（兩種形狀都算）帶 `discard_token`，只送其中一種，後端對
    兩種都帶或都沒帶回 422。確認框記住使用者按下「放棄」當下看到的那一份草稿，確認時
    送的就是那一份；其間若換成別份，後端回 `draft_changed`（`draftDiscardFailure`：
    重讀清單並說明），不會刪到沒看過的草稿。`draft_not_found` 重讀清單，`job_busy`
    請稍後再試；`500 draft_not_removed`（沒能確認草稿已刪除，或一開始就看不到它而沒動手）
    照後端的訊息顯示——它寫出 `draft/` 的路徑與下一步——通知不自動關閉，並重讀清單
    （`draft/` 可能已改名移走、只是之後的查詢失敗）；放棄成功而有殘留（`residual_path`）時，通知會說殘留在哪
    （`draftDiscardNotification`）。確認框（`draftDiscardConfirmation`）說整個草稿目錄都會
    刪掉，包括保留下來的舊目錄與其他內容，需要的請先取回。放棄的請求佔用後端的全域名額，
    所以它的 pending 併入 `ownSummaryBusy`，和其他寫入一起停用。
  - **工作卡片分兩層**：`components/ReviseDraftResult.jsx` 依
    `jobDraftView(job, kind, context)` 畫。
    - **這一輪自己的結果一律顯示**，不看清單：它可能是成果唯一的取回位置。「進度已保存
      （第 N 輪）」之後帶後端的附註（`reason`，例如保留了上一份檔案目錄）與另外留著的路徑
      （`retained_path`：說明檔沒確認落盤）；「未能保存（原因）」；「成果留在 <路徑>，
      未能存成草稿」只用在 `saved` 為 false；已發布的修訂只顯示後端說草稿為什麼還在
      （不說「進度已保存」）。
    - **對目前草稿的宣稱與按鈕**只在三件事都成立時出現：卡片有對應的工具列（沒有對應的
      卡片什麼都不說、不給按鈕）；清單的草稿與 job 的 `draft_id`、`generation` 都相同；
      清單來自看到 job 結束**之後才發起、而且成功**的那次讀取。generation 或 `draft_id`
      不同時說「目前的草稿已不是這張卡片對應的那一份」，不推論發生了什麼、不給按鈕；草稿
      可接續才給「接續修正」「加上說明後接續…」，「放棄」只在工具列。目前的 job 與從其他
      頁面接上的 job（`?job=`）都一樣，後者的工具列就是同名的那一列，身分比對擋得住同名重裝。
    - **「之後才發起」怎麼判斷**（`utils/toolList.js`）：每次讀清單在**開始時**取一個遞增的
      編號（`readToolList`，存在清單資料裡的 `readSeq`；`ToolFailureNotice` 讀同一個快取鍵，
      也走它），看到 job 結束時從同一個計數器取一個標記（`markListReads`），卡片比較兩者
      （`listReadAfter`）。重讀失敗不會產生資料，畫面上的清單保有舊編號，所以不算；結束前就
      在飛、結束後才回來的讀取編號比標記小，也不算。job 結束後的重讀（`rereadToolList`）先
      取消在飛的讀取再讀：第一次載入還在飛時，react-query 會讓新的重讀直接併進那次舊的讀取
      （query-core 的 `Query.fetch` 在沒有資料時沿用執行中的 retryer），那次讀取的編號早於
      標記。計數器放在模組層：快取裡的清單活得比頁面元件久，元件重掛時計數器若從零開始，舊
      清單的編號會大過新的標記。只看「重讀已 settle」（`settledJobId`）不夠：`finally` 在
      重讀失敗時也會跑。那個旗標仍只管寫入閘。
    - 它和 `ReviseStopNotice`（說這一輪為何停止）各管一件事，分開。
  - 上一輪的停止原因沿用 `utils/toolJobStop.js` 的詞彙（`toolJobStopReason`）；草稿
    沒有保存工具回合數，所以句子不帶「用了 N／M 次」。
  - **`draft_pending`**（送出一次從目前版本重新開始的修訂，工具卻有草稿）：後端依草稿狀態
    給兩種說法（可接續時說接續或放棄，其他說先放棄）。工具頁用 `reviseRefusalNotification`
    的通知照原文顯示（訊息本身就是指示，不自動關閉），並依 `versionWriteConflictReaction`
    重讀清單，讓該列顯示出訊息說的草稿；其他頁面（`ToolFailureNotice`）只在清單讀到之後才
    出現的草稿會遇到它，`fixRefusalMessage` 用和清單上有草稿時同一句話說明並請使用者到工具頁。
  - **接續被拒**：`draft_gone`（草稿已不在或換成另一份）、`draft_changed`（讀到之後被改
    過）、`draft_unusable`（看不到、說明檔讀不出或檔案目錄不在）、`draft_publishing`（正在
    發布、已發布或結果未知）。工具頁一律重讀清單（`versionWriteConflictReaction` 回
    `refresh`），通知用我們自己的話說明發生了什麼（`reviseRefusalNotification`；後兩種的
    下一步在草稿本身，不自動關閉）；其他頁面不送接續，收不到這四個碼。草稿的 `base_vid` 已不是
    `current` 時，後端回的是 `version_mismatch`，照舊處理。
  - **有草稿時，從目前版本重新開始的修訂停用並在旁邊說明原因**：「送出修訂」與範例驗證
    失敗的「請 AI 修正」在該列的 `draft` 不是 null 時停用（不分狀態，`draft_id` 是 null 的
    「無法使用」也算），判準與原因句只在 `utils/toolDraft.js` 的 `reviseBlockedByDraft(draft)`，
    可接續的草稿另外指向「接續修正」；元件（`components/ReviseBlockedNote.jsx`）只畫它的
    回答，送出的 handler 在按下時也再查一次；跑一次範例、重新產生、丟掉這一版、刪除與
    「放棄」不是修訂，不停用。
  - 路徑旁的「複製路徑」靠瀏覽器的剪貼簿，只有 https 或 localhost 才開放；開不了時
    會說明，路徑區塊點一下就全選，仍可手動複製。
- **沒有 jsdom ⇒ 寫錯的 prop 名稱沒有任何閘門擋得住**：`pnpm lint`（Biome）不做
  型別檢查、`pnpm build`（Vite）只轉譯不檢型別、`pnpm test`（vitest）在 node 環境
  下完全不 render 元件。一個拼錯的 Mantine prop 是合法 JS／合法 JSX，會被靜默
  spread 進 `...others`，三個閘門依然全綠而功能是零（P4 的
  `<Collapse in={…}>` 就是這樣讓整個 AI 總結面板從未打開過）。改動 Mantine 元件的
  props 時，唯一可靠的驗證是**對照安裝版原始碼**——
  `node_modules/@mantine/core/lib/components/<Name>/<Name>.d.ts` 的介面宣告，或
  `esm/.../<Name>.mjs` 的解構，style props 則見
  `lib/core/Box/style-props/style-props.types.d.ts`。憑記憶或憑線上文件都不算。
