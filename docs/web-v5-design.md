# web-v5 設計：不可變版本 + 指標切換

本文件是 web-v5 的**設計本體**。`docs/web-v5-plan.md` 是最初的計畫，其設計章節已被取代。
已完成的 P1 的裁決在 `docs/web-v5-decisions.md`（D41 及其五個 review 附錄）。

**修訂紀錄**：四輪 design review（9 + 15 + 15 + 11 條 finding）與兩輪與操作者的討論。
第 16 節逐條記錄每一條 finding 的處置。四輪的性質依序是：

1. 規則**自相矛盾**
2. 規則**沒寫下來**
3. 規則寫下來了但**錯**，或兩條各自正確的規則**互相打架**
4. **上一輪的修法本身**有錯——三條是新寫的順序或推論錯誤，兩條推翻了上一輪的決定

## 1. 要解決什麼

v4 的 overall review 跑了九輪、33 個 finding，全部是同一個類別的實例：

> **可變的路徑就是身分。** 名稱 → 路徑 → *此刻*擺在那裡的東西。

P1 已經拆掉其中一個根因（開關就地改寫 manifest）。本設計拆掉另一個：**套件的內容會被原地
換掉**。

每一輪 review 的第一課，留在這裡當作實作時的提醒：

- **第二輪**：本設計自己引進了一個新的「名字 → 路徑 → 此刻在那裡的東西」（`current`），
  而第一版只寫了「切換是原子的」六個字。引進間接層就要同時寫出它的契約。
- **第三輪**：把一件事拆成兩個目錄之後，**每一個原本只需要一個答案的地方都變成需要兩個**，
  漏掉的不會報錯，只會安靜答錯。
- **第四輪**：**修法本身要當成新程式碼審**。三條 P1 是上一輪修法的順序錯誤與不成立的推論
  （單一版本的 discard 永遠 409、遷移的 rollback 救不回已完成的套件、「空目錄一定是我們的
  殘骸」無法證明）。**不要相信「這是為了修 X 而寫的，所以它是對的」。**

## 2. 操作環境與嚴格度校準

本專案是**單人本機工具**，這件事是設計前提，不是免責聲明。

- **一個操作者，通常一個瀏覽器分頁。** 沒有多使用者、沒有多租戶。
- **建置類工作要花錢且以分鐘計。**
- **沒有敵對行為者**（D21）。手改是**支援的工作流程**。
- **工具集規模是個位數到數十個。**

**嚴格度校準**，適用於本設計的 review 與實作：

- **併發類的 finding 要說出一個單一操作者做得出來的實際順序**，並以**對這個操作者的後果**
  判斷嚴重性，而不是理論可達性。
- **修法要與嚴重性成比例。**
- **但這個折扣不涵蓋三類問題**：
  1. **正確性**——同一個問題在兩處會得到兩個答案、或某個函式被餵了錯的東西卻不會報錯。
  2. **失效復原**——當機、中斷、寫入失敗之後留下的狀態，以及有沒有路徑離開那個狀態。
  3. **資料遺失**——尤其是憑證、設定值與 origin 這種**唯一副本**。
- **「不成立」的宣稱不打折。** 第 15 節的每一列都要對得起這一條。

## 3. 版面

```
<tools_dir>/<name>/                 # 整個目錄是後端管的
    .afterthread.meta/              #   後端的：這個工具「現在」的狀態
        state.json                  #     { "afterthread": ..., "enabled": bool }
        current                     #     目前生效的 <vid>（契約見第 7 節）
    versions/                       #   後端的：由後端建立、命名、刪除
        <vid>/                      #     一個版本
            .afterthread.meta/      #       後端的：這一版的資料
                origin.json         #         來源、修訂意見、前一版（＝已提交標記）
                summary.json        #         這一版的 AI 總結（人類看的）
            tool.json               #       以下是工具自己的東西
            run.py …
            .env                    #       這一版隨附的設定（builder 寫的，見下）
    .env                            #   你的憑證與跨版本設定（你的，見下）
```

### 所有權規則

- **套件層 `<name>/`**：`.afterthread.meta/` 與 `versions/` 是後端的；`.env` 是**操作者的**。
  工具的檔案在這一層**一個都沒有**。
- **版本層 `versions/<vid>/`**：`.afterthread.meta/` 是後端的，**其餘全部是工具的**，
  包括那一版自己的 `.env`。

### 兩層 `.env`：這是第四輪推翻第三輪的結果

第三輪為了讓修訂只有一個提交點，**取消了 builder 出貨 `.env` 的能力**。第四輪證明那個修法
有代價而且推理不完整：

- `.env` **不只裝秘密**。它可以裝 endpoint、mode、預設值——**只有 builder 知道的值**。只留
  key 名而丟掉值，等於發布一個操作者無從補齊的版本（finding 6）。
- 「本來沒有 `.env`，builder 做的那份出貨」是**既有的、寫下理由的標準接受**（R3-1，
  `tool_builder.py:1932`，並有測試釘住），理由是「不然修訂就無法執行『把 key 存進 .env』
  這種意見」。第三輪等於在沒有反駁該理由的情況下推翻它。

正確的解法不是二選一，而是**分層**——原本擠在一個檔案裡的兩種東西本來就該分開：

| | 誰的 | 存活範圍 | 誰寫 |
|---|---|---|---|
| `<name>/.env` | **操作者的** | 跨版本 | 安裝表單的秘密注入、以及你自己手改 |
| `versions/<vid>/.env` | **這一版的** | 跟著版本 | builder，隨版本一起發布 |

