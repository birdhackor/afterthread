# 安裝與啟動

`afterthread` 發布在 PyPI。需求是 Python `>=3.14`,支援 Linux 與 macOS(Windows 尚未列入支援範圍)。wheel 已內嵌 frontend production build,一般使用者不需要另外安裝 Node.js 或 pnpm。

## 直接執行

不需要常駐安裝,直接用 `uvx` 啟動:

```bash
uvx afterthread
```

`uvx` 會在自己管理的暫存虛擬環境裡解析依賴並啟動服務。同一個環境第一次執行需要網路下載依賴,之後會用快取,離線也能跑。啟動後終端機只會印一行「資料目錄在哪、資料庫在哪」的訊息(絕不會印出 API key 等機密),接著用瀏覽器打開 <http://127.0.0.1:8000> 就是完整介面。

### 裝成常駐指令

不想每次都打一長串 `uvx --from ...`,可以裝成一個固定指令,之後直接打 `afterthread`:

```bash
uv tool install afterthread
# 或用 pipx:
pipx install afterthread
```

### 常用旗標

`afterthread --help` 可以看完整說明。

| 旗標 | 預設值 | 對應環境變數 |
| --- | --- | --- |
| `--host` | `127.0.0.1`(只綁本機,不對外網開放——這是單人本機工具,不是要曝露在網路上的服務) | `AFTERTHREAD_HOST` |
| `--port` | `8000` | `AFTERTHREAD_PORT` |
| `--data-dir` | 見下方「首次設定」 | `AFTERTHREAD_DATA_DIR` |
| `--version` | 印出版本後結束 | — |

### 升級

`uvx` 使用者可重新整理解析結果;常駐安裝則用對應工具的 upgrade 指令:

```bash
uvx --refresh afterthread
uv tool upgrade afterthread
# 或
pipx upgrade afterthread
```

## 從原始碼建置

開發者若要驗證尚未發布的 checkout,需先安裝 `pnpm` 與 `uv`,再從 repo 根目錄執行:

```bash
bash scripts/build-wheel.sh
uvx --from backend/dist/afterthread-*.whl afterthread
```

腳本會清掉 `backend/dist/` 內舊的 wheel／sdist,再產生唯一一組已內嵌前端的發布產物。

## 首次設定

- **資料目錄**:預設是 `$XDG_DATA_HOME/afterthread`,沒設定 `XDG_DATA_HOME` 時退回 `~/.local/share/afterthread`。可以用 `--data-dir <path>` 或環境變數 `AFTERTHREAD_DATA_DIR` 覆寫。第一次啟動時,若目錄不存在會自動建立(權限設為只有你自己能讀寫);已存在的目錄則不會被動權限。SQLite 資料庫檔與工具目錄(見 [KB 工具安裝指南](kb-tools.md))都會落在這裡。
- **設定檔 `.env`**:在 `<資料目錄>/.env` 建立設定檔;從原始碼開發時可複製 `backend/.env.example`。啟動時只有資料目錄下的 `.env` 會被讀取。啟用 AI 的最小設定如下:

    ```dotenv
    OPENAI_BASE_URL=https://your-endpoint.example/v1
    OPENAI_API_KEY=your-key-if-required
    OPENAI_MODEL=your-model
    ```
- **啟用 AI 功能(必填)**:`OPENAI_BASE_URL` 與 `OPENAI_MODEL` 兩者都要填,且 base URL 需能解析為合法的 http/https 網址,才算「已設定」;`OPENAI_API_KEY` 是否需要則視該 endpoint 而定(不是判斷「已設定」的條件之一)。相容任何 OpenAI-compatible 的 chat completions endpoint。兩者留空時,AI 快速捕捉／AI 補齊／AI 進度更新／工具安裝都會顯示「尚未設定」,但手動新增、編輯、刪除、篩選、回顧完全不受影響。

!!! tip "使用 GLM5.2 等 1M-token context 模型"
    後端的預設值本身就已經是為大 context 模型調校過的(逾時 120 秒、prompt 預算 20 萬 token)。prompt 預算以 **token** 計價(模型真正的限制是 token,不是字元):系統會從近期每次 LLM 互動實際回報的 token 數,動態估一個「字元↔token 比值」,把 token 預算換算成當下該套用的字元上限,剛啟動、樣本還不夠時用保守比值,行為與舊的純字元預算等價(細節見[AI 工具呼叫是怎麼運作的](tool-calling.md))。如果內部 LLM 是 GLM5.2 這類 1M-token context 的模型,`.env.example` 內建了進一步調高的建議:

    ```bash
    LLM_PROMPT_BUDGET_TOKENS=800000  # 讓超大項目也能整項進 prompt 不截斷
    OPENAI_TIMEOUT_SECONDS=300       # 長 context 生成較慢,放寬逾時避免誤判 502
    ```

完整的設定項目清單,見[設定參考](config-reference.md)。
