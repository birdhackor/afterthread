# afterthread

**Keep the thread after the conversation ends.**

> 本專案原名 context-memory，已於 2026-07 更名為 afterthread。

`afterthread` 是一個私人使用的工具，核心目的是抵抗 **architectural
knowledge vaporization**：討論當下腦中的理解、取捨、風險、下一步都很清楚，但
事後往往只剩關鍵字，脈絡全部蒸發。

## 這是什麼

方法論是同一套「快速捕捉 → 全面補充 → 更新／回顧」（完整規則見
[docs/methodology.md](docs/methodology.md)），把還在腦中的脈絡轉成之後能被
恢復的記憶項目（memory item：一個決策、一個調查中的主題、一件擱置的任務……）。

**主要介面是本機網頁應用**：FastAPI + SQLite 後端、React/Mantine 前端，打包成
內嵌前端 build 的單一 wheel，一個 `afterthread` 指令就能啟動，資料儲存在
本機 SQLite（無帳號、無自有雲端）；接上一顆 OpenAI-compatible 的 LLM 之後，
還能用 AI 快速捕捉、AI 補齊、AI 進度更新，以及讓 AI 自己建立可呼叫外部 API
的工具——**使用 AI 功能時，被處理的內容會送往你設定的那個 LLM endpoint**，
若該 endpoint 在遠端，內容就會離開本機（「留在本機」精確講是指 SQLite 儲存
層）。**沒有聊天視窗**——AI 功能一律是針對「一則記憶項目」的結構化操作，不是
對話介面。

repo 裡同時保留一套更早的檔案版介面：OpenCode skill（`.opencode/`）+
`scripts/afterthread.py`，資料存成 `memory/**/*.md`。這套介面依然可用，
適合不想開網頁、只想在終端機或 agent 對話裡做快速記錄的情境；細節見下方
「CLI 與 OpenCode skill 補充」。**兩套介面資料互不相通**——網頁版的項目存在
SQLite 資料庫，CLI/skill 操作的是獨立的 Markdown 檔案，選一種或兩種一起用
都可以。

### 專案結構（節錄）

```text
backend/afterthread/      FastAPI 應用（routers/services/models/schemas）；細節見 backend/README.md
backend/tests/            pytest 測試
backend/afterthread/env.example   環境變數範本（打包模式：afterthread init-env；開發複製為 backend/.env）
frontend/src/             React + Mantine SPA（pages/components/atoms/api）；細節見 frontend/README.md
e2e/smoke.sh              dev 模式全端煙霧測試（見 e2e/README.md）
e2e/wheel_smoke.sh        打包（uvx）模式端對端煙霧測試（見 e2e/README.md）
scripts/build-wheel.sh    建置內嵌前端 build 的可攜 wheel
scripts/afterthread.py    檔案版 CLI（見下方「CLI 與 OpenCode skill 補充」）
.opencode/skills/afterthread/SKILL.md   OpenCode skill
memory/YYYY/MM/*.md       檔案版記憶項目（CLI／skill 專用，與網頁版資料庫分開）
docs/methodology.md       方法論
docs/releasing.md         PyPI 發布與 Trusted Publishing 維運手冊
```

## 安裝與啟動

需求：Python `>=3.14`，支援 Linux 與 macOS（Windows 尚未列入支援範圍）。
wheel 已內嵌 frontend production build，不需要另外安裝 Node.js 或 pnpm。

### 直接執行（不常駐安裝）

```bash
uvx afterthread
```

`uvx` 會從 PyPI 取得套件，在自己管理的暫存虛擬環境裡解析依賴並啟動服務
（同一環境第一次執行需要網路下載依賴，之後會用快取）。終端機只會印一行 `afterthread:
data dir=... database=...`（見下方「首次設定」，絕不印出 API key 等機密），
接著開瀏覽器連 <http://127.0.0.1:8000> 就是完整介面。

### 安裝成常駐指令（`uv tool install` / pipx）

不想每次都打一長串 `uvx --from ...` 的話，可以裝成常駐指令，之後直接打
`afterthread`：

```bash
uv tool install afterthread
# 或用 pipx：
pipx install afterthread
```

### 常用旗標

`afterthread --help` 看完整說明；旗標與對應環境變數（見
`backend/afterthread/cli.py`）：

| 旗標 | 預設 | 環境變數 |
| --- | --- | --- |
| `--host` | `127.0.0.1`（只綁 loopback——這是單人本機工具，不是要曝露在區網上的服務） | `AFTERTHREAD_HOST` |
| `--port` | `8000` | `AFTERTHREAD_PORT` |
| `--data-dir` | 見下方「首次設定」 | `AFTERTHREAD_DATA_DIR` |
| `--version` | 印版本後結束 | — |