- **載入時合併，套件層優先**：你手動設定的值蓋得過 builder 的預設。
- **修訂仍然只有一個提交點**：版本的 `.env` 隨 shell 一起 rename 進去，套件層的 `.env`
  在修訂期間**完全不動**。第三輪 finding 5 指出的「兩個發布點沒有安全先後」因此不成立，
  而且不需要付出丟掉設定值的代價。
- **「丟掉這一版」現在會一併還原那一版的設定**，比第三輪的版本更正確。
- 兩份都要過大小上限、都要進遮蔽集合、都要過既有的 `.env` 政策閘與內嵌秘密掃描。

**第一輪的 `.env` hoist 因此整個消失。** hoist 之所以存在，是因為當時只有一個 `.env` 位置；
分層之後 builder 寫的那份**留在原地**（build 根 → 版本根），操作者的憑證住在套件層。

### 「不准碰」不是強制力

builder 的指示會寫明保留名稱，但**指示不能取代檢查**。現行的 `_strip_builder_sidecars`
必須繼續存在，比對對象是「build 根目錄下的 `.afterthread.meta/`」。

## 4. 兩個目錄錨點：哪個函式拿到哪一個

餵錯目錄**不會報錯，只會安靜地答錯**。**規則：不留任何一個叫 `directory` 而語意含糊的
參數。** 引入 `ResolvedPackage(package_root, version_root, vid)`，兩個路徑成員是**不同型別**
（同 D40 r5 的手法），餵錯是型別錯誤。

### 只需要一個錨點的

| 既有函式 | 拿哪一個 |
|---|---|
| `package_enabled` / `_read_enabled_state` | package_root |
| `_read_manifest_object` | version_root |
| `package_identity`（`tool.json` 的 inode） | version_root |
| `_run_tool_subprocess` 的 cwd | version_root |
| `directory_identity`（執行中保護的登記） | version_root |
| `read_tool_meta`（總結） | version_root 的 `.afterthread.meta/` |

### 同時需要兩個的——必須整個 `ResolvedPackage` 傳進去

| 函式 | 它同時做的事 |
|---|---|
| **`_scan_package`** | **讀 state（package）與 manifest（version）**——見下，第三輪的表把它漏了 |
| `_make_handler` / `_run_tool_subprocess` | 查啟用（package）、讀兩份 `.env`（package + version）、驗 manifest identity（version）、當 cwd（version） |
| `_load_tool_dotenv` / `_env_values_for_package` | 合併兩層 `.env`（package + version） |
| `tool_meta` 的總結 prompt 組裝 | 版本檔案 inventory（version）、`.env` 的 key 名（兩層） |
| `validate_package` | manifest 與內容（build/version root）、`.env` 大小閘（見下） |
| `delete_tool`、`discard`、`.stale-` 清掃 | 走訪**所有** version_root（見下） |

**`_scan_package` 是被漏掉的那一個**（第四輪 finding 5）。它在同一個 `directory` 上讀
`_read_enabled_state`（`tools.py:942`）**和** manifest。第三輪的單錨點表只寫了「`_scan_package`
的 manifest 讀取 → version_root」，實作者照字面把它的 `directory` 改成 version_root，state
就會從錯的位置讀——配合新版「state 缺席即 disabled」，**所有合法工具會一次全部被列成停用，
而且沒有任何例外或錯誤訊息**。

### 「這底下還有沒有東西在跑」只有一個拼法，而且有三個問這個問題的地方

已查證兩個既有缺陷：handler 登記的是**子行程實際執行的目錄**（`tools.py:3993`），而
`delete_tool` 查**套件根**（`tools.py:4357`）、`.stale-` 清掃查 **stale 套件根**
（`tool_builder.py:2494`）。分層之後兩者都對不到 inode。

第四輪 finding 2 指出**還有第三個**：第 9 節的 discard 要刪 `versions/V`，同樣需要這個判斷，
而第三輪只列了兩個。三個地方共用同一個函式，不要有第二種拼法。

**版本層的延後刪除需要一個落點**（第三輪沒定義）：把 `versions/<vid>` 改名成
`versions/.at-stale-<vid>`。這個名字**以點開頭，過不了 vid 語法**，所以 `current` 永遠不可能
指到它；同一個清掃同時走訪套件層的 `.stale-` 與每個套件 `versions/` 底下的 `.at-stale-`，
用同一個所有版本判斷決定能不能收。少了這個落點，discard 面對執行中的版本只有三個都錯的
選項：直接 rmtree（子行程 ENOENT）、整包搬成 `.stale-`（連已切回的 P 一起下線）、或什麼都
不做（discard 永遠不會完成）。

### `.env` 大小閘的時機

現行閘讀「傳進來的那個目錄的 `.env`」（`tools.py:1160`）。分層之後要檢查**兩處**：

1. **驗證 build 時**：build 根的 `.env`（＝未來的版本 `.env`）
2. **注入表單秘密之後、rename 之前**：shell 的**套件層** `.env`

第 2 次必須在 rename 之前，這樣失敗時整場安裝還沒被回報成功，丟掉暫存即可。少了它，一個
超大的 `.env` 會安裝成功，執行期再靜默降級成空環境（`tools.py:3273`）。

## 5. 三層可變性

| 東西 | 什麼時候固定 |
|---|---|
| 版本裡的**工具檔案**（含該版 `.env`） | 發布後後端不再修改（你手動改仍然可以，見第 11 節） |
| `origin.json` | **在提交那一刻就已經在裡面**，之後不再修改 |
| `summary.json` | **可以隨時整份原子替換** |

