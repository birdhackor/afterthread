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
| `/tools` | `ToolsPage` | 「已安裝工具」（清單／啟停／刪除，每列可展開讀取／重新產生／定版／解除定版 AI 總結，並可提意見送出 AI 修訂）與「安裝新工具」（貼 OpenAPI JSON 網址 + 指示，AI 背景建置、輪詢進度）兩個分頁；細節見根目錄 README「KB 工具安裝指南」。 |
| `/llm-logs` | `LlmLogsPage` | AI 日誌：呼叫 `GET /api/llm/logs` 列出最近的 LLM 互動，每筆可展開讀取 `GET /api/llm/logs/{id}` 取得的請求/回應內容（每則受 `LLM_LOG_BODY_MAX_CHARS` 截斷）；支援 `?log=<id>` 深連結自動展開（`工具` 頁的安裝結果會連過來）。 |
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
  409（`tool_job_in_progress`）錯誤處理接住。**分頁往上報的只能是它第一手知道的
  忙碌**（`ownSummaryBusy`），絕不可把收到的 `externalBusy` 折進去再報回對方——那
  會讓鏡像回聲，安裝表單自己送出的那段窗口會被自己指控成「另一分頁有 AI
  任務正在進行中」；本地的閘（自己的 ＋ 對方的）才是疊加的那一層。每個工具列的
  AI 總結面板走 inline
  展開（`@mantine/core` 的 `Collapse` + `useDisclosure`，零新依賴，比照 D38
  選用 Mantine 內建元件的理由；每列獨立展開，不像 `LlmLogsPage` 的 Accordion
  同時間只開一項；展開 prop 是 `Collapse` 自己的 `expanded`，**不是** React
  Transition Group 的 `in`——寫錯只會被靜默吞進 `...others`，見下面「沒有 jsdom」
  那條），總結內容以 `enabled: expanded` 延遲讀取（比照
  `LlmLogsPage.LogDetailPanel`，收合的列從不打 API）。定版／解除定版
  （`PATCH .../summary`）刻意**不**受這個 busy gate 管制：後端這個端點本來就
  沒有查 single-flight，且刻意支援「AI 修訂進行中先定版，換裝前重新檢查會擋下
  取代」這種中途操作，在前端補一個它不需要的鎖只會擋掉後端特地支援的動作。
  （這說的是 busy gate；**「定版」方向另有自己的一組閘**，見下面「定版／解除定版
  不吃 `writesBlocked`」那條——不受這個 gate 管制不等於不受任何 gate 管制。）
- **`ToolsPage` 的 per-row 閘：啟用開關 ⇄ 進行中的修訂**：跨分頁的 `summaryBusy`
  管的是後端那個全域 single-flight，但「啟用／停用」跟修訂的衝突是**另一回事**、
  而且是 per-row 的檔案系統競態：`PATCH /api/tools/{name}` 會原地改寫該套件的
  `tool.json`，而修訂 session 正是以 `tool.json` 的 `(st_dev, st_ino, st_ctime_ns)`
  當「還是同一個套件嗎」的身分、換裝前再驗一次。所以該列的 `Switch` 會被該列自己的
  修訂（送出中或工作進行中）停用，該列的「送出修訂」也會被該列自己的 toggle
  停用；只鎖同一列，別的工具不連坐。**刪除刻意不在這個閘內**（由後端的目標不存在
  拒絕回答，且它走自己的確認 Modal）。
- **`ToolsPage` 的兩個工作查詢在終局後會關掉自己**：`enabled` 用
  `toolJobQueryEnabled(jobId)`（函式型 `enabled`），跟停止輪詢的
  `toolJobRefetchInterval` 共用同一條規則。只停輪詢不夠：app 的 query 預設是
  `refetchOnWindowFocus: true` ＋ `staleTime: 5s`，`enabled` 留著 true 的查詢會在
  每次視窗對焦重打，而後端工作表是行程內有界的，於是幾分鐘後一張「修訂完成」會被
  換成 404 錯誤卡。
