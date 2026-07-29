# Changelog

本專案的公開版本變更會記錄在此；版本號遵循 Semantic Versioning。

## [Unreleased]

## [0.4.0] - 2026-07-29

### 需要一次性遷移(破壞性變更)

- 已安裝的工具套件版面從「一個資料夾就是這個工具」改成「不可變版本 + 一個指標決定哪一版生效」。**升級後首次啟動前**，請先執行一次性離線遷移:

  ```bash
  python -m afterthread.migrate_tools_v5 --dry-run   # 唯讀預檢,先看它打算做什麼
  python -m afterthread.migrate_tools_v5             # 實際執行
  ```

  遷移是 all-or-nothing 的:任何一包無法安全處理就整批不動並回報。舊版內容不會被刪除,會保留在隱藏的隔離目錄裡。套件層的 `.env` 逐位元組與權限模式原樣保留。

### 新增

- 工具安裝完成後,AI 會自動產生一份總結,說明它做了什麼與工具原理;可在工具頁提出意見請 AI 修訂,每次修訂產生新的一版,舊版保留。
- 新增「丟掉這一版」:退回前一版並移除被丟棄的版本。前一版的資訊來自該版本自己記錄的來源檔,不是靠資料夾名稱排序。
- AI 日誌現在逐次記錄該次請求實際廣告給模型的工具清單(`tools_advertised`)。工具清單走 API 參數而非 messages,先前在日誌裡完全看不到,無法回答「模型當時知道有哪些工具」。

### 變更

- 工具執行與破壞性操作(刪除、丟棄版本、清理)改為互斥:AI 請求持有共享鎖,工具子行程繼承它,破壞性操作需取得獨佔鎖,取不到時回報「AI 工作進行中」而不動任何檔案。鎖由子行程持有,後端行程即使被強制結束,只要工具還在跑就仍然擋得住。
- 快速捕捉的「追問」與詳情頁的「仍待補齊」不再使用看起來可勾、實際不能勾的 Checkbox,改為誠實的非互動清單。
- 移除工具總結的「定版」:總結現在屬於各自的版本,不再需要凍結;原本用來中止進行中修訂的用途由「丟掉這一版」取代。「重新產生總結」保留。

### 修正

- `scripts/build-wheel.sh` 先前以 `pnpm --dir` 呼叫,導致 pnpm 無法切換到 `packageManager` 釘住的版本,只要本機 pnpm 版本漂移就完全建置不出 wheel。

## [0.3.0] - 2026-07-23

- 新增 `afterthread init-env` 指令:把套件內建的 `env.example` 範本寫到資料目錄成 `.env`(已存在時拒絕覆寫,`--force` 可強制);範本自本版起隨 wheel 打包(原 `backend/.env.example` 移入套件成 `afterthread/env.example`)。
- CLI 內部改為 callback+子指令結構:裸 `afterthread` 與 `--host`/`--port`/`--data-dir`/`--version` 行為皆不變;頂層選項誤放在子指令之前會直接報錯並提示正確位置,而不是被靜默忽略。

## [0.2.0] - 2026-07-22

- HTTP client 遷移至 httpx2(Pydantic 接手維護的 httpx 後繼);openai SDK 邊界因其自身依賴 httpx<1,維持使用 httpx。
- 新增 `TLS_NO_VERIFY` 環境變數,預設關閉。開啟後停用後端對外連線(OpenAPI 文件抓取、LLM endpoint)的 TLS 憑證驗證,供內網自簽憑證部署使用;啟動時會記錄一則警告,並以 advisory 方式透過環境變數傳入已安裝工具的子行程。
- OpenAPI 文件抓取的 client 建構步驟,現在也納入既有的總逾時範圍,並改到有界的單一背景執行緒執行,不再有機會卡住 event loop。

## [0.1.0] - 2026-07-22

- 首次公開版本。
- 提供內嵌 React SPA 的 FastAPI／SQLite 本機網頁應用與 `afterthread` 指令。
- 支援手動捕捉、補齊、更新、篩選與回顧記憶項目。
- 支援 OpenAI-compatible 的 AI 捕捉、補齊、進度更新與 KB 工具安裝器。
- 保留獨立的 Markdown file-based CLI／OpenCode skill 補充工作流。