套件層的 `.env` 不在這張表裡——它是操作者的，隨時可改，不屬於任何一版。

## 6. `.afterthread.meta/` 的合法狀態

### 套件層 `<name>/.afterthread.meta/`

| 狀態 | 判定 |
|---|---|
| `<name>/` 已存在（**任何內容，含空目錄**） | 全新安裝一律拒絕（見下） |
| 目錄不存在（但 `<name>/` 非空） | 未遷移的舊版面套件：invalid，訊息指向遷移腳本 |
| `current` 缺、壞、或指不到一個完整版本 | 套件 **invalid，不啟用、不執行**（第 7 節，fail closed） |
| `state.json` 缺 | **視為 disabled** |
| `state.json` 是別人的（FOREIGN） | 同 P1 已定語意：視為缺，且**永不覆寫、永不刪除** |
| `state.json` 讀不到（UNREADABLE） | fail closed，視為 disabled |

**第一列是第四輪推翻第三輪的另一處。** 第三輪寫「空的 `<name>/` 只可能是我們自己中斷的
安裝，所以下一次安裝可以取代它」——**兩個錯**：第三輪自己把 mkdir 佔名拿掉之後，新流程
**沒有任何一步能產生**那種空目錄；而且現行安裝對**任何**既有 target 都拒絕
（`tool_builder.py:1729`），取代一個操作者手動建立的空目錄會丟掉它的 mode／ACL／xattr 與
「先佔名字」的意圖。**保持現行行為：既有名稱一律拒絕。**（那是一次 check-then-act instant，
屬於已接受的殘留類。）

**`state.json` 缺席的預設必須從現行的 `true` 翻成 `false`。** 已查證 `_effective_enabled`
（`tools.py:757`）：狀態不是我們的、manifest 又讀不到時回傳 `True`。新版面下套件層沒有
`tool.json`，於是一個只寫到一半的 meta 目錄會**把工具自己打開**。修法是**刪掉整條 legacy
fallback**，前提是第 14 節的**全有全無**遷移。

### 版本層 `versions/<vid>/.afterthread.meta/`

| 狀態 | 判定 |
|---|---|
| `origin.json` 在且合法 | 這一版**已提交**——它同時就是提交標記 |
| `origin.json` 缺或壞 | **未提交**：不可被 `current` 指向、找前一版時跳過、可被清理 |
| `summary.json` 缺 | **正常**（總結晚於發布） |
| `summary.json` 壞 | 視為沒有總結（沿用現行 `read_tool_meta` 的 total 語意） |

## 7. `current` 的讀寫契約

**vid 的語法**：`20260728T134501Z-a3f9c1`，正規式 `^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}$`。
不含路徑分隔符也不含點，所以 `../../.staging/<uuid>` 是**語法錯誤**——把穿越變成不可表示，
比事後檢查便宜也可靠。這也讓 `.at-stale-<vid>` 這種落點名字天生指不到。

**讀（「目標驗證」）**：

- 有界讀取器（`O_NOFOLLOW` + `S_ISREG`），上限 64 bytes
- 內容去掉尾端換行後必須**恰好**符合 vid 語法；空檔案是錯誤
- `versions/<vid>/` 必須存在、是**真的目錄**（非 symlink）、且**已提交**
- 任何一項不成立 → **這個套件沒有生效版本**：invalid、不列為可用、不廣告、不執行。
  **不猜、不 fallback 到最新的一版**

這套驗證在讀取、discard 選前一版、以及第 9 節的 `has_previous` 三個地方共用同一個實作。

**寫**：走與側檔相同的原子發布器（mkstemp → fchmod → write → fsync → `os.replace` →
fsync 目錄）。

### 持久化要誠實回報，但**不能改既有 wrapper 的回傳型別**

已查證現行發布器在 `os.replace` 之後的目錄 fsync 是 `contextlib.suppress(OSError)` 包起來
的，失敗也回 `True`（`tools.py:1643`）。對開關來說那是對的；但 discard 要**先確定指標落地、
才可以毀掉舊版**。

**但第四輪 finding 3 指出把共用底層改成 tuple 會靜默出事**：`write_tool_meta` 與
`write_package_state` 直接回傳底層的布林（`tools.py:1844`、`:1952`），呼叫端寫
`if not write_tool_meta(...)`（`tools.py:2487`）——**tuple 在 Python 恆為 truthy**，
`(False, False)` 會被當成成功。所以：

- 底層 `_write_package_file_atomic` 回傳 `PublishResult(published, durable)`
- **既有的兩個 wrapper 在自己的邊界投影成 `bool`**（回傳 `result.published`），呼叫端
  一行都不用改
- **只有新的 `publish_current` 把完整結果往上傳**，也只有 discard 讀 `durable`
- 安裝與修訂發布 `current` 時**不要求** `durable`：它們發布之前的東西都還在暫存或還沒有人
  指向，失敗就是整場失敗，沒有「毀掉還需要的東西」這一步

### 指標 durable 不代表它指向的版本 durable

第四輪 finding 4：`current` 所在目錄的 fsync **不能替 `versions/` 底下先發生的 rename、
版本檔案內容與目錄項宣稱任何事**。斷電後可能保留 `current=V` 而 V 的內容或目錄項沒落盤，
套件變成「有指標、沒版本」的 invalid，而且沒有自動離開的路徑；`origin.json` 也跟著沒了，
所以第 15 節「origin 不會永久遺失」那一列也一併不成立。

**規則：版本要先 durable，才可以發布指向它的 `current`。**

