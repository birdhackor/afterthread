# web-v7 裁決紀錄

沿用 web-v4～v6 的體例：`docs/web-v7-plan.md` 寫**打算**做什麼，`docs/web-v7-design.md` 是設計本體，
本文件寫實作過程中計畫沒寫到的判斷，以及 review 的處置。逐階段接續，從 D48 起編號。

## D48（設計）：已審定的決策

以下是兩輪設計審查後定案、各階段派工都以它為前提的決策；推翻任何一條都要先改設計文件並重新審查。

| 決策 | 內容 | 推翻的代價 |
|---|---|---|
| 存放位置 | 草稿在套件層 `.afterthread.meta/draft/`，不在 `.staging`、不進 SQLite | 刪除工具、同名重裝、sweep、README 的 `.staging` 指示都要重新設計 |
| 接續模型 | 每一輪都從草稿複製到新的暫存工作區；草稿只在保存時換新 | 子行程與保存的競爭、唯一成果的保護要重新設計 |
| 識別 | 接續與放棄以 `(draft_id, generation)` 樂觀比對，`draft_id` 放棄後不重用 | 跨分頁的舊畫面會刪掉或重跑沒看過的成果 |
| 過期 | 只由 `current != base_vid` 判定；基底內容的變更是資訊、不是閘門 | 執行期寫檔的工具會讓草稿無故過期；也等於推翻 v5 §12 |
| 秘密 | 保存與複製進暫存時都用完整掃描器；命中的路徑保留上一份或不存；掃描失敗就不存 | 草稿會長期保存明文秘密 |
| 模型自述欄位 | `handoff`、`operator_action` 只用來顯示與交接，任何邏輯都不讀 | 會重開 v6 R11 不採用模型自報分類的理由 |
| 人接手的出口 | 發布草稿（不經 AI）是 P5 的範圍 | 只差設定的修正必須再付一輪 |

## D49（P1）：單一請求逾時的歸類

openai SDK（2.45.0）對單一請求的逾時擲 `openai.APITimeoutError`。它是 `APIConnectionError` 的子類別、屬於
`OpenAIError`，不是 builtin `TimeoutError`，所以只認 builtin `TimeoutError` 的歸類會把它算成上游錯誤。設計 §5
把「單一請求逾時」與「上游錯誤」分開，因此 `StructuredRunStats.failure` 把 `APITimeoutError` 記為
`request_timeout`，它的父類別與其他 SDK 錯誤仍記 `upstream`；請求本身擲出、整場期限沒有觸發的 builtin
`TimeoutError` 也記 `request_timeout`。擲出的例外與訊息不變（DJ）。卡片的 `request_timeout` 文案點名
`OPENAI_TIMEOUT_SECONDS`：`_get_client` 以它建出 client，作為每個請求的逾時。

## D50（P1）：`operator_action` 不限於沒做完

模型可以在 `run_shell` 裡帶新值驗證過程式、回 `ready=true` 發布，而套件的 `config.env` 仍要操作者貼上那一行。
提示因此要求：只要操作者必須在工作區外動手就寫 `operator_action`，不論做完與否；`handoff` 只在沒做完時寫。
前端在已發布的修訂卡片上也顯示「需要你動手」與交接筆記；停止原因在已發布的卡片上只在 `stopped_by` 有值
（預算結束了工具階段）時才顯示。設計 §4.1、§8 已照此更新。

## D51（P1）：前端不寫設定的上限值

設定的上限只定義在 `config.py` 的 `Field(le=…)`，超過上限的值在載入設定時就被拒絕。前端手抄一份，後端改邊界時
會無聲過時；卡片因此只點名設定名稱，不寫上限數字。

## D52（P2）：以中止訊號停止 builder 指令，不從外部殺

設計 §3.3 第 1 步原本寫「登記 `Popen`，逾時用 `tools._kill_process_group` 殺」。從外部
`killpg(getpgid(pid))` 可能落在 worker 的 `proc.wait()` 回收之後，打到已被重用的 PID，正是
`_communicate_bounded` 的 `teardown_lock`／`reaped` 要擋的情形。

實作改為每場 session 一個 `_BuilderShells`：一個 `threading.Event` 加上強參照的 task 集合。
`stop_and_wait` 先設訊號、再等 task。`_communicate_bounded` 在輪詢時看到訊號，就用它既有、在鎖下的
拆除流程殺掉並回收；訊號設下之後才要啟動的指令不啟動。

等待上限是 `tool_install_shell_timeout_seconds` 加上 `_REAP_TIMEOUT_SECONDS`。超過時不剝除、不發布，
job 失敗並說明；清理照做、名額照放，因為到那時還沒返回的 worker 不會再啟動指令。

不變量 DC 不變。設計 §3.3 第 1 步已照此更新。