- **總結快取鍵帶實例判別子**：`toolSummaryQueryKey(name, description)`。工具名稱
  是可以被重新指派的（別的分頁刪掉再用同名裝一個不同的工具），而
  `GET /api/tools` 的列裡沒有任何安裝 id／時間戳可用，`description` 是唯一由安裝
  寫出來的欄位。判別子是啟發式而非證明，所以面板另外在 `isError` 但**有**舊資料時
  加一張「內容可能已過期」的 Alert——react-query 會保留 `data` 只把 status 翻成
  'error'，那正是舊內容原本會無聲留在畫面上的狀態。做 invalidate／removeQueries
  時一律用 `toolSummaryKeyPrefix(name)` 前綴比對，**不可以**加 `exact`（鍵是三段，
  `exact` 會一個都比對不到）。
- **同一個判別子必須同時管快取鍵、列的 React key、工作卡的歸屬與列徽章的補寫**：
  四者是同一個「這還是同一個工具嗎」的問題。列的 key 用
  `toolInstanceKey(name, description)`
  （＝把 `toolSummaryQueryKey` 那把鍵 `JSON.stringify`，所以兩者不可能各自漂移），
  身分一變就 remount、列內未送出的修訂意見不會跨過同名重裝被送給別的工具；修訂
  工作卡也以同一把鍵決定歸屬（`activeJob` 記下送出當下那一列的 `description`）；
  `patchToolRowSummaryStatus(listBody, instanceKey, status)` 同樣**按實例身分**找
  列——一個回應同時要寫詳情快取與列徽章，兩邊必須指同一個工具，否則同名重裝之後
  詳情寫進 A 的項、徽章卻蓋到 B 的列，而且沒有任何請求失敗來說明。**規則是：寫入
  用實例身分，重新讀取（invalidate／removeQueries）用名稱前綴**——後者只是叫伺服器
  再答一次，只可能拿到當下的答案，涵蓋得寬鬆才是保守方向。
  **「閘」也刻意維持按名稱比對**（`reviseBusyForThisTool`／`isTogglingThisTool`）：
  `PATCH /api/tools/{name}` 與 `POST .../revise` 都按名稱定址，它們防的檔案系統
  競態會落在「當下叫這個名字的套件」；決定「卡片屬於哪一列」「徽章要蓋哪一列」
  則相反。
- **權威回應寫進快取前，一定要先取消同一把鍵上在飛的讀**：`setQueryData` 不會動
  in-flight 的 fetch，所以一個在 mutation 之前因視窗對焦發出、讀到舊值的 GET
  可以在寫入之後才落地，把畫面翻回舊資料，而且**不會有任何錯誤提示**（那個 GET
  是成功的）。共用的寫入路徑一律先 `await cancelQueries({queryKey})` 再
  `setQueryData`。`removeQueries` 不需要這道手續：`queryCache.remove()` 會
  `query.destroy()` → `cancel({silent: true})`，本來就取消得掉。
- **取消只排序了「寫 vs 讀」，寫與寫要另外排**：「重新產生」與「定版／解除定版」
  可以同時在飛（定版刻意不受忙碌閘管制，見上），寫同一個快取項時**先回來的不一定
  是先送出的**，落後的那個會把畫面永久留在舊答案上，而兩個請求都成功、沒有任何提示。
  作法是**發出當下**取一個單調遞增的號碼（`createSummaryWriteLedger` /
  `nextSummaryWriteStamp`，比照 `atoms/llm.js` 的世代計數器），套用前用
  `claimLatestSummaryWrite` 比對；**嚴格較舊**的回應直接丟掉。刻意**不**改成「兩個
  mutation 互鎖」：那正是後端特地不需要的鎖，會把定版這條逃生路在最需要的時候關掉。
  問兩次是有意的——`cancelQueries` 是一個 await，較新的回應可能在那個窗口內插進來，
  所以取消前問一次（避免落後的回應去取消較新寫入剛啟動的重新讀取）、取消後再問一次。