1. 組 shell 時，每個檔案 write → fsync
2. `os.rename(shell_root, versions/<vid>)` 之後，**fsync `versions/` 目錄**
3. **然後才**發布 `current`

成本是一個工具包（通常幾個檔案）的 fsync，可以忽略。

## 8. 生命週期

### 暫存區的三個名字

| 名字 | 是什麼 |
|---|---|
| `session_root` = `<tools_dir>/.staging/<uuid>/` | 這一場工作的全部，**`finally` 永遠清掉這個** |
| `build_root` = `session_root/build/` | builder 的工作區 |
| `shell_root` = `session_root/shell/` | 組好的成品 |

現行 `_cleanup_staging` 只遞迴刪掉**它收到的那個路徑**（`tool_builder.py:2543`）。若 builder
改成拿 `build_root`，清理就只刪 `build/`，含 `.env` 的 `shell/` 會**永久留下一份憑證**。
所以三個名字要明確分開，而**清理一律以 `session_root` 為對象**。

暫存區放在 `<tools_dir>/.staging/` 底下是**結構性**要求：現行程式碼靠「staging 與 target
同在 `<tools_dir>` 底下」證明 rename 不會 EXDEV（`tool_builder.py:2188`）。

### 全新安裝

1. builder 在 `build_root` 建置（現行行為，不變）
2. 驗證 build 內容；檢查 build 根的 `.env` 大小
3. 在 `shell_root` 組出**完整**的套件（每個檔案 write → fsync）：
   - `versions/<vid>/` ← build 的內容，**含 builder 寫的 `.env`（留在版本裡，不 hoist）**
   - `versions/<vid>/.afterthread.meta/origin.json`（`previous` 為 null）
   - `.afterthread.meta/state.json` 與 `.afterthread.meta/current`
4. 把表單秘密注入 shell 的**套件層** `.env`；再檢查一次套件層 `.env` 大小
5. **確認 `<name>` 不存在**（既有一律拒絕，第 6 節），然後 `os.rename(shell_root, <name>)`
   ← **這一刻才叫安裝完成**
6. 之後產生 `summary.json`

### 修訂

1. builder 在 `build_root` 建置（不變）
2. 驗證（不變）
3. 在 `shell_root` 組出**一個版本目錄**的內容（每個檔案 write → fsync）：build 的內容
   （含該版 `.env`）+ `.afterthread.meta/origin.json`（含這次的意見，`previous` = 目前的 vid）
4. `os.rename(shell_root, <name>/versions/<vid>)`，然後 **fsync `versions/`**
5. **發布 `current`** ← **這一刻才叫修訂完成**
6. 之後產生 `summary.json`

套件層的 `.env` 在整場修訂中**完全不動**。第 5 步之前新版本已經在磁碟上而且是**完整已提交**
的，但沒有任何東西指向它。`current` 是唯一的提交點。

**vid 碰撞是要檢查的正常失敗，不是前提。** 六位 hex 只有 24 bits；時鐘回撥、同一秒重試、
或先前未發布的殘骸都可能讓名字已存在。偵測到就重新產生 vid 並重試（有限次），不要讓一場
花了幾分鐘和金錢的建置在提交那一刻失敗。

### 執行

- **廣告的當下記住 `<vid>`**，連同那一版的規格一起
- **呼叫的時候直接跑那一版**，不再去讀 `current`

**唯一的例外是你在對話進行中親手丟掉那一版**（第 9 節）。廣告不是執行，那段期間沒有任何
identity 登記，discard 的延後判定看不到它。這是**明示接受**的：後果是模型呼叫時得到一個
明確的拒絕（「這個版本已被丟棄」），而那是你幾秒前自己做的動作。不引入 reservation／
refcount，理由是第 2 節的比例原則。

### 列表與總結讀取

`list_tools` 對每個套件**只解析一次 `current`**，規格與總結都從**同一個**版本目錄讀。

**總結的 GET 也要綁版本**（第四輪 finding 8）。現行 GET 只帶名稱，後端自己重新解析當下的
current（`routers/tools.py:401`），而前端已經用實例身分當 cache key（`toolSummary.js:82`）。
於是：展開 V 的總結送出 GET → 丟掉 V 切回 P → 那個 GET 讀到 P 的總結，卻寫進以 V 為 key 的
cache，畫面把另一個實作的總結掛在 V 上，後續意見還會基於錯誤內容。做法：**回應帶上它實際
讀的 vid，前端不符就丟棄不寫 cache**（或請求帶 expected vid 由後端拒絕，兩者擇一並寫死）。
少了這條，第 15 節「後端造成的總結錯配已消失」是假的。

### 刪除工具

整個 `<name>/` 移除；延後判定依第 4 節，要看**所有**版本目錄——`.stale-` 清掃也一樣。

## 9. 版本保留與「丟掉這一版」

**所有版本永久保留，沒有自動回收。** 一個版本只有兩種方式消失：你按「丟掉這一版」，或你
刪掉整個工具。

**「前一版」來自 `origin.json` 記下的 `previous`，不是資料夾名稱排序。**

### 狀態轉移，順序是承重的

1. 解析 `current` 得到 V，讀 V 的 `origin.json` 得到 `previous` = P
2. **`P` 為 null** → 這是唯一的一版，等同**刪掉整個工具**，走現有的確認對話框。**結束**
3. `P` 必須通過第 7 節的**目標驗證**，而且 `P != V`。任一項不成立 → 回
   `lineage_unavailable` 409，**不刪 V**
