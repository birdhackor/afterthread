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
pnpm test          # vitest run（純函式測試用 node；元件測試逐檔使用 jsdom）
pnpm typecheck     # 僅檢查已撰寫的 .ts；既有 .js/.jsx 暫不做語意型別檢查
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

## 元件測試

元件測試使用 jsdom、React Testing Library 與
`src/test/render.tsx` 的 `renderWithAppProviders`。測試檔使用
`.test.tsx`，並在第一行加上：

```tsx
// @vitest-environment jsdom
```

Vitest 的全域預設仍明確設為 `node`；只有帶上述 pragma 的檔案才建立 DOM，既有純函式
測試不會載入 jsdom。測試從 `@testing-library/react` 使用 `screen`／`waitFor`，
互動則用 `@testing-library/user-event` 的 `userEvent.setup()`，不要直接呼叫 DOM
元素的 `.click()`。

`renderWithAppProviders` 依 `src/main.jsx` 的實際組裝提供
`MantineProvider`、`Notifications`、每次 render 獨立的
`QueryClientProvider`，以及使用 memory history 的 `RouterProvider`。測試 helper
刻意不加 `StrictMode`，避免每個 smoke／行為規格都被開發期 double mount 混淆；也
不加 jotai `Provider`，因為正式 app 明確使用 jotai default store。helper
另補 jsdom 本身沒有、但 Mantine mount 時會讀取的 `matchMedia` 與
`document.fonts`；兩者只提供事件介面的 no-op，不模擬 layout 或字型載入結果。
API 一律 mock `src/api/client.ts`，mock response 則以產生的 schema 型別檢查，例如：

```tsx
import type { ApiSuccessResponse } from "../api/client.js";

type ToolListResponse = ApiSuccessResponse<"/api/tools", "get">;
const response = {
	tools: [],
} satisfies ToolListResponse;
```

## API schema 型別

`src/api/schema.gen.ts` 是從後端 FastAPI/Pydantic 的真實 OpenAPI schema 產生並
提交的 API contract；瀏覽器 build 只讀這個 TypeScript 檔，不需要 Python，也不會
啟動後端。後端的 request／response schema 有任何異動後，請在 repo 已安裝
`uv` 與 `pnpm` 依賴的環境執行：

```bash
pnpm generate:api-types
pnpm typecheck
```

產生器位於 repo 共用的 `scripts/generate-api-types.sh`，會直接 import FastAPI
`app`、呼叫 `app.openapi()` 寫入暫存 JSON，再由 `openapi-typescript` 更新已提交的
型別；它不會啟動 server 或 curl `/openapi.json`。CI 的 full-stack job 另執行
`pnpm --dir frontend check:api-types`，以同一路徑重產並在 committed output 有任何
diff 時失敗。

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
| `/tools` | `ToolsPage` | 「已安裝工具」（清單／啟停／刪除，每列可展開讀取／重新產生 AI 總結，並可提意見送出 AI 修訂）與「安裝新工具」（貼 OpenAPI JSON 網址 + 指示，AI 背景建置、輪詢進度）兩個分頁；細節見根目錄 README「KB 工具安裝指南」。 |
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
  `api/client.ts` 的 `messageFor`。
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
  Transition Group 的 `in`——寫錯只會被靜默吞進 `...others`，見下面「jsdom
  元件測試」那條），總結內容以 `enabled: expanded` 延遲讀取（比照
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
  進前者的檔名只會誤導之後的讀者。這些純邏輯仍留在 node 環境的快速單元測試；
  需要驗證元件組裝與互動時，另以逐檔 jsdom 測試搭配共用 render helper。新邏輯一律
  先問「這算安裝，還是總結」再決定放哪個檔案。**唯一的例外寫在
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
- **jsdom 元件測試補上渲染行為閘門**：`pnpm lint`（Biome）不做
  型別檢查、`pnpm build`（Vite）只轉譯不檢型別；一個拼錯的 Mantine prop 仍是合法
  JS／合法 JSX，會被靜默 spread 進 `...others`（P4 的
  `<Collapse in={…}>` 就是這樣讓整個 AI 總結面板從未打開過）。現在可用逐檔 jsdom
  元件測試斷言實際展開／互動結果；改動 Mantine 元件 props 時仍要先**對照安裝版原始碼**——
  `node_modules/@mantine/core/lib/components/<Name>/<Name>.d.ts` 的介面宣告，或
  `esm/.../<Name>.mjs` 的解構，style props 則見
  `lib/core/Box/style-props/style-props.types.d.ts`。憑記憶或憑線上文件都不算。
