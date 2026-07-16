# context-memory

`context-memory` 是一個私人使用的工具，核心目的是抵抗 **architectural
knowledge vaporization**：討論當下腦中的理解、取捨、風險、下一步都很清楚，但
事後往往只剩關鍵字，脈絡全部蒸發。

## 這是什麼

方法論是同一套「快速捕捉 → 全面補充 → 更新／回顧」（完整規則見
[docs/methodology.md](docs/methodology.md)），把還在腦中的脈絡轉成之後能被
恢復的記憶項目（memory item：一個決策、一個調查中的主題、一件擱置的任務……）。

**主要介面是本機網頁應用**：FastAPI + SQLite 後端、React/Mantine 前端，打包成
內嵌前端 build 的單一 wheel，一個 `context-memory` 指令就能啟動，資料留在本機
SQLite；接上一顆 OpenAI-compatible 的 LLM 之後，還能用 AI 快速捕捉、AI 補齊、
AI 進度更新，以及讓 AI 自己建立可呼叫外部 API 的工具。**沒有聊天視窗**——AI
功能一律是針對「一則記憶項目」的結構化操作，不是對話介面。

repo 裡同時保留一套更早的檔案版介面：OpenCode skill（`.opencode/`）+
`scripts/context_memory.py`，資料存成 `memory/**/*.md`。這套介面依然可用，
適合不想開網頁、只想在終端機或 agent 對話裡做快速記錄的情境；細節見下方
「CLI 與 OpenCode skill 補充」。**兩套介面資料互不相通**——網頁版的項目存在
SQLite 資料庫，CLI/skill 操作的是獨立的 Markdown 檔案，選一種或兩種一起用
都可以。

### 專案結構（節錄）

```text
backend/context_memory/   FastAPI 應用（routers/services/models/schemas）；細節見 backend/README.md
backend/tests/            pytest 測試
backend/.env.example      環境變數範本（複製到 data dir 或 backend/.env）
frontend/src/             React + Mantine SPA（pages/components/atoms/api）；細節見 frontend/README.md
e2e/smoke.sh              dev 模式全端煙霧測試（見 e2e/README.md）
e2e/wheel_smoke.sh        打包（uvx）模式端對端煙霧測試（見 e2e/README.md）
scripts/build-wheel.sh    建置內嵌前端 build 的可攜 wheel
scripts/context_memory.py 檔案版 CLI（見下方「CLI 與 OpenCode skill 補充」）
.opencode/skills/context-memory/SKILL.md   OpenCode skill
memory/YYYY/MM/*.md       檔案版記憶項目（CLI／skill 專用，與網頁版資料庫分開）
docs/methodology.md       方法論
```

## 安裝與啟動

`context-memory` 目前只以原始碼／wheel 檔的形式存在——這是私人 repo，沒有發佈
到 PyPI，所以啟動前要先在這個 checkout 裡建置一次 wheel（已內嵌前端
production build）：

```bash
bash scripts/build-wheel.sh
```

前置需求：`pnpm`（前端 build）與 `uv`（後端 build／`uvx`）。完成後會在
`backend/dist/` 產生 `context_memory-*.whl`（與對應的 sdist）。

### 直接執行（不安裝，`uvx`）

```bash
uvx --from backend/dist/context_memory-*.whl context-memory
```

`uvx` 會在自己管理的暫存虛擬環境裡解析依賴並啟動服務（同一環境第一次執行需要
網路下載依賴，之後會用快取，離線也能跑）。終端機只會印一行 `Context Memory:
data dir=... database=...`（見下方「首次設定」，絕不印出 API key 等機密），
接著開瀏覽器連 <http://127.0.0.1:8000> 就是完整介面。

### 安裝成常駐指令（`uv tool install` / pipx）

不想每次都打一長串 `uvx --from ...` 的話，可以裝成常駐指令，之後直接打
`context-memory`：

```bash
uv tool install --from backend/dist/context_memory-*.whl context-memory
# 或用 pipx：
pipx install backend/dist/context_memory-*.whl
```

### 常用旗標

`context-memory --help` 看完整說明；旗標與對應環境變數（見
`backend/context_memory/cli.py`）：

| 旗標 | 預設 | 環境變數 |
| --- | --- | --- |
| `--host` | `127.0.0.1`（只綁 loopback——這是單人本機工具，不是要曝露在區網上的服務） | `CONTEXT_MEMORY_HOST` |
| `--port` | `8000` | `CONTEXT_MEMORY_PORT` |
| `--data-dir` | 見下方「首次設定」 | `CONTEXT_MEMORY_DATA_DIR` |
| `--version` | 印版本後結束 | — |