4. 發布 `current` = P，且**必須拿到 `durable=True`**（第 7 節）
5. **然後才**處理 `versions/V`：沒有東西在跑就刪除；有的話改名成 `versions/.at-stale-V`
   交給清掃（第 4 節）

**第 2 步必須排在第 3 步前面**（第四輪 finding 1）：`null` 不可能通過版本目標驗證，第三輪
把驗證寫在分流之前，等於**單一版本的工具按「丟掉這一版」永遠回 409**，而且與
`has_previous=false` 的確認流程互相矛盾。這是照字面實作就會穩定失敗、但只有寫了
sole-version 測試才會被發現的那種錯。

第 3 步是第三輪 finding 6：你手動移除舊版 P 的目錄或它的 `origin.json` 之後在 UI 上丟掉 V，
沒有這步驗證，後端會先把 `current` 指向不存在的版本，再刪掉唯一還能用的 V。`P == V` 時更糟：
寫回 V 再刪掉 V，直接做出懸空指標。

第 4、5 步的順序加上 `durable` 要求：先改指標再毀舊版，所以任何一步中斷都**不會留下懸空的
`current`**；最壞的殘留是一個沒人指向的版本目錄。

### API 與身分

**`DELETE /api/tools/{name}/versions/{vid}`**。現行刪除只用名稱定址
（`routers/tools.py:238`、`ToolsPage.jsx:1508`），沿用會讓「畫面顯示 A、你按確認、伺服器
刪掉 B」變成可能。

**`current_vid` 一旦存在，就必須成為前端的列身分。** 這不是重提裁決紀錄 #7——是 #7 自己
寫下的觸發條件發生了。現行前端用 `name + description` 當代理，程式註解自己說那只是因為
API 沒有真實身分（`toolSummary.js:82`、`:102`）。新的出錯情境**單一分頁就會發生**：在 V 的
欄位打了修訂意見還沒送出 → 丟掉 V 退回 P → P 的 description 相同 → row 不 remount，草稿
存活 → 按送出，寫給 V 的意見拿去修訂 P，並花掉一場建置。所以：

- `GET /api/tools` 增加 `current_vid` 與 `has_previous`
- `toolInstanceKey` / summary cache key / job / ledger **一起**改用 `current_vid`
- 版本相關的請求（revise、regenerate、discard）都帶 expected vid，總結 GET 依第 8 節處理

**`has_previous` 的語意是「前一版存在**且通過目標驗證**」**，不是「`previous` 欄位非 null」。
這讓 UI 不會提供一個按下去必然失敗的動作，也讓 `lineage_unavailable` 409 幾乎不可達——但
它仍必須存在，因為手改可以在你看到畫面之後才把 P 弄壞。

### 三種 409，各有各的前端動作

| 錯誤碼 | 什麼情況 | 前端該做什麼 |
|---|---|---|
| `version_mismatch` | 畫面過期，伺服器上的 current 已經不是你看到的那個 | **重抓列表並 remount 該列** |
| `job_busy` | 全域名額被佔用 | 保留操作，稍後再試 |
| `lineage_unavailable` | `previous` 不存在或壞掉（手改的後果） | **不要重試、不要 refetch**——顯示可行動的說明，並提供「刪掉整個工具」 |

第三種是第四輪 finding 10：把它併進前兩種任一種，UI 會變成無限重抓或無限重試，而狀態不會
改變。三種用**不同的結構化錯誤碼**，不要讓前端解析訊息文字。

### 沒有生效版本的列長什麼樣

- `description` 對這種列改為**可為 null**（現行 `ToolSummary.description` 是必填字串，
  `schemas.py:516`），前端顯示錯誤原因而不是描述
- `current_vid` / `has_previous` 為 null / false
- 這種列只允許「刪除工具」，不允許開關、修訂、重新產生

## 10. 移除「定版」

**`finalized` 整個拿掉**，連同 409 `tool_finalized`、`PATCH /api/tools/{name}/summary`、
兩道前置閘、換裝前的重讀、`StoreRefusal.FINALIZED`、以及前端兩顆按鈕和它們背後那套不對稱
閘門規則。

**第一個用途**（保護可變的總結）消失：總結屬於版本。**第二個沒寫在名字上的用途**是中止一場
進行中的修訂（`tool_builder.py:2253`），取代它的是**「丟掉這一版」**。

**已知且明示接受的語意差額**（裁決紀錄 #10）：舊閘門是「這一版永遠不會上線」，新做法是
「先上線但退得回去」。理由是**對照組不是舊閘門而是今天的實際行為**——今天若沒在建置中途按
定版，修訂完成後**舊實作就被刪掉了**。

**「重新產生總結」保留。**

## 11. 手改仍然被接住

手動編輯版本目錄裡的工具檔案仍然是**支援的行為**（D21）。「不可變」是**後端自己的操作之間**
的約定，不是對你強制的性質。

現行的「對話中途 `tool.json` 被改過就拒絕執行」保留不變。**範圍要說清楚**：
`package_identity` 只 lstat `tool.json`（`tools.py:2637`）。改 `run.py` 而不動 `tool.json`
時，實作變了但身分沒變，**這道檢查不會拒絕**，該版的 `summary.json` 也仍在描述改之前的實作。

手改會製造第 9 節第 3 步要擋的狀態，那不是攻擊，是支援的工作流程的正常後果——所以擋在
discard 並給它自己的錯誤碼，而不是禁止手改。

## 12. 併發

**全域一次一個工作維持不變**（`_admit_job`）。安裝、修訂、同步重新產生總結，**以及「丟掉
這一版」**共用一個名額。

