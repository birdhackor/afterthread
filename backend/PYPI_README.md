# afterthread

**Keep the thread after the conversation ends.**

`afterthread` 是一個 local-first 的個人知識延續工具，用來保存討論結束後最容易
蒸發的架構脈絡：決策、理由、替代方案、取捨、風險與下一步。主要介面是一個由
FastAPI、SQLite 與 React 組成的本機網頁應用；wheel 已內嵌完整前端，只需要一個
`afterthread` 指令即可啟動。

> 目前是早期版本，支援 Python 3.14，以及 Linux／macOS。Windows 尚未列入支援
> 範圍。

## 安裝與啟動

不需要常駐安裝，直接執行：

```bash
uvx afterthread
```

或安裝成固定指令：

```bash
uv tool install afterthread
# 或
pipx install afterthread
```

啟動後以瀏覽器開啟 <http://127.0.0.1:8000>。資料預設儲存在
`$XDG_DATA_HOME/afterthread`；未設定 `XDG_DATA_HOME` 時使用
`~/.local/share/afterthread`。可用 `afterthread --data-dir <path>` 覆寫。

## 功能

- 快速捕捉一個決策、調查主題或擱置工作。
- 逐步補齊背景、理由、替代方案、後果、風險與下一步。
- 以狀態、階段、標籤與關鍵字篩選，回顧陳舊或待補齊項目。
- 選配 OpenAI-compatible LLM，提供結構化的 AI 捕捉、補齊與進度更新。
- 讓 AI 依 OpenAPI 文件建立可呼叫外部 API 的本機工具。

本產品沒有聊天介面；AI 功能都是針對單一記憶項目的結構化操作。

## AI 設定與隱私

手動新增、編輯、刪除、篩選與回顧不需要 LLM。若要啟用 AI 功能，請先執行
`afterthread init-env` 在資料目錄建立 `.env` 範本，再編輯以下設定：

```dotenv
OPENAI_BASE_URL=https://your-endpoint.example/v1
OPENAI_API_KEY=your-key-if-required
OPENAI_MODEL=your-model
```

記憶項目儲存在本機 SQLite；但使用 AI 功能時，正在處理的內容會傳送到你設定的
LLM endpoint。若 endpoint 是遠端服務，內容就會離開本機。

KB 工具安裝器能讓 AI 執行真實 bash 指令，而且沒有容器隔離。只應使用你信任的
OpenAPI 文件與安裝指示；完整威脅模型與設定參考請閱讀
[afterthread 文件](https://birdhackor.github.io/afterthread/)。

## 授權

MIT License。Copyright (c) 2026 birdhackor。