### 升級

版號目前固定在 `0.1.0`（還沒決定對外發佈節奏），所以「升級」就是拉新程式碼後
重新建置、重新執行：

```bash
git pull
bash scripts/build-wheel.sh
uvx --refresh --from backend/dist/context_memory-*.whl context-memory
```

`uvx` 對本機 wheel 檔會快取解析結果，一般重新 build 後直接重跑就會抓到新
內容；不放心的話加 `--refresh` 強制重新解析、忽略快取。已用 `uv tool
install` 裝成常駐指令的話，改用 `--reinstall`（`--force` 是另一件事：那是用
來覆蓋「非 uv 安裝」的同名執行檔，不是這裡要的重裝）：

```bash
uv tool install --reinstall --from backend/dist/context_memory-*.whl context-memory
```

## 首次設定

- **資料目錄（data dir）**：預設是 `$XDG_DATA_HOME/context-memory`，未設定
  `XDG_DATA_HOME` 時退回 `~/.local/share/context-memory`。可用 `--data-dir
  <path>` 或環境變數 `CONTEXT_MEMORY_DATA_DIR` 覆寫。第一次啟動時，若目錄不
  存在會自動建立（權限設為僅該使用者可讀寫）；已存在的目錄則不會被動權限。
  SQLite 資料庫檔（預設 `context_memory.db`）與工具目錄（`tools/`，見下方
  「KB 工具安裝指南」）都會落在這裡。
- **設定檔（`.env`）**：把 `backend/.env.example` 複製一份到
  `<data-dir>/.env`（例如 `~/.local/share/context-memory/.env`），依需要
  填入下方變數。啟動時只有這個目錄下的 `.env` 會被讀取，不會去讀 checkout
  裡的 `backend/.env`（那是 dev 模式專用，見下方「開發模式」）。
- **啟用 AI 功能（必填）**：`OPENAI_BASE_URL` 與 `OPENAI_MODEL` 兩者都要填，
  且 base URL 需能解析為合法的 http/https 網址，才算「已設定」；
  `OPENAI_API_KEY` 是否需要則視該 endpoint 而定（不是判斷「已設定」的條件之
  一）。相容任何 OpenAI-compatible 的 chat completions endpoint。兩者留空
  時，AI 快速捕捉／AI 補齊／AI 進度更新／工具安裝都會顯示「尚未設定」，但
  手動新增、編輯、刪除、篩選、回顧完全不受影響。
- **GLM5.2（或其他 1M-token context 模型）建議值**：後端目前的預設值本身就
  已經是為大 context 模型調校過的（`OPENAI_TIMEOUT_SECONDS` 預設 120 秒、
  `LLM_PROMPT_BUDGET_CHARS` 預設 200000，上限到 2,000,000）；如果內部 LLM
  是 GLM5.2 這類 1M-token context 的模型，`.env.example` 內建了進一步調高的
  建議（取代上方預設）：

  ```bash
  LLM_PROMPT_BUDGET_CHARS=800000   # 讓超大項目也能整項進 prompt 不截斷
  OPENAI_TIMEOUT_SECONDS=300       # 長 context 生成較慢，放寬逾時避免誤判 502
  ```

  完整變數清單（含工具呼叫、AI 互動紀錄等其餘設定）見 `backend/README.md`
  「環境變數」一節，或直接看 `backend/.env.example` 的註解。

## 網頁功能導覽

啟動後，左側導覽列共 6 個項目：

| 路徑 | 頁面 | 功能 |
| --- | --- | --- |
| `/`（總覽） | 總覽 | 呼叫 `GET /api/review`，把非終態項目分成「待補齊／進行中／等待中／擱置」四組，顯示陳舊項目提示，以及後端連線／AI 設定狀態。 |
| `/capture` | 快速捕捉 | 貼上一段原始討論文字，AI 整理成結構化項目並列出追問問題；LLM 未設定時停用送出，可改連到手動新增。 |
| `/items`（記憶清單） | 記憶清單 | 狀態／階段／標籤／關鍵字篩選 + 分頁。 |
| `/items/new` | 新增項目 | 手動建立項目的表單頁。 |
| `/items/$itemId` | 項目詳情 | 完整欄位內容、進度時間軸（可追加一筆進度）、狀態/階段快速修改、「AI 補齊」與「AI 進度更新」兩個 AI 協助操作、刪除。 |
| `/items/$itemId/edit` | 編輯項目 | 手動編輯項目的表單頁。 |
| `/tools` | 工具 | 已安裝工具的清單（啟用/停用/刪除）+「安裝新工具」的 KB 網頁安裝器（見下一節）。 |
| `/llm-logs` | AI 日誌 | 每一次 AI 互動（快速捕捉／AI 補齊／AI 進度更新／工具安裝建置）的完整提示與回應紀錄，可展開查看每次嘗試，供除錯用。 |