1. 兩場修訂**同一個工具**時，`current` 是會被覆蓋的暫存器。
2. **AI 日誌的歸屬**：後端問的是「這個工作流程最新的一筆」而不是「我這次產生的那一筆」。
   這是裁決紀錄 #3 已接受的殘留，而它可以被接受**正是因為**全域一次一個。

**要並行時的正確順序是先把日誌歸屬修對**，而不是先放寬。

一般的 AI 對話**不**受這個名額限制，這是刻意的，也是第 10 節那個語意差額與第 8 節「廣告後
被丟掉」的來源。

## 13. `origin.json` 與 `summary.json` 的寫入契約

現行的 `write_tool_meta` 是側檔的**唯一收口**：固定 schema、秘密遮蔽、大小上限、原子寫入，
**遮蔽失敗就整個不寫**。把 origin 拆成獨立檔案**不得**把這些丟掉。

- 兩個檔案都走**同等**的 typed / bounded / sanitized / fail-closed / atomic publisher
- **來源網址在捕捉當下就降級成無憑證的 provenance**（現行行為，不變）
- `origin.json` 裝著來源、**你的修訂意見**（自由文字，要過遮蔽）與 **`previous` vid**
- `origin.json` 在 rename 之前就要在 shell 裡，所以失敗只是丟掉暫存；`summary.json` 失敗
  只是沒有總結

## 14. 遷移

一次性離線腳本，操作者手動執行，執行時沒有服務在跑。

### 全有全無

**腳本要嘛把整個 tools 目錄遷移完成，要嘛讓它維持原狀。** 這是第 6 節刪掉 legacy fallback
的**前提**：只要允許部分遷移，就會有「舊扁平套件沒有 `current`、新 reader 又不再讀 manifest
`enabled`」的套件，它失效且沒有離開那個狀態的路徑。

**先跑一次唯讀的預檢**，走訪每一個套件，把所有問題一次列給操作者；**全部可遷移才開始寫**。
預檢要判定的：

- `.afterthread-state.json` 的三種語意（D41，`tools.py:662`）：**OURS**（`enabled` 搬進新的
  `state.json`）、**FOREIGN**（開關退回 manifest legacy 欄位，原檔當工具內容搬進版本，一個
  byte 都不動）、**UNREADABLE**（報告，不猜）
- `.ai_meta.json` 的三種狀態，見下
- 套件已經有 `versions/` 或 `.afterthread.meta/` 而**沒有我們的標記** → 操作者自己的東西，
  報告，不自作主張
- **`<name>.at-migrated` 或 `<name>.at-premigrate` 已經存在** → 一律報告（第四輪 finding 11：
  這兩個名字兼任交易日誌，所以不能同時是「可能是操作者資料」的名字。預檢階段拒絕，就不會
  在已經開始寫之後才發現）

### `.ai_meta.json`：缺席才是常態

summary sidecar **從未出貨**，所以「沒有 `.ai_meta.json`」是**最常見**的狀態。三種狀態都要
有規則，否則遷移會為每一個工具寫出指向**未提交版本**的 `current`，新讀取端一次全判 invalid：

| 狀態 | 做法 |
|---|---|
| **缺席**（常態） | 合成一份合法的最小 `origin.json`（來源標「遷移前既有」，`previous: null`） |
| **合法** | origin → `origin.json`，總結 → `summary.json`，`previous: null` |
| **讀不到／解析不了** | 原 bytes 保留，**停下來報告**——它是 origin 的唯一副本 |

### `.env` 的歸屬

既有扁平套件只有一個 `.env`，而且無法判斷裡面哪些是安裝時注入的憑證、哪些是 builder 的預設。
**整份放到套件層**（操作者的那一層），因為注入的憑證必須跨版本存活。版本層的 `.env` 從此由
後續的修訂產生。

### 每個套件的交換，用確定性的兄弟名字

1. `os.rename(shell, <name>.at-migrated)`
2. `os.rename(<name>, <name>.at-premigrate)`
3. `os.rename(<name>.at-migrated, <name>)`

重跑時看兄弟名字就知道停在哪一步，而且每一步都可以直接續做或回退。這兩個名字**不是**
`.stale-` 前綴，所以萬一誤啟動服務，既有的 stale 清掃不會把它們當殘骸收走。

**所有 `<name>.at-premigrate` 的刪除，一律等到每一個套件都換完之後才做**（第四輪 finding 9）。
第三輪把刪除寫在每個套件的第 4 步，於是「A 換完並刪掉舊 A → B 失敗 → 全部回退」變成不可能，
「全有全無」是假的。延後刪除之後，回退在任何時點都只是把兄弟名字換回去，而**回退本身不需要
刪除任何東西**，所以它自己不會半途失敗到無法收拾。

- **已經是新版面**（有我們標記的 `.afterthread.meta/state.json`）→ 跳過。第二次執行是 no-op
- **任何一個套件失敗** → 用兄弟名字把已經換好的全部回退，非零離開
- 開始寫之前**先完整備份整個 tools 目錄**，文件寫明位置、權限與成功後的處置

manifest 裡的 legacy `enabled` 在讀完之後**從 `tool.json` 移除**。遷移後
`_STATE_PUBLISH_LOCK` 可以刪除。

## 15. 這個設計讓哪些既有問題不成立

**這張表的每一列都要對得起第 2 節的最後一條**：宣稱不成立而其實成立，比 bug 更糟。

