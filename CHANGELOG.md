# Changelog

本專案的公開版本變更會記錄在此；版本號遵循 Semantic Versioning。

## [Unreleased]

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
