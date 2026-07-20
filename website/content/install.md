# 安裝與啟動

`afterthread` 目前不透過 PyPI 發佈(這是一個私人 repo),所以要先從原始碼建置一次 wheel 檔,再用它啟動服務。

## 1. 建置 wheel

前置需求:`pnpm`(建置前端)與 `uv`(建置後端、執行 `uvx`)。

在專案根目錄執行:

```bash
bash scripts/build-wheel.sh
```

完成後,`backend/dist/` 目錄下會出現一個內嵌了前端 production build 的 `afterthread-*.whl`(以及對應的原始碼套件 sdist)。

## 2. 啟動

### 不安裝,直接執行(`uvx`)

```bash
uvx --from backend/dist/afterthread-*.whl afterthread
```

`uvx` 會在自己管理的暫存虛擬環境裡解析依賴並啟動服務。同一個環境第一次執行需要網路下載依賴,之後會用快取,離線也能跑。啟動後終端機只會印一行「資料目錄在哪、資料庫在哪」的訊息(絕不會印出 API key 等機密),接著用瀏覽器打開 <http://127.0.0.1:8000> 就是完整介面。

### 裝成常駐指令

不想每次都打一長串 `uvx --from ...`,可以裝成一個固定指令,之後直接打 `afterthread`:

```bash
uv tool install --from backend/dist/afterthread-*.whl afterthread
# 或用 pipx:
pipx install backend/dist/afterthread-*.whl
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

版號目前固定在 `0.1.0`(還沒決定對外發佈節奏),所以「升級」就是拉新程式碼、重新建置、重新執行:

```bash
git pull
bash scripts/build-wheel.sh
uvx --refresh --from backend/dist/afterthread-*.whl afterthread
```

`uvx` 對本機 wheel 檔會快取解析結果,一般重新 build 後直接重跑就會抓到新內容;不放心的話加 `--refresh` 強制忽略快取。已用 `uv tool install` 裝成常駐指令的話,升級改用:

```bash
uv tool install --reinstall --from backend/dist/afterthread-*.whl afterthread
```

## 首次設定

- **資料目錄**:預設是 `$XDG_DATA_HOME/afterthread`,沒設定 `XDG_DATA_HOME` 時退回 `~/.local/share/afterthread`。可以用 `--data-dir <path>` 或環境變數 `AFTERTHREAD_DATA_DIR` 覆寫。第一次啟動時,若目錄不存在會自動建立(權限設為只有你自己能讀寫);已存在的目錄則不會被動權限。SQLite 資料庫檔與工具目錄(見 [KB 工具安裝指南](kb-tools.md))都會落在這裡。
- **設定檔 `.env`**:把 `backend/.env.example` 複製一份到 `<資料目錄>/.env`,依需要填入下面的變數。啟動時只有這個目錄下的 `.env` 會被讀取。
- **啟用 AI 功能(必填)**:`OPENAI_BASE_URL` 與 `OPENAI_MODEL` 兩者都要填,且 base URL 需能解析為合法的 http/https 網址,才算「已設定」;`OPENAI_API_KEY` 是否需要則視該 endpoint 而定(不是判斷「已設定」的條件之一)。相容任何 OpenAI-compatible 的 chat completions endpoint。兩者留空時,AI 快速捕捉／AI 補齊／AI 進度更新／工具安裝都會顯示「尚未設定」,但手動新增、編輯、刪除、篩選、回顧完全不受影響。

!!! tip "使用 GLM5.2 等 1M-token context 模型"
    後端的預設值本身就已經是為大 context 模型調校過的(逾時 120 秒、prompt 預算 20 萬 token)。prompt 預算以 **token** 計價(模型真正的限制是 token,不是字元):系統會從近期每次 LLM 互動實際回報的 token 數,動態估一個「字元↔token 比值」,把 token 預算換算成當下該套用的字元上限,剛啟動、樣本還不夠時用保守比值,行為與舊的純字元預算等價(細節見[AI 工具呼叫是怎麼運作的](tool-calling.md))。如果內部 LLM 是 GLM5.2 這類 1M-token context 的模型,`.env.example` 內建了進一步調高的建議:

    ```bash
    LLM_PROMPT_BUDGET_TOKENS=800000  # 讓超大項目也能整項進 prompt 不截斷
    OPENAI_TIMEOUT_SECONDS=300       # 長 context 生成較慢,放寬逾時避免誤判 502
    ```

完整的設定項目清單,見[設定參考](config-reference.md)。