### 升級

`uvx` 使用者可重新整理解析結果；常駐安裝則用對應工具的 upgrade 指令：

```bash
uvx --refresh afterthread
uv tool upgrade afterthread
# 或
pipx upgrade afterthread
```

### 從原始碼建置

開發者若要驗證尚未發布的 checkout，可從 repo 根目錄建置本機 wheel。前置需求是
`pnpm` 與 `uv`：

```bash
bash scripts/build-wheel.sh
uvx --from backend/dist/afterthread-*.whl afterthread
```

腳本會先移除 `backend/dist/` 內既有的 wheel／sdist，再產生且驗證唯一一組
`afterthread-<version>.whl` 與 `.tar.gz`。

## 首次設定

- **資料目錄（data dir）**：預設是 `$XDG_DATA_HOME/afterthread`，未設定
  `XDG_DATA_HOME` 時退回 `~/.local/share/afterthread`。可用 `--data-dir
  <path>` 或環境變數 `AFTERTHREAD_DATA_DIR` 覆寫。第一次啟動時，若目錄不
  存在會自動建立（權限設為僅該使用者可讀寫）；已存在的目錄則不會被動權限。
  SQLite 資料庫檔（預設 `afterthread.db`）與工具目錄（`tools/`，見下方
  「KB 工具安裝指南」）都會落在這裡。
- **設定檔（`.env`）**：在 `<data-dir>/.env`（例如
  `~/.local/share/afterthread/.env`）建立設定檔；打包安裝後可直接執行
  `afterthread init-env` 產生這個檔案，從原始碼開發時則可複製
  `backend/afterthread/env.example`。打包模式只讀 data dir 內的 `.env`，不會讀
  checkout 裡的 `backend/.env`（那是 dev 模式專用，見下方「開發模式」）。啟用
  AI 的最小設定如下：

  ```dotenv
  OPENAI_BASE_URL=https://your-endpoint.example/v1
  OPENAI_API_KEY=your-key-if-required
  OPENAI_MODEL=your-model
  ```
- **啟用 AI 功能（必填）**：`OPENAI_BASE_URL` 與 `OPENAI_MODEL` 兩者都要填，
  且 base URL 需能解析為合法的 http/https 網址，才算「已設定」；
  `OPENAI_API_KEY` 是否需要則視該 endpoint 而定（不是判斷「已設定」的條件之
  一）。相容任何 OpenAI-compatible 的 chat completions endpoint。兩者留空
  時，AI 快速捕捉／AI 補齊／AI 進度更新／工具安裝都會顯示「尚未設定」，但
  手動新增、編輯、刪除、篩選、回顧完全不受影響。
- **GLM5.2（或其他 1M-token context 模型）建議值**：後端目前的預設值本身就
  已經是為大 context 模型調校過的（`OPENAI_TIMEOUT_SECONDS` 預設 120 秒、
  `LLM_PROMPT_BUDGET_TOKENS` 預設 200000，上限到 1,000,000）。prompt 預算以
  **token** 計價（模型真正的限制是 token，不是字元）：系統會從近期每次 LLM
  互動實際回報的 token 數，動態估一個「字元↔token 比值」，把 token 預算換算成
  當下該套用的字元上限，冷啟動（樣本不足）時用保守比值 1.0，行為與舊的純字元
  預算等價（細節見 [docs/tool-calling.md](docs/tool-calling.md)）。如果內部
  LLM 是 GLM5.2 這類 1M-token context 的模型，
  `backend/afterthread/env.example` 內建了進一步調高的建議（取代上方預設）：

  ```bash
  LLM_PROMPT_BUDGET_TOKENS=800000  # 讓超大項目也能整項進 prompt 不截斷
  OPENAI_TIMEOUT_SECONDS=300       # 長 context 生成較慢，放寬逾時避免誤判 502
  ```

  完整變數清單（含工具呼叫、AI 互動紀錄等其餘設定）見 `backend/README.md`
  「環境變數」一節，或直接看 `backend/afterthread/env.example` 的註解。

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
| `/tools` | 工具 | 已安裝工具的清單（啟用/停用/刪除；每個工具可展開讀取 AI 總結、重新產生、定版/解除定版、提意見送出 AI 修訂）+「安裝新工具」的 KB 網頁安裝器（見下一節）。 |
| `/llm-logs` | AI 日誌 | 每一次 AI 互動（快速捕捉／AI 補齊／AI 進度更新／工具安裝建置）的提示與回應紀錄，可展開查看每次嘗試，供除錯用（每則內容受 `LLM_LOG_BODY_MAX_CHARS` 截斷、超量的較早訊息會被省略、工具呼叫參數為約 200 字元預覽）。 |

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