| 問題 | 為什麼不成立 | 前提條件 |
|---|---|---|
| R3／R6-1 執行中套件被換走或刪除 | 子行程的 cwd 是不可變的版本目錄 | **`delete_tool`、discard、`.stale-` 清掃三者都要以所有版本目錄的 identity 判定**，而且版本層要有 `.at-stale-` 落點（第 4 節） |
| R7-1 掃描→建 handler 的身分配對 | 廣告當下綁定 `<vid>` | 「不可變 + 不自動刪除 + 廣告當下綁定」三件事一起；**手動 discard 是明示的例外**（第 8 節） |
| R9-2 `list_tools` 撕裂的列 | 規格與總結來自同一個版本目錄 | 列表只解析一次 `current`（第 8 節） |
| R8-1 origin 永久遺失 | `origin.json` 在 rename 之前就在版本裡 | **版本必須先 durable 才發布 `current`**（第 7 節）；遷移對 `.ai_meta.json` 三種狀態都要有規則（第 14 節） |
| P1 r1-1 修訂覆蓋剛完成的切換 | 狀態檔不在被換掉的目錄裡 | — |
| 裁決 #9 修訂吞掉手改的狀態檔 | 同上 | — |
| **後端造成的**「總結描述的是另一個實作」 | 總結屬於版本 | **只限後端造成的錯配**；手改 `run.py` 之後總結仍會過期（第 11 節）；**總結 GET 必須綁版本**（第 8 節） |
| 修訂只有一個提交點 | 版本的 `.env` 隨版本一起 rename，套件層 `.env` 不動 | 兩層 `.env` 且套件層優先（第 3 節） |

**不會**因此消失：

- v4 R9-1 日誌編號跨行程重用（llm_log 的問題，與版面無關）
- 操作者手改造成的中途變動與總結過期（刻意保留，第 11 節）
- 裁決 #5（能替換 tools root 的人已有服務的 uid）
- 裁決 #3 的日誌歸屬（第 12 節）
- 裁決 #10 的語意差額（第 10 節）
- 廣告後被手動 discard 的那次呼叫失敗（第 8 節，明示接受）

## 16. design review 的處置

### 第一輪（9 條）

| # | 內容 | 處置 |
|---|---|---|
| 1 (P1) | 所有權規則自相矛盾 | 規則改寫（§3） |
| 2 (P1) | 沒把 staging 根 `.env` 拉到套件層 | 當時加了 hoist；**第四輪改成兩層 `.env`，hoist 消失**（§3） |
| 3 (P1) | 沒有原子發布，失敗留下佔名殘骸 | 暫存組裝 + 一次 rename（§8） |
| 4 (P1) | 遷移會重開 toggle 競態 | 離線一次性腳本（§14） |
| 5 (P1) | 沒有版本租約，R7-1 仍可達 | 全部保留 + 廣告當下綁定（§8、§15） |
| 6 (P1) | `origin.json` 掉了 fail-closed 契約 | 明訂同等 publisher（§13） |
| 7 (P2) | `finalized` 也是中止訊號 | 用 discard 取代；差額記入裁決 #10（§10） |
| 8 (P2) | `current` 會被覆蓋 | 全域一次一個並納入 discard（§12） |
| 9 (P2) | 可變性不變量自相矛盾 | 拆成三層（§5） |

### 第二輪（15 條）

| # | 內容 | 處置 |
|---|---|---|
| 1 (P1) | `current` 沒有讀寫契約 | 完整契約（§7） |
| 2 (P1) | 「directory」被當成一個概念 | 錨點表 + `ResolvedPackage`（§4） |
| 3 (P1) | meta 部分狀態沒有合法性矩陣 | 合法狀態表；刪掉 legacy fallback（§6） |
| 4 (P1) | 沒有已提交版本集合 | `origin.json` 兼任提交標記與 lineage（§6、§9） |
| 5 (P1) | discard 是第二個 `current` writer | 納入 `_admit_job`；API 帶 vid（§9、§12） |
| 6 (P1) | 刪除延後判定對不到 inode | **查證屬實**；以版本 identity 判定（§4、§15） |
| 7 (P1) | `.env` 只定義全新安裝 | 補上修訂語意（§3，第三、四輪各再修一次） |
| 8 (P1) | P2 不能單獨出貨 | 合為一個交付單位（§17） |
| 9 (P1) | 「發布後 discard」≠「提交前中止」 | **駁回**，裁決紀錄 #10（§10） |
| 10 (P1) | 遷移沒處理 `.ai_meta.json` 與 state 三態 | 兩者都寫明（§14） |
| 11 (P1) | 遷移二次執行與中斷恢復自相矛盾 | 每套件原子 + 可續跑（§14） |
| 12 (P2) | vid 字典序不是可靠先後 | lineage 寫進 `origin.json`（§9） |
| 13 (P2) | R9-2 依賴未寫出的契約 | 寫成契約（§8、§15） |
| 14 (P2) | §15 對手改作了絕對宣稱 | 限縮並註明後果（§11、§15） |
| 15 (P2) | 隱藏組裝位置沒有契約 | session/build/shell 三個名字（§8） |

### 第三輪（15 條）

