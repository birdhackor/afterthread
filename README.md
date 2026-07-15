# context-memory

`context-memory` 是一個私人 git repo，用來抵抗 **architectural knowledge vaporization**：討論當下理解得很清楚，但隔一段時間只剩關鍵字，真正的脈絡、取捨、風險與下一步都蒸發了。

MVP 的核心不是做完整 PM 系統，而是讓 OpenCode 透過 skill 幫你把「當下還在腦中的 context」快速轉成可恢復的記憶檔。

## 現在可以怎麼用

在 repo 根目錄啟動 OpenCode：

```bash
opencode
```

常用指令：

```text
/cm-capture 剛剛跟同事討論了 xxx，關鍵字是 ...
/cm-enrich memory/2026/07/2026-07-09-context-memory-project-vision.md
/cm-update memory/2026/07/2026-07-09-context-memory-project-vision.md 今天決定先做 opencode skill MVP
/cm-review
```

也可以直接對 OpenCode 說：

```text
Use the context-memory skill to capture this discussion.
```

## 資料結構

```text
.opencode/
  skills/context-memory/SKILL.md   # OpenCode 會自動發現的 skill
  commands/*.md                    # OpenCode slash commands
docs/
  methodology.md                   # 方法論
  research-notes.md                # survey 摘要與來源
memory/
  INDEX.md                         # 記憶條目索引
  YYYY/MM/YYYY-MM-DD-slug.md       # 每個 topic/item 一個檔案
templates/
  memory-item.md                   # 條目範本
scripts/
  context_memory.py                # 建立、索引、驗證記憶條目的小工具
backend/
  context_memory/                   # FastAPI 應用（routers/services/models/schemas），細節見 backend/README.md
  tests/                            # pytest 測試（386+）
  .env.example                      # 環境變數範本（複製為 .env 後填入）
frontend/
  src/                              # React + Mantine SPA（pages/components/atoms/api），細節見 frontend/README.md
e2e/
  smoke.sh                          # 全端煙霧測試（見下方「網頁版」章節與 e2e/README.md）
  mock_llm.py                       # 純標準函式庫的 OpenAI-compatible mock 伺服器
```

## CLI

建立空白 quick capture 條目：

```bash
python3 scripts/context_memory.py new --title "Payment retry strategy" --summary "Discussed retry/backoff options with Alice."
```

更新索引：

```bash
python3 scripts/context_memory.py index
```

驗證條目格式：

```bash
python3 scripts/context_memory.py validate
```

列出條目：

```bash
python3 scripts/context_memory.py list
```

## 設計原則

- 快速建立階段只要求最少問題，先把易蒸發的脈絡留下。
- 完整補充階段才補決策理由、替代方案、限制、風險、未知與恢復線索。
- 每個 record 都應該讓「未來的你」能回答：當時為什麼這樣想？下一步是什麼？什麼情境下需要重看或推翻？
- 用 git 版本化記憶，不急著引入資料庫或服務。

## 網頁版

`backend/` + `frontend/` 是這套 file-based MVP 的 web 化：同一套快速捕捉／全面
補充／回顧方法論，多一個可點擊操作的網頁介面與 HTTP API，**與上面的 file-based
MVP 並存**——彼此互不依賴，也互不取代，你可以只用其中一種，或兩種一起用。

架構一句話：**FastAPI + SQLite 後端，React/Mantine SPA 前端**。

### Quickstart

後端：

```bash
cd backend && uv sync && uv run uvicorn context_memory.main:app --port 8000
```

資料庫（SQLite）不存在時會在啟動時自動建立（預設 `backend/context_memory.db`）；
環境變數請複製 `backend/.env.example` 為 `backend/.env` 後依需要填入。

前端：

```bash
cd frontend && pnpm install && pnpm dev
```

開發伺服器預設在 <http://localhost:5173>，`/api` 會被代理到後端的 8000 埠。

LLM 設定：把 `OPENAI_BASE_URL` 與 `OPENAI_MODEL` 填入 `backend/.env`（相容任何
OpenAI-compatible endpoint）；`OPENAI_API_KEY` 是否需要則視該端點而定。兩者留空
時，AI 功能（快速捕捉／AI 補齊／AI 協助更新）會顯示「尚未設定」，但手動 CRUD
（新增、編輯、刪除、篩選、回顧）完全可用，不受影響。

e2e 煙霧測試（啟動真實後端 + mock LLM + 前端 production build，驗證全端整合）：

```bash
./e2e/smoke.sh
```

更完整的技術細節（環境變數總覽、API 概覽、設計筆記）見 `backend/README.md`；
前端的頁面總覽與開發慣例見 `frontend/README.md`；e2e 測試涵蓋範圍見
`e2e/README.md`。