- **寫進快取只能「改已存在的項」，不能「建出新的項」**：`removeQueries` 攔不住
  已經在飛的請求，所以刪除之後才落地的 regenerate／定版回應會把剛清掉的項**重建**
  出來，之後同名同描述的重裝一展開就撞到它。用 query-core 自己的規則解：
  `setQueryData` 的 updater 回傳 `undefined` 時，它會在 `queryCache.build()` **之前**
  就 return（`build/modern/queryClient.js` 第 99-101 行），等於「只在已存在時寫」。
  `writeSummaryDetailIfPresent` 就是這個 updater；`patchToolRowSummaryStatus` 因為
  看不懂的 body 原樣返回，本來就已經符合同一條規則。
- **「還有東西看不見」是這頁的一類 bug，不是個案**：清單背景 refetch 失敗時列會
  留在畫面上（react-query 保留 `data` 只翻 status），必須用非阻擋的橘色 Alert
  講明清單可能過期；修訂中的工具被刪掉／被同名重裝換掉時，列內的進度卡會跟著
  消失但輪詢與忙碌閘還在，所以沒有任何一列擁有那個工作時，改由面板層渲染同一張
  卡（兩個條件是同一把鍵上的互補，卡片永遠恰好顯示一次）。
- **警告不等於保證：清單已知過期時，按名稱定址的 AI 寫入要真的停用**：清單背景
  refetch 失敗時列的實例身分跟列本身一樣是過期的，列不會 remount、未送出的修訂
  意見留著，而「送出修訂」是打到 `/api/tools/{名稱}/revise`——同名重裝之後那就是
  另一個工具。所以 `summaryWritesBlocked = summaryBusy || staleList || settlingJobEnd`
  是**一個值、一個 prop**（`writesBlocked`），同時管「重新產生」與「送出修訂」的
  `disabled` **和**送出處的提前 return（`disabled` 只是渲染，不是閘），Alert 也要寫明
  控制項已停用。**讀取不受管制**（展開面板只會 GET）。啟用開關與刪除不納入（意圖本來
  就是「叫這個名字的工具」，可還原或有確認 Modal，也沒有夾帶為某個實例寫的內容）。
- **定版／解除定版不吃 `writesBlocked`，但「定版」這個方向另有自己的閘**——這一條
  是規則，不是這顆按鈕的特例，照著 README 重構的人必須先讀懂它才不會把洞裝回去：
  **凍結需要看得到現況，釋放不需要**。「定版」的語意是「把我正在看的內容凍起來」，
  所以只要畫面上的東西**已知可能不是現況**就不能按；「解除定版」只是把凍結放掉，
  它不凍結任何內容，而且是後端刻意做成無條件的逃生路（D40 r6）——一個持久的讀取
  失敗若能鎖住它，操作者就會卡在「已定版且無路可退」。因此：
  - **不納入 `writesBlocked`**：那個值折進了 `staleList` 這種**持久**條件，一個已定版
    的工具本來就被 `tool_finalized` 擋掉另外兩個動作，連它也鎖住等於讓操作者無路可走；
    而且它只改一個列舉欄位、按反方向就能還原，其啟用條件是算自面板自己那個**按名稱**
    讀回來的總結，不是算自過期的列。
  - **但只有「定版」方向另外受三個「畫面可能不是現況」的條件管制**（見 `ToolRow`
    該按鈕上方的長註解）：`settlingJobEnd`（剛結束的工作讓它過期，一次往返會自己清掉）、
    `isFetching`（任何背景更新還沒落地——收合的列 query 是 disabled，那次 invalidation
    根本沒發 GET，展開時會先渲染修訂前的快取內容）、`isError`（重讀**失敗**：
    TanStack Query 保留舊 `data`、`isFetching` 回到 false，閘會全開而畫面是舊的）。
    這三個都套在 `!isFinal` 這一側，**解除定版永遠不受它們影響**（D40 r8／r10／r11）。