項目詳情頁上的兩個 AI 動作：

- **AI 補齊**：提供更多背景文字，AI 依 checklist 補齊各區段內容，回報仍缺少
  的資訊；checklist 完整時會把階段推進為「完整」，並把仍在「快速捕捉」狀態的
  項目升級為「進行中」。`decisions`／`rationale`／`alternatives`／
  `consequences` 這類有歷史意義的欄位不會被直接覆寫——舊內容會接在新內容
  後面，掛上 `--- (superseded YYYY-MM-DD) ---` 這樣的標記，供之後回顧。
- **AI 進度更新**：描述最新進展，AI 整理後記錄成一筆進度，並局部更新項目
  內容。

兩者都遵守樂觀鎖定：如果項目在 AI 處理期間被別的請求改動，會回報衝突並提供
「重新整理」動作，不會悄悄覆蓋掉別人剛寫入的內容。

## KB 工具安裝指南

「工具」頁的「安裝新工具」分頁，是一個像 Claude Code 建 skill 那樣的體驗：
貼上一個 API 的 OpenAPI JSON 網址 + 一段指示，讓 AI 自己讀文件、寫程式、
測試、裝進工具目錄——裝好之後，AI 快速捕捉／AI 補齊／AI 進度更新都能呼叫這個
新工具。

### 操作步驟

1. **OpenAPI JSON 網址**：填目標 API 的 OpenAPI/Swagger JSON 文件網址（必須
   是 `http://` 或 `https://`，且可公開下載）。文件上限 2MB，抓取總時間上限
   60 秒，超過會直接判定失敗（不會截斷後硬塞給 AI）。
2. **給 AI 的指示**：描述要建立什麼工具——要查什麼資料、要用 OpenAPI 文件裡
   的哪個 endpoint、認證方式，以及該 API 自己的 key/token（如果需要）。例
   如：「建立一個用關鍵字搜尋內部知識庫的工具，使用 `/search` 端點；API key
   是 `xxxx`，請放在 `X-Api-Key` 標頭。」AI 會把金鑰寫進**這個工具自己的**
   `.env`（不是後端的 `.env`），執行時才注入該工具的子行程環境。
3. **送出**：後端立刻回一個安裝工作，開始背景建置（通常需要數分鐘：AI 要
   寫檔、用 `run_shell` 實際呼叫該 API 測試、視結果修正，反覆幾輪直到可用或
   放棄）。頁面每 2 秒輪詢一次進度，同一時間**只能有一個安裝在跑**——已有
   安裝進行中時再次送出，會顯示「已有安裝正在進行中，請等待其完成」。
4. **結果**：「安裝完成」或「安裝失敗」。**一旦 AI 建置階段真的開始**，結果會
   附上 AI 自己的文字摘要，以及一個「查看 AI 日誌」連結——跳到 AI 日誌頁並
   展開這次建置工作的互動紀錄（提示、AI 回覆、每一次寫檔/讀檔/`run_shell`
   呼叫的摘要與其輸出，內容受 `LLM_LOG_BODY_MAX_CHARS` 上限截斷）。**AI 開始
   建置後失敗時，這個連結是最主要的除錯入口**：AI 判定「尚未完成」的原因、
   `run_shell` 測試時的錯誤輸出都在裡面。若是更早期就失敗（連 OpenAPI 文件
   都下載不到、或建不出暫存目錄），則還沒有 AI 摘要、也沒有可展開的紀錄，連結
   只會帶你到一般的 AI 日誌頁。

### instructions prompt 撰寫要領

- 講清楚**目的**（要用來查什麼、給哪個工作流程用），AI 會自己從 OpenAPI
  文件裡挑合適的 endpoint，不需要你先讀文件、指定確切的 schema。
- 認證方式要講清楚：標頭名稱、query 參數、或 bearer token，並直接附上真正的
  key/token（見下方「風險與信任」——這段文字本身會被完整記錄）。
- 想要工具回什麼樣的結果（精簡文字？結構化 JSON？）可以順帶說一句，AI 會
  據此決定怎麼整理 API 回應。
- 一次只描述一個工具的需求；需要多個能力就分次安裝（同一時間本來就只能跑
  一個安裝工作）。

### `.staging` 殘留清理