想了解 AI 呼叫工具背後完整的運作機制（生命週期、安全邊界、為什麼不用
LangChain），見 [docs/tool-calling.md](docs/tool-calling.md)。

### 操作步驟

1. **OpenAPI JSON 網址**：填目標 API 的 OpenAPI/Swagger JSON 文件網址（必須
   是 `http://` 或 `https://`，且可公開下載）。文件上限 2MB，抓取總時間上限
   60 秒，超過會直接判定失敗（不會截斷後硬塞給 AI）。
2. **給 AI 的指示**：描述要建立什麼工具——要查什麼資料、要用 OpenAPI 文件裡
   的哪個 endpoint，以及認證方式**的形狀**（標頭名稱、query 參數，還是 bearer
   token）。例如：「建立一個用關鍵字搜尋內部知識庫的工具，使用 `/search` 端點；
   把 `KB_API_KEY` 的值放進 `X-Api-Key` 標頭。」**金鑰本身不要寫在這裡**，填到
   下一步的秘密欄位——這段文字會原封不動送給模型、也會原封不動進 AI 日誌（畫面
   上這個欄位的說明講的就是這件事），而且會被記進工具側檔的「出處」，之後每次
   AI 修訂都會再讀一次。
3. **秘密名稱／秘密值（選填）**：目標 API 需要金鑰時走這條路。兩欄要嘛都填、
   要嘛都留空；名稱必須是合法的環境變數名（大寫字母開頭，只含大寫字母、數字與
   底線，最長 64 字），值至少 6 個字元（更短的值遮蔽器一律跳過，等於一個永遠遮
   不掉的秘密）且不能有換行。填了之後後端做三件事：
   - 用**你給的名字**把值注入 `run_shell` 子行程的環境變數，所以 AI 能拿真的
     金鑰實測 API 通不通；
   - 整個建置期間把這個值登記成「已知秘密」，AI 日誌在寫下每則提示／回覆之前
     就會把它換成 `•••[秘密已遮蔽]•••`；
   - 驗證通過、要搬進正式目錄的那一刻，由**後端自己**把它寫進**這個工具自己的**
     `.env`（不是後端的 `.env`），之後每次執行才注入該工具的子行程環境。

   值**不會**出現在任何送給模型的提示裡——模型只被告知「有一個叫這個名字的環境
   變數可以用，不准印出來、不准自己寫進檔案」。誠實的邊界：這個值確實在
   `run_shell` 的環境變數裡，而 `run_shell` 正是模型下指令的地方，所以那些禁令是
   **行為指示，不是強制圍籬**（回到同一個信任模型：只安裝你信任的文件與指示）。
4. **送出**：後端立刻回一個安裝工作，開始背景建置（通常需要數分鐘：AI 要
   寫檔、用 `run_shell` 實際呼叫該 API 測試、視結果修正，反覆幾輪直到可用或
   放棄）。頁面每 2 秒輪詢一次進度，同一時間**只能有一個工具任務在跑**——安裝、
   已安裝工具的「AI 修訂」、以及「重新產生總結」三者共用同一個名額，任一個進行
   中時再次送出，會顯示「已有工具任務正在進行中（安裝、AI 修訂或重新產生總結），
   請等待完成後再安裝」。
5. **結果**：「安裝完成」或「安裝失敗」。**一旦 AI 建置階段真的開始**，結果就會
   附上一個「查看 AI 日誌」連結——跳到 AI 日誌頁並展開這次建置工作的互動紀錄
   （提示、AI 回覆、每一次寫檔/讀檔/`run_shell` 呼叫的摘要與其輸出，內容受
   `LLM_LOG_BODY_MAX_CHARS` 上限截斷）。**AI 開始建置後失敗時，這個連結是最
   主要的除錯入口**：AI 判定「尚未完成」的原因、`run_shell` 測試時的錯誤輸出
   都在裡面。若 AI 有回傳一段文字摘要，也會一併顯示在結果上（但 LLM 逾時或
   上游錯誤時可能沒有摘要，這時只有連結能用）。若是更早期就失敗（連 OpenAPI
   文件都下載不到、或建不出暫存目錄），則連 AI 建置都還沒開始、沒有可展開的
   紀錄，連結只會帶你到一般的 AI 日誌頁。

### instructions prompt 撰寫要領

