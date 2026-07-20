# 設定參考

所有設定都是環境變數,寫在 `<資料目錄>/.env`(打包模式)或 `backend/.env`(開發模式)裡,範本見 `backend/.env.example`。數值型設定都在啟動時就驗證邊界——超出範圍會讓服務直接啟動失敗,而不是留到第一次呼叫才出錯。

## LLM 連線

| 變數 | 預設值 | 邊界 | 說明 |
| --- | --- | --- | --- |
| `OPENAI_BASE_URL` | (空) | — | OpenAI-compatible endpoint 的 base URL。留空 = AI 功能未設定。 |
| `OPENAI_API_KEY` | (空) | — | 對應 endpoint 的 API key。**不是**判斷「是否已設定」的條件之一——有些相容 gateway 不需要 key。 |
| `OPENAI_MODEL` | (空) | — | 呼叫該 endpoint 使用的 model 名稱。 |
| `OPENAI_TIMEOUT_SECONDS` | 120 | (0, 1800] | 單次請求逾時秒數,同時是「第一次呼叫 + 一次修正重試」整體的時間上限。 |
| `OPENAI_MAX_OUTPUT_TOKENS` | 未設定 | [1, 1000000] | 選填,設定時才以 `max_tokens` 送給 endpoint(有些 reasoning endpoint 會拒絕顯式帶這個參數)。 |

## Prompt 與工具對話的 token 預算

| 變數 | 預設值 | 邊界 | 說明 |
| --- | --- | --- | --- |
| `LLM_PROMPT_BUDGET_TOKENS` | 200000 | [4000, 1000000] | AI 補齊/AI 進度更新/工具安裝組 prompt 時,內容的 token 預算。實際套用時經動態「字元↔token 比值」換算成字元上限(見[AI 工具呼叫是怎麼運作的](tool-calling.md))。 |
| `LLM_TOOL_CONVERSATION_BUDGET_TOKENS` | 500000 | [50000, 1000000] | 工具迴圈中「實際送給模型的對話」token 預算,超過就停止再帶工具、直接收斂成最後回答。 |

## AI 日誌

| 變數 | 預設值 | 邊界 | 說明 |
| --- | --- | --- | --- |
| `LLM_LOG_MAX_ENTRIES` | 50 | [1, 1000] | 「AI 日誌」頁顯示的最近互動筆數上限(記憶體內環狀緩衝,隨程序重啟清空)。 |
| `LLM_LOG_BODY_MAX_CHARS` | 200000 | [1000, 2000000] | 單次互動中,任一則請求/回應內容儲存時的字元數上限。 |
| `LLM_LOG_FILE` | 未設定 | — | 選填。設定後,每次 LLM 互動會額外追加寫入這個 JSONL 檔案;預設關閉——記錄含個人記憶內容,落不落地是使用者自己的隱私選擇。 |
| `LLM_LOG_FILE_MAX_BYTES` | 50000000(50MB) | [1000000, 1000000000] | 上面這個 JSONL 檔案的 rotation 門檻:超過就把現檔改名(加時間戳)、開新檔。舊檔不會自動刪除。只在設定了 `LLM_LOG_FILE` 時有意義。 |

## 工具呼叫

| 變數 | 預設值 | 邊界 | 說明 |
| --- | --- | --- | --- |
| `TOOLS_DIR` | 未設定(打包模式自動注入 `<資料目錄>/tools`) | — | 已安裝工具套件所在目錄;留空 = 工具功能整個關閉,AI 完全不會看到任何工具。 |
| `LLM_TOOL_ROUNDS_MAX` | 8 | [1, 64] | 一次 AI 動作最多允許幾輪工具呼叫,用完就強制模型直接給答案。 |
| `LLM_TOOL_TIMEOUT_SECONDS` | 60 | (0, 600] | 單一工具子行程的逾時秒數。 |
| `LLM_TOOL_OUTPUT_MAX_CHARS` | 50000 | [1000, 500000] | 工具輸出餵回模型前的字元上限,超過會截斷並標記。 |

## KB 網頁安裝器

| 變數 | 預設值 | 邊界 | 說明 |
| --- | --- | --- | --- |
| `TOOL_INSTALL_MAX_ROUNDS` | 24 | [4, 64] | 安裝一個工具時,建置 session 的工具輪數上限。 |
| `TOOL_INSTALL_TIMEOUT_SECONDS` | 900 | (0, 3600] | 整個安裝 session 的總時間上限(15 分鐘)。 |
| `TOOL_INSTALL_SHELL_TIMEOUT_SECONDS` | 120 | (0, 600] | 安裝過程中單一 `run_shell` 指令的逾時秒數。 |

## 資料庫與其他

| 變數 | 預設值 | 邊界 | 說明 |
| --- | --- | --- | --- |
| `DATABASE_URL` | `sqlite:///./context_memory.db` | — | SQLAlchemy URL。**只支援 SQLite**,且必須是檔案型(不可為 in-memory)。 |
| `STALE_AFTER_DAYS` | 14 | [0, 36500] | 非終態項目的 `updated` 超過這個天數,會被標記為陳舊。 |

!!! tip "GLM5.2(或其他 1M-token context 模型)建議值"
    ```bash
    LLM_PROMPT_BUDGET_TOKENS=800000  # 讓超大項目也能整項進 prompt,不截斷
    OPENAI_TIMEOUT_SECONDS=300       # 長 context 生成較慢,放寬逾時避免誤判 502
    ```

安裝與升級步驟見[安裝與啟動](install.md)。