- **哪些失敗要重新讀取，要逐碼講清楚**：一個拒絕不只是訊息，有些拒絕本身就是伺服器
  在說「我已經不是你畫面上那個樣子了」——`404`（工具已不叫這個名字）、409
  `tool_finalized`（我們的控制項是開著的，代表快取說它不是 final）、409
  `summary_missing`（定版是拿快取裡的文字判斷可不可按的）三者都證明快取過期，就
  重新讀總結＋清單（列徽章與詳情 status 是同一個側檔欄位）。`tool_job_in_progress`
  （還沒寫任何東西）、`llm_not_configured`／502（在寫側檔之前就失敗）、5xx／傳輸
  失敗（沒有任何證據）刻意**不**重讀——規則寫在純函式 `summaryErrorRevalidates`
  裡並逐條測試。修訂**工作**是唯一的例外：`ToolJobStatus` 沒有結構化原因、只有一段
  後端每輪都在改寫的 zh-TW `error` 字串，字串比對是會靜默失效的閘，所以改成「終局
  轉換（成功或失敗）就重讀」——那是每個工作最多一次、且發生在數分鐘工作之後，不是
  「每個錯誤都重抓」。
- **`utils/toolInstall.js` 與 `utils/toolSummary.js` 的分工**：前者是安裝表單
  驗證（URL／秘密名稱與值）＋工具任務輪詢共用的純函式
  （`isToolJobActive`／`toolJobRefetchInterval`／`isTerminalToolJobState`——
  D40 之前只服務安裝工作，現在安裝與修訂共用同一張後端 job 表與同一個輪詢
  路由，因此改用不含「install」字樣的名稱，並更新了每個呼叫點與測試）；後者
  是 AI 總結網域的純邏輯（狀態→badge 對映 `summaryStatusMeta`、是否可定版
  `canFinalizeSummary`、快取鍵 `toolSummaryQueryKey`／`toolSummaryKeyPrefix`／
  列的 `toolInstanceKey`、面板自己的忙碌旗標 `ownSummaryBusy`、
  列徽章的快取更新器 `patchToolRowSummaryStatus`、詳情的「只在已存在時寫」updater
  `writeSummaryDetailIfPresent`、寫入排序帳本
  `createSummaryWriteLedger`／`nextSummaryWriteStamp`／`claimLatestSummaryWrite`、
  失敗要不要重讀的判準 `summaryErrorRevalidates`），因為那與「安裝」無關，硬塞
  進前者的檔名只會誤導之後的讀者——這個專案的 vitest 在 node 環境跑、沒有
  jsdom，元件本身測不到，抽出的純函式是唯一能自動化驗證的介面，所以新邏輯一律
  先問「這算安裝，還是總結」再決定放哪個檔案。
- **沒有 jsdom ⇒ 寫錯的 prop 名稱沒有任何閘門擋得住**：`pnpm lint`（Biome）不做
  型別檢查、`pnpm build`（Vite）只轉譯不檢型別、`pnpm test`（vitest）在 node 環境
  下完全不 render 元件。一個拼錯的 Mantine prop 是合法 JS／合法 JSX，會被靜默
  spread 進 `...others`，三個閘門依然全綠而功能是零（P4 的
  `<Collapse in={…}>` 就是這樣讓整個 AI 總結面板從未打開過）。改動 Mantine 元件的
  props 時，唯一可靠的驗證是**對照安裝版原始碼**——
  `node_modules/@mantine/core/lib/components/<Name>/<Name>.d.ts` 的介面宣告，或
  `esm/.../<Name>.mjs` 的解構，style props 則見
  `lib/core/Box/style-props/style-props.types.d.ts`。憑記憶或憑線上文件都不算。
