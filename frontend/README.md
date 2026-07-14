# Context Memory Frontend

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
```

以上指令皆已在本機實際執行過並確認通過：`pnpm install`、`pnpm lint`（30 個檔案，
無錯誤）、`pnpm build`（成功，僅有 chunk size 提示，非錯誤）；`pnpm dev` 啟動後
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
| `/` | `HomePage` | 總覽／回顧儀表板：呼叫 `GET /api/review`，把非終態項目分成待補齊／進行中／等待中／擱置四組。 |
| `/capture` | `CapturePage` | 快速捕捉：貼上一段原始討論文字，呼叫 `POST /api/capture`，顯示 AI 產出的項目與追問問題；LLM 未設定時顯示提示、AI 動作停用。 |
| `/items` | `ItemsListPage` | 項目列表：狀態／stage／tag／關鍵字篩選 + 分頁（篩選與分頁狀態存在 `atoms/filters.js`）。 |
| `/items/new` | `ItemNewPage` | 手動新增項目的表單頁（沿用 `ItemForm` 元件）。 |
| `/items/$itemId` | `ItemDetailPage` | 單筆項目詳情：完整欄位、progress 歷史、狀態/階段快速修改、AI 補齊（enrich）／AI 協助更新（assist-update）操作。 |
| `/items/$itemId/edit` | `ItemEditPage` | 手動編輯項目的表單頁（沿用 `ItemForm` 元件）。 |
| （其他） | `NotFoundPage` | 404 fallback。 |

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
- **Mutation gates**：`ItemDetailPage` 用單一共用旗標
  （`mutationPending` / `mutationGate`，`@mantine/hooks` 的 `useDisclosure`）
  同一時間只允許一個會改動該項目的操作進行中（狀態/階段快速修改、追加 progress
  note、AI 補齊、AI 協助更新皆共用這個 gate）；任一操作進行中時，其餘會修改此項目
  的控制項全部停用，避免兩個併發的 mutation 互相覆蓋對方剛寫入的結果。