建置過程中，AI 是在 `<data-dir>/tools/.staging/<隨機字串>/` 這個暫存目錄裡
寫檔測試，成功後才整個搬進 `<data-dir>/tools/<工具名稱>/`；正常結束（無論
成功或失敗）都會自動清掉暫存目錄。但如果後端在建置途中被中斷（當掉、被強制
關閉、主機重開機），暫存目錄可能會殘留。這個目錄名稱以 `.` 開頭，工具清單的
掃描邏輯會直接忽略它，不會被列成「無效工具」，也不影響任何功能，只是佔一點
磁碟空間；可以直接手動刪除 `<data-dir>/tools/.staging/` 底下的殘留目錄。

### 風險與信任

- **沒有容器隔離**：AI 用來建置工具的 `run_shell` 是「以本服務自己的權限」
  執行的真實 bash，只是預設從暫存目錄開始（工作慣例，不是圍籬）——它能讀寫
  這個服務的使用者帳號能碰到的任何東西。真正被強制圍住的**只有**檔案類
  meta-tool（`write_file`／`read_file`／`list_dir`）：這三個的路徑一定會被
  限制在暫存目錄之內。
- **因此**：只安裝你信任的 OpenAPI 文件來源與指示內容，就像你只會執行信任
  的安裝腳本一樣——這是本機單人工具的設計取捨（比照 Claude Code 建 skill 的
  能力/風險模型），不是尚未做完的安全機制。
- **貼進去的金鑰會被記錄**：安裝指示文字（作為提示原文）、以及 AI 工具呼叫
  參數的摘要／預覽（每次呼叫的參數會被截到約前 200 字元寫進日誌），都會進這次
  建置工作的 AI 日誌。也就是說，你貼給 AI 的第三方 API key 不只會進到工具自己
  的 `.env`——安裝指示裡的金鑰會原樣進日誌，而出現在工具呼叫參數裡的金鑰通常
  也短於預覽長度、因而被記到。日誌預設只存在後端記憶體、隨程序重啟清空（每則
  內容另受 `LLM_LOG_BODY_MAX_CHARS` 截斷），但如果設定了 `LLM_LOG_FILE`，就會
  連同其他 AI 互動一起落地到那個檔案。不要因為「金鑰只會寫進工具的 .env」而
  誤以為它不會出現在別處。

## 開發模式

後端（從 `backend/` 目錄）：

```bash
cd backend && uv sync
uv run uvicorn context_memory.main:app --reload --port 8000
```

資料庫（SQLite）不存在時會在啟動時自動建立（預設 `backend/context_memory.db`）；
環境變數複製 `backend/.env.example` 為 `backend/.env` 後依需要填入——dev
模式讀的是這個 checkout 裡的 `backend/.env`，跟打包模式讀
`<data-dir>/.env` 是兩回事（見上方「首次設定」）。

前端（從 `frontend/` 目錄）：

```bash
cd frontend && pnpm install && pnpm dev
```

開發伺服器預設在 <http://localhost:5173>，`/api` 會被代理到後端的 8000 埠。

打包成內嵌前端 build 的可攜 wheel（`uvx`/`uv tool install` 用的就是這個）：見
`scripts/build-wheel.sh`。完整技術細節（環境變數總覽、API 概覽、設計筆記、
打包/測試指令）見 `backend/README.md`；前端頁面總覽與開發慣例見
`frontend/README.md`；e2e 煙霧測試涵蓋範圍（dev 模式的 `e2e/smoke.sh` 與
打包模式的 `e2e/wheel_smoke.sh`）見 `e2e/README.md`。

## CLI 與 OpenCode skill 補充

網頁版是主要介面；下面這套檔案版工具鏈仍然可用，適合不開網頁、只在終端機或
agent 對話裡工作的情境。資料存成 `memory/**/*.md`，與網頁版的 SQLite 資料庫
**完全分開，兩邊互不同步**。

在 repo 根目錄啟動 OpenCode 就能使用 `.opencode/skills/context-memory/`
這個 skill（自動被 OpenCode 發現）：

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

也可以直接對 OpenCode 說「Use the context-memory skill to capture this
discussion.」。

不用 OpenCode 時，`scripts/context_memory.py` 提供同一套操作的最小 CLI：

```bash
python3 scripts/context_memory.py new --title "Payment retry strategy" --summary "Discussed retry/backoff options with Alice."
python3 scripts/context_memory.py validate   # 驗證條目格式
python3 scripts/context_memory.py index      # 重新產生 memory/INDEX.md
python3 scripts/context_memory.py list       # 列出條目
```

## 方法論

快速捕捉／全面補充的完整規則、狀態值定義、confidence marker（Known／
Inferred／Unknown）等方法論細節，見 [docs/methodology.md](docs/methodology.md)。