| # | 內容 | 處置 |
|---|---|---|
| 1 (P1) | `ResolvedPackage` 沒穿透 mixed-anchor 函式 | 表分成兩張（§4；**第四輪發現仍漏了 `_scan_package`**） |
| 2 (P1) | `.env` 大小閘指到還不存在的 package root | 檢查兩次（§4） |
| 3 (P1) | 「既有 cleanup 自動涵蓋」不成立 | 三個名字，清理針對 session_root（§8） |
| 4 (P1) | `mkdir`+`rename` 兩個 syscall 仍留空殼 | 取消 mkdir 佔名（§8；**第四輪推翻了配套的「空目錄可回收」規則**） |
| 5 (P1) | 共享 `.env` 使 `current` 不再是唯一提交點 | 當時取消修訂出貨 `.env`；**第四輪改成兩層 `.env`**（§3） |
| 6 (P1) | discard 沒重新驗證 P | 跑同一套目標驗證（§9） |
| 7 (P1) | 發布器吞掉持久化失敗 | 回 `(published, durable)`（§7；**第四輪補上型別投影規則**） |
| 8 (P1) | `.stale-` 清掃會繞過新的 delete deferral | 共用同一個判斷（§4；**第四輪發現還有第三個地方**） |
| 9 (P1) | 廣告綁定 V，保護只從呼叫開始 | 撤回無條件保證，明訂接受的例外（§8、§15） |
| 10 (P1) | 常態套件遷移後拿不到提交標記 | 三種狀態都給規則（§14） |
| 11 (P1) | per-package 遷移有無法續跑的空窗 | 確定性兄弟名字（§14；**第四輪補上刪除時機與撞名規則**） |
| 12 (P1) | 刪 legacy fallback 與允許部分遷移互斥 | 預檢 + 全有全無（§6、§14） |
| 13 (P1) | 有了 `current_vid` 卻只給 DELETE 用 | 四個消費端一起改用（§9；**第四輪補上總結 GET**） |
| 14 (P2) | invalid 列與兩種 409 的契約未定義 | 明訂 nullable description 與錯誤碼（§9；**第四輪補上第三種 409**） |
| 15 (P3) | 新 vid 不是「必然空著」 | 碰撞是要檢查的正常失敗（§8） |

### 第四輪（11 條）——全部打在前三輪的修法上

| # | 內容 | 處置 |
|---|---|---|
| 1 (P1) | 單一版本的 discard 分支照文件順序不可達 | **順序錯誤，修正**：`P is null` 先分流，非空才驗證（§9） |
| 2 (P1) | 「共同 all-versions 判斷」漏了第三個刪除端 discard | 三個地方共用；**新增 `.at-stale-<vid>` 落點**（§4、§15） |
| 3 (P1) | 把共用 publisher 改 tuple 會讓 `if not ...` 靜默成功 | 既有 wrapper 在自己邊界投影成 `bool`，只有新函式傳完整結果（§7） |
| 4 (P1) | `current` durable ≠ 它指向的版本 durable | **版本先 durable 才發布 `current`**；`origin` 那列的前提補上（§7、§15） |
| 5 (P1) | mixed-anchor 清單漏了 `_scan_package` 本身 | 列進 mixed-anchor 表，並寫明照字面實作會讓所有工具一次被列成停用（§4） |
| 6 (P1) | 取消 builder `.env` 會遺失只有 builder 知道的設定值 | **推翻第三輪的決定，改成兩層 `.env`**（§3、§18） |
| 7 (P1) | 「空 `<name>/` 一定是我們的殘骸」無法證明且已不可達 | **推翻第三輪的規則**，回到現行「既有名稱一律拒絕」（§6、§8） |
| 8 (P1) | `current_vid` 修了 key，卻沒把總結 GET 綁到同一版本 | 回應帶 vid，不符不寫 cache（§8、§15） |
| 9 (P1) | 遷移 rollback 救不回已完成的套件 | **所有 `at-premigrate` 的刪除延到全部換完之後**（§14） |
| 10 (P2) | 手改造成的第三種 409 沒有前端處置 | 新增 `lineage_unavailable`；`has_previous` 改為「存在且通過驗證」（§9） |
| 11 (P2) | 兄弟名字兼任 journal 卻沒有撞名規則 | 預檢一律拒絕既有的兄弟名字（§14） |

## 17. 交付單位

**P2 與 P3 是同一個不可分割的交付與 review 單位。** P2 先移除定版時，版面仍是扁平的、修訂
仍整包換掉並刪除舊實作，而「丟掉這一版」要到 P3 才存在——中間那個狀態**既不能中止也不能
回復**，比現況差。已查證換裝前那道閘門（`tool_builder.py:2253`）就是中止能力本體。

| 內部順序 | 範圍 |
|---|---|
| 先 | 移除「定版」（後端 + 前端）。純刪除，不動版面 |
| 後 | 版本版面 + 兩層 `.env` + 遷移腳本 + 「丟掉這一版」 + 前端身分改用 `current_vid` |

review 對整段範圍一次進行。

## 18. 剩下的不確定

1. **遷移腳本的初始 `<vid>` 從哪來**：既有套件沒有可靠的建立時間。傾向用一個固定的初始
   vid，反正遷移後每個工具只有一版，lineage 從 `previous: null` 開始就對了。**自由選擇，
   選錯不會逼迫重做。**
2. **前端「丟掉這一版」的確認流程**：`has_previous` 為 false 時等同刪除工具，需要讓你清楚
   知道按下去會發生什麼，又不該每次都跳一樣的重量級警告。**產品選擇，UI review 看得出來。**
3. **兩層 `.env` 的合併規則要不要讓操作者看得見**：套件層蓋過版本層是設計決定，但你在
   `<name>/.env` 設了一個值、而某一版的 `.env` 有同名的不同值時，UI 上看不出來哪個生效。
   傾向在工具詳情裡列出「這一版宣告的 key、以及哪些被你的設定蓋過」——**這是新的不確定，
   取代第三輪那個「怎麼告訴操作者需要哪些 key」的問題**（兩層之後 builder 的值不再被丟棄，
   所以那個問題消失了）。
