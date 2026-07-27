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
  409（`tool_job_in_progress`）錯誤處理接住。每個工具列的 AI 總結面板走 inline
  展開（`@mantine/core` 的 `Collapse` + `useDisclosure`，零新依賴，比照 D38
  選用 Mantine 內建元件的理由；每列獨立展開，不像 `LlmLogsPage` 的 Accordion
  同時間只開一項；展開 prop 是 `Collapse` 自己的 `expanded`，**不是** React
  Transition Group 的 `in`——寫錯只會被靜默吞進 `...others`，見下面「沒有 jsdom」
  那條），總結內容以 `enabled: expanded` 延遲讀取（比照
  `LlmLogsPage.LogDetailPanel`，收合的列從不打 API）。定版／解除定版
  （`PATCH .../summary`）刻意**不**受這個 busy gate 管制：後端這個端點本來就
  沒有查 single-flight，且刻意支援「AI 修訂進行中先定版，換裝前重新檢查會擋下
  取代」這種中途操作，在前端補一個它不需要的鎖只會擋掉後端特地支援的動作。
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
- **`utils/toolInstall.js` 與 `utils/toolSummary.js` 的分工**：前者是安裝表單
  驗證（URL／秘密名稱與值）＋工具任務輪詢共用的純函式
  （`isToolJobActive`／`toolJobRefetchInterval`／`isTerminalToolJobState`——
  D40 之前只服務安裝工作，現在安裝與修訂共用同一張後端 job 表與同一個輪詢
  路由，因此改用不含「install」字樣的名稱，並更新了每個呼叫點與測試）；後者
  是 AI 總結網域的純邏輯（狀態→badge 對映 `summaryStatusMeta`、是否可定版
  `canFinalizeSummary`、快取鍵 `toolSummaryQueryKey`／`toolSummaryKeyPrefix`、
  列徽章的快取更新器 `patchToolRowSummaryStatus`），因為那與「安裝」無關，硬塞
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