- 講清楚**目的**（要用來查什麼、給哪個工作流程用），AI 會自己從 OpenAPI
  文件裡挑合適的 endpoint，不需要你先讀文件、指定確切的 schema。
- 認證方式要講清楚：標頭名稱、query 參數、或 bearer token，並指名金鑰會以**哪個
  環境變數**提供（就是你在「秘密名稱」填的那個）。**值本身填在秘密欄位，不要貼
  進這段文字**——指示是原封不動送給模型、也原封不動落進 AI 日誌的（見下方
  「風險與信任」）。
- 想要工具回什麼樣的結果（精簡文字？結構化 JSON？）可以順帶說一句，AI 會
  據此決定怎麼整理 API 回應。
- 一次只描述一個工具的需求；需要多個能力就分次安裝（同一時間本來就只能跑
  一個工具任務——安裝、AI 修訂與重新產生總結共用同一個名額）。

### `.staging` 殘留清理

建置過程中，AI 是在 `<data-dir>/tools/.staging/<隨機字串>/` 這個暫存目錄裡
寫檔測試，成功後才整個搬進 `<data-dir>/tools/<工具名稱>/`。清理是 best-effort
（清理時的例外都被吞掉），正常結束時通常會清掉暫存目錄；但如果後端在建置途中
被中斷（當掉、被強制關閉、主機重開機），**或清理本身失敗**（例如 `run_shell`
改動了目錄權限），暫存目錄就可能殘留。這個目錄名稱以 `.` 開頭，工具清單的
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
- **打進指示欄位的東西一律會被記錄**：安裝指示文字（作為提示送給 AI）、以及 AI
  工具呼叫參數的摘要／預覽（每次呼叫的參數會被截到約前 200 字元寫進日誌），都會
  進這次建置工作的 AI 日誌。這些內容一樣受 `LLM_LOG_BODY_MAX_CHARS` 逐則截斷，
  所以很長的指示文字不保證整段留存；但一般長度的 API key／token 遠短於任何合理的
  上限，實務上會被完整記到。日誌預設只存在後端記憶體、隨程序重啟清空，但如果設定
  了 `LLM_LOG_FILE`，就會連同其他 AI 互動一起落地到那個檔案；而指示文字另外還會
  被存進工具側檔的「出處」，每次 AI 修訂都會再讀一次。
- **所以憑證走秘密欄位，不要貼進指示**（上面步驟 3）：那條路徑的值從頭到尾不進
  提示、全程被登記成已知秘密所以在日誌裡是遮蔽的，最後由後端寫進工具自己的
  `.env`。反過來，貼在指示裡的金鑰在**它還不是「已知秘密」的那一刻**就已經被寫
  進日誌了——遮蔽器只認得已登記的值，事後登記救不回已經落地的紀錄。（同理，
  用秘密欄位並不代表值就消失了：它仍然在工具的 `.env` 與 `run_shell` 的環境變數
  裡，只是不再出現在提示與日誌中。）

## 開發模式

後端（從 `backend/` 目錄）：

```bash
cd backend && uv sync
uv run uvicorn afterthread.main:app --reload --port 8000
```

資料庫（SQLite）不存在時會在啟動時自動建立（預設 `backend/afterthread.db`）；
環境變數複製 `backend/afterthread/env.example` 為 `backend/.env` 後依需要填入——dev
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

在 repo 根目錄啟動 OpenCode 就能使用 `.opencode/skills/afterthread/`
這個 skill（自動被 OpenCode 發現）：

```bash
opencode
```

常用指令：

```text
/aft-capture 剛剛跟同事討論了 xxx，關鍵字是 ...
/aft-enrich memory/2026/07/2026-07-09-context-memory-project-vision.md
/aft-update memory/2026/07/2026-07-09-context-memory-project-vision.md 今天決定先做 opencode skill MVP
/aft-review
```

也可以直接對 OpenCode 說「Use the afterthread skill to capture this
discussion.」。

不用 OpenCode 時，`scripts/afterthread.py` 提供同一套操作的最小 CLI：

```bash
python3 scripts/afterthread.py new --title "Payment retry strategy" --summary "Discussed retry/backoff options with Alice."
python3 scripts/afterthread.py validate   # 驗證條目格式
python3 scripts/afterthread.py index      # 重新產生 memory/INDEX.md
python3 scripts/afterthread.py list       # 列出條目
```

## 方法論

快速捕捉／全面補充的完整規則、狀態值定義、confidence marker（Known／
Inferred／Unknown）等方法論細節，見 [docs/methodology.md](docs/methodology.md)。

## 授權

本專案採用 [MIT License](LICENSE)。Copyright (c) 2026 birdhackor。
