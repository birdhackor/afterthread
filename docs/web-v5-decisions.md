# web-v5 裁決紀錄

沿用 web-v4 的體例：`docs/web-v5-plan.md` 寫**打算**做什麼，本文件寫**為什麼最後
不是那樣**、以及實作過程中做了哪些計畫沒寫到的判斷。逐階段接續。

## D41（P1）：`enabled` 移出 `tool.json` → 套件層 `.state.json`

### 這一階段真正在修的東西

一句話：**`tool.json` 的檔案身分同時被當成「規格的身分」和「開關的載體」**。

`package_identity`（`(st_dev, st_ino, st_ctime_ns)` of `tool.json`）是本子系統
回答「這個路徑上還是我剛才看的那一包嗎」的唯一判準，而 `set_enabled` 為了翻一個
布林**就地改寫同一個檔案**——判準跟著位移。於是一次開關，對每一道守衛來說都長得
跟「整包被換掉」一模一樣。web-v4 的 overall review 四次撞到這同一件事的不同面：

| 既有 finding | 它看到的那一面 |
|---|---|
| overall-r5 O5-1 | 執行登記表以 manifest 身分為 key，一次開關讓正在跑的子行程從表上消失，delete／promote 判定「沒人在跑」然後把檔案毀掉 |
| overall-r7 O7-2 | 開關與同步 regenerate 必須互鎖，否則一整趟 LLM 往返之後換到 404 |
| overall-r8 O8-1 | 落在總結往返裡的一次開關讓側檔寫入被拒，安裝唯一的 origin 副本永久遺失 |
| 裁決紀錄 #8（REJECTED） | 同上情境下的失敗分支：placeholder 也被拒，那次失敗 session 的日誌連結沒落盤 |

**量測（本機 ext4，非推論）**：改動後 `set_enabled` 連續三次（False／True／False）

```
package_identity before: (64770, 543698, 1785167009937732790)
set_enabled(False)=True  manifest bytes identical=True  identity unchanged=True
set_enabled(True) =True  manifest bytes identical=True  identity unchanged=True
set_enabled(False)=True  manifest bytes identical=True  identity unchanged=True
package_identity after : (64770, 543698, 1785167009937732790)
```

**裁決紀錄 #8 因此 moot**：它被駁回的理由是「要讓那次 placeholder 寫入通過，就得
改用目錄身分回答『還是同一個套件嗎』，而 D40 r5 已經證明那個問題不能用目錄身分
回答」。現在那個窗口的**觸發條件**（開關會位移 manifest 身分）不存在了，所以既不
需要放寬守衛、也不需要換身分。#8 描述的另一半——「總結生成失敗」——照舊會留下
`llm_log_id` 為 null 以外的正常結果，因為守衛不再拒絕它。**唯一還在的殘留**是
一次真正的「刪除後同名重裝」落在那趟往返裡，那正是 manifest 身分**應該**拒絕的
情況，D40 r5 的裁決原封不動。

### 讀取優先序與遷移（R1）

`.state.json` 在且讀得出來 ⇒ 權威。**不存在** ⇒ 退回 `tool.json` 既有的可選
`enabled`（預設 true，與改動前 `_scan_package` 完全一致）。

**那個 fallback 就是遷移的全部**：沒有啟動時的掃描改寫，讀取路徑永遠不寫入
（測試 `test_a_package_with_no_state_file_falls_back_to_its_manifest` 直接釘住
「掃完之後兩個套件都還是沒有狀態檔」）。第一次切換才產生 `.state.json`。

**刻意不刪 manifest 裡那個已成 legacy 的欄位**：為了整潔改寫 manifest，正好會
移動這次改動要固定住的那個身分——這是本階段唯一不能犯的錯。所以兩個檔案會長期
不一致，而優先序就是用來讀它的（`test_the_state_file_wins_over_a_manifest_that_disagrees`
兩個方向都釘）。

### 壞掉的狀態檔：列成無效 + 停用（R2）

**選項**：(a) 只當成停用；(b) 列成 invalid 並在 `error` 欄說明。**選 (b)，並且
同時讓 `enabled=False`。**

理由：

- **`error` 欄的用途就是這個**。列表的 `error` 一直是「這一包為什麼不可執行、
  類別是什麼」的頻道。而 R4 之後**執行前會讀這個檔案**——一個答不出「可不可以
  跑」的套件，就是 `valid` 這個欄位在講的那種套件，不是一個外觀正常但安靜消失的
  工具。只當成停用會讓操作者看到工具無故關閉、沒有任何解釋。
- **「不跑」勝過「洩漏」**。`enabled_llm_tools` 濾的是 `valid AND enabled`，兩邊
  都說不，代表未來任何一次只動其中一個濾條的重構都不會把它悄悄放回模型面前。
- **absent 與 unreadable 用不同機制分辨**。`_read_regular_file_capped` 把「檔案
  不存在」與「拒絕讀取」折成同一個 None，所以讀取器先自己 `lstat`：只有
  `FileNotFoundError` 算不存在，其他 `OSError` 是「看不到」而不是「沒有東西」
  ——這條規則直接沿用 D40 P3b r2-3 對 `.env` 來源做過的同一個裁決。
- **`{}` 與 `{"enabled": "yes"}` 也算讀不出來**：它們對「操作者的意圖」的沉默
  程度和一個被截斷的檔案完全一樣。

**修法有兩條，寫進 README 與 tool-calling.md**：再按一次開關（`set_enabled`
不讀這個檔案，直接覆蓋成乾淨的一份），或自己刪掉該檔案退回 fallback。**誠實的
殘留**：前端的開關在 `valid=false` 的列上是停用的（既有行為，本階段不動前端），
所以 UI 上只剩「自己刪檔」這條——而手改套件檔案是 D21 明文支援的行為。API 兩條
都通。

### 寫入：共用發佈紀律，且**不帶** `expected_identity`（R3）

**generalize，不是加一個 sibling**。`_write_sidecar_atomic` 改名為
`_write_package_file_atomic(directory, filename, data, expected_identity)`。它裡面
是一百行分別裁決過的細節（R7-3 的權限保留、R11 的 owner 下限、lstat 拒絕
symlink／非一般檔、fsync、暫存檔清理、R6-2／R6-3 的最後一刻身分比對）；複製一份
用手維持同步，正是本模組在「一邊寫、另一邊讀」的每個地方都反對的漂移。暫存檔前綴
改由 `filename` 導出，所以 `.state.json.<x>.tmp` 自動落在同一個保留名域裡。

**不帶 `expected_identity`**，理由三條：

1. **唯一拿得到的身分就是 manifest 的**，而把開關重新綁回 manifest 正是本階段要
   移除的東西。一個「`tool.json` 動了就拒絕開關」的守衛，等於把開關放回它剛被
   搬走的那個檔案上。
2. **側檔的守衛存在是因為它的內容是「為某一包量身組出來的」**：A 的總結與 A 的
   origin 絕不能落進搶走名字的 B。這裡的內容是**兩個值**，按**名稱**定址，在
   resolve 之後幾個 syscall 就寫完，中間沒有 LLM 往返。`delete_tool`——另一個
   by-name 的變動入口，而且破壞性大得多——同樣不主張任何身分。
3. **殘留寫明**：若在那幾個 syscall 內套件被刪除、另一包以同名裝進來，這次開關
   會落在新那一包上。代價是一個操作者在清單上看得見、一鍵可翻回來的布林；不是
   側檔那種「A 的文字與不可重生的 origin 永久錯置進 B」。

**`set_enabled` 退掉的三道拒絕**（寫明，以免被讀成疏漏）：`tool.json` 讀不出來、
過大、以及「pretty 化之後會超過上限」。三道守的都是**對那個檔案的寫入**，而那個
寫入已經不存在。留下來的行為嚴格更好：操作者現在關得掉一個 manifest 壞掉的套件
（那正是最想關掉它的時候），而那種套件本來就列成無效、本來就不會被端給模型。
**寫入邊界的 containment 重驗**同理移除，保證沒有跟著移除：發佈器自己的 pre-write
`lstat` 拒絕任何非一般檔（含 symlink），而 `os.replace` 換掉的是**連結本身**，
從不寫穿它。這一條由 `test_set_enabled_refuses_a_symlinked_state_file` 直接釘。

### 執行時的 `enabled` 檢查（R4）——把一個意外變成明講

**這是本階段最容易漏掉的一條。** 改動前，「對話中途被停用的工具不會跑」是**副作用**：
開關位移了 manifest 身分，`_make_handler` 的身分守衛抓到漂移就拒絕——理由是錯的，
但它確實拒絕了。身分不再位移之後，那個意外消失，一個被停用的工具會直接**跑起來**
——那正是 overall-r7 O7-1 的情境 (a) 原樣復活。

所以 handler 在執行當下**自己讀 `.state.json`** 並拒絕，而且用**自己的字串**：

```
_TOOL_DISABLED_RESULT = "tool not run: this tool was disabled after it was offered"
```

沿用 `_TOOL_REPLACED_RESULT`（「這個工具的套件在被端出去之後改變了」）會**講一件
不成立的事**：套件一個位元組都沒變，那句話會把模型導去重讀一份根本沒動過的規格。

**兩道檢查都留著，順序是身分在前**，而順序本身有兩個作用：

- 被**換掉**的套件要回報「換掉了」，而不是去讀它繼受者的狀態檔；
- 更重要的：身分檢查剛剛證明了 `tool.json` **沒有動過**，所以「狀態檔不存在」
  可證地仍然等於 manifest 當初廣告時說的那個值——對一個已被廣告的工具而言就是
  **啟用**。因此 handler **不需要**在這裡重讀 manifest：身分守衛正是讓「只讀
  `.state.json`」成為完整答案的那個前提。

**這道檢查要放兩個地方，不是一個**（最容易只做一半的地方）。舊的那個「意外」是由
**`Popen` 前一行**的身分檢查提供的，涵蓋的窗口因此包含 handler 檢查與 `Popen`
之間那一段——`.env` 讀取、參數序列化、以及長度無上限的 threadpool 排隊等待，正是
D40 R6-1 指名「這不是本模組接受的那種瞬間」的那個窗口。只補 handler 那一道會**悄悄
縮小**既有保證：落在排隊等待裡的一次開關，工具照樣會啟動。所以
`_run_tool_subprocess` 在 `_still_the_expected_package` 的**下一行**也重讀一次，
與它守的那個動作貼在一起（同一條規則、同一個理由）。ABSENT 在那裡同樣不需要重讀
manifest，因為上一行剛證明 manifest 沒動。

**可證的量測（把檢查拿掉，當場失敗）**：

- 兩道都拿掉 → `test_runtime_refuses_a_tool_disabled_after_it_was_offered` 與
  `test_a_toggle_during_the_scan_cannot_advertise_the_tool_it_disabled` 都拿到
  `'ok'`——**被停用的工具真的跑了**，即 O7-1 情境 (a) 原樣復活；
- 只拿掉 `Popen` 那一道 →
  `test_a_toggle_landing_while_a_call_prepares_still_refuses_before_popen`
  拿到 `'ok'`（該測試從 `_build_tool_env` 內部驅動開關，就是那個窗口本身）。

### 保留名域變成集合（R5），以及它逼出來的一個**新**步驟

`tools._RESERVED_PACKAGE_FILENAMES = (_AI_META_FILENAME, _STATE_FILENAME)`，
`tool_builder._is_reserved_sidecar_name` 改成走這個 tuple（每個名字仍比對
「完全相同」或「`<名稱>` 前綴 + `.tmp` 後綴」，仍 casefold）。`_strip_builder_sidecars`
與 `_revise_copy_ignore` 都只呼叫那個判別式，所以兩處自動涵蓋。

**函式名保留 `_is_reserved_sidecar_name`**（即使現在涵蓋兩個檔案）：與
`_STALE_BACKUP_RE` 保留 "backup" 字眼同一條理由——D40 的附錄指名這些符號，改名
會讓那些裁決指向不存在的名字，而真正的契約是磁碟上的名字。

**排除之後必須補回來，否則會生出一個新缺陷**（計畫文件沒寫到這一步）：修訂的
換裝是**整包替換**目錄，所以把 `.state.json` 排除在複製之外、然後什麼都不做的話，
**每一次修訂都會發佈一個沒有狀態檔的套件**——退回 manifest fallback 讀成「啟用」，
一次關於分頁的修訂靜默打開了操作者刻意關掉的工具。

裁決：`_promote_staging_replace` 在 `_preserve_env_file` 旁邊，**從正式套件讀出
當下的有效啟用狀態、用同一個原子寫法寫進 staging**。與 `.env` 的保留同一個形狀、
同一個理由（後端自己的位元組疊在一個已通過 `validate_package` 的套件上），並且
**寫不進去就拒絕換裝**（`_ERROR_REVISE_STATE_RESTORE`：「無法保留工具的啟用狀態，
修訂已取消。」）——所有動作都落在 staging，拒絕的代價只是一次被放棄的建置，而
反方向的代價是發佈一個會自己開起來的工具。讀的是 `package_enabled(target)`，
所以**壞掉的狀態檔被帶成停用**而不是被「修好成啟用」，與掃描同方向。

### 兩個身分的拆分**保留**（R7）

R5（overall）當初拆開 `directory_identity` 與 `package_identity`，是因為開關會
位移 manifest 身分。那個動機現在沒了——但**拆分本身的理由還在**：操作者手改
`tool.json` 是 D21 明文支援的行為，而它在子行程仍在該目錄裡跑的時候，會用完全
一樣的方式位移 manifest 身分。所以程式碼一行不動，只改敘述。

把兩者合回一個 tuple 是**後續階段**的判斷（等不可變版本出現、「還是同一包嗎」
不再需要 stat 才有意義），在這裡做會把兩件事纏在一起。

### 測試名稱的變更（給後續 review 的對照表）

D40 的附錄指名了幾個以「enabled toggle」命名的測試，它們釘的性質仍在，只是**驅動
它的那個寫入者換人**（開關不再改寫 manifest，改由手改 manifest 驅動）：

| 舊名 | 新名 |
|---|---|
| `test_runtime_refuses_after_the_enabled_toggle_rewrites_the_manifest` | `test_runtime_refuses_after_a_hand_edit_rewrites_the_manifest`（＋新增 `test_runtime_refuses_a_tool_disabled_after_it_was_offered`、`test_an_off_then_on_toggle_no_longer_refuses_the_rest_of_the_conversation`、`test_a_toggle_landing_while_a_call_prepares_still_refuses_before_popen`） |
| `test_delete_after_an_enabled_toggle_still_defers_a_running_call` | `test_delete_after_a_manifest_edit_still_defers_a_running_call`（＋新增 `test_a_toggle_mid_call_moves_neither_identity`） |
| `test_promote_replace_defers_after_an_enabled_toggle_moved_the_manifest` | `test_promote_replace_defers_after_a_manifest_edit_moved_the_identity` |
| `test_an_enabled_toggle_during_the_generation_costs_the_summary_not_the_origin` | `test_an_enabled_toggle_during_the_generation_now_costs_nothing_at_all`（斷言反轉：總結**與** origin 都留下） |

**測試輔助函式 `_edit_manifest_in_place`（兩份測試檔各一份）**：本機 ext4 的
`st_ctime_ns` 實測**約 1 ms 粒度**（50 ms 內 292 次改寫只產生 50 個相異值），所以
一次落在同一毫秒內的改寫**不會**位移身分。輔助函式重試到 tick 前進為止，讓每一個
「這次編輯位移了身分」的測試變成確定性的，而不是取決於上面幾行剛好花了多久。

### 一個誠實的成本

`_scan_package` 現在每包多一次小檔讀取——`list_tools` 每次、以及**每一次 AI 請求**
的 `enabled_llm_tools` 每次。這是把可變狀態搬出 manifest 的固有代價（權威來源就
是那個檔案），並非可以最佳化掉的東西；`_STATE_MAX_BYTES` 因此訂得比 manifest 還
緊（4 KiB）：讀得一樣頻繁，而承載的只有一個布林。

### D41 附錄（P1 review r1）：P1 自己拆掉的那道意外守衛，要用一把鎖補回來

三條 finding，第一條是 **P1 引進的缺陷**，另兩條是它旁邊被一起看見的舊帳。

#### R1-1：修訂會靜默還原一次成功的開關（P2）

`_promote_staging_replace` 原本在 `_preserve_env_file` 旁邊就把正式套件的啟用狀態
讀出來、寫進 staging，註解還寫著這一步「位置是自由的」（因為它有界又便宜）。**那句
話就是缺陷**：讀完之後還排著 定版 重檢、`summary_status_or_unknown` 的側檔讀取、
一整趟 `_scan_package`、以及兩個 rename——這段期間落地的任何一次
`PATCH /api/tools/{name}` 都會被 staging 裡那份舊值蓋回去，而且**兩邊都回報成功**。

**這個窗口在 P1 之前是被意外蓋住的**：開關會改寫 `tool.json`、位移 manifest 身分，
換裝前的身分重檢因此拒絕。P1 讓身分不再位移（那正是它存在的目的），意外消失，**沒有
任何東西接手**——重檢照樣通過，換裝照樣把舊布林出貨，操作者剛關掉的工具被重新端給
模型。

裁決分兩半，第二半才是真正關上它的：

1. **讀取搬到能搬的最後一刻**：`carry_package_state` 現在是 tail 的第一句，排在
   定版 閘與 origin 讀取**之後**、身分重檢**之前**。身分重檢仍然是第一個 rename
   前的最後一道（D40 P3b r12 的裁決不動，且新增測試從 carry 內部置換套件來釘住
   這個相對順序）。
2. **光靠順序關不掉**（讀取與換裝之間永遠至少還有一次寫入），所以引入
   `tools._STATE_PUBLISH_LOCK`，讓 `set_enabled` 的發佈與 promote 的
   `[讀取 → 寫入 staging → 身分重檢 → 兩個 rename]` **互斥**。

**為什麼這裡可以鎖，而 D40 overall r8 當初拒絕鎖開關**：同一個問題問在兩個不同長度
的窗口上。r8 要鎖住的是**一整趟 LLM 往返**（以分鐘計、操作者看得見），所以裁決是把
救不回來的那一半提前落盤、不動路由；這裡鎖住的是一次小寫入、一次 lstat 與兩個
rename，全部以毫秒計，而且鎖內**沒有 await、沒有子行程、不取任何其他鎖**（唯一的
巢狀風險已排除：`write_package_state` / `carry_package_state` /
`_write_package_file_atomic` 全部不取鎖，都是在持有中被呼叫的）。備份的 `rmtree`／
延後改名**刻意留在鎖外**——那不是「有界的一瞬間」，而且到那時修訂已經生效，落在那裡
的開關會正確地落在新套件上。

**它蓋不到什麼，寫明**：這是**行程內**的鎖，與 `_META_LOCK` 同一個等級。第二個
afterthread 行程共用同一個 tools 目錄、或操作者手改 `.state.json`（D21 明文支援），
都不受它序列化——本 app 就是**單一行程**（console script 同時服務 API 與 UI），
它唯一的並行來源是每條路由都會跳進去的 threadpool，而那正是這把鎖涵蓋的東西。

**一個張眼睛做的取捨**：啟用 carry 現在排在 定版 閘與第一個 rename **之間**，所以
一次落在那次小寫入＋`fsync` 裡的 定版 會被漏掉——就跟一直以來落在身分 lstat 裡的
那一次一樣。換到的是開關那個窗口**歸零**（原本橫跨一整趟掃描、側檔讀取與一道閘）。
兩邊不對稱才是理由：定版 是操作者的動作落在**我們自己有界的一次寫入**裡（既有已接受
的那一類，只是多一步），而開關是被一個操作者根本不會聯想到的操作**每一次都**還原掉，
窗口還是 LLM session 的殘餘工作撐出來的。這條與 D40 P3b r4「定版 閘要排到最後」不
牴觸：r4 拒絕的是**外部決定長度**的步驟（操作者選的 `.env` 大小），carry 的長度由
`_MANIFEST_MAX_BYTES`／`_STATE_MAX_BYTES` 與一次 20 位元組的寫入決定。

**這是過渡性的，而且程式碼裡就寫著**（`_STATE_PUBLISH_LOCK` 與
`carry_package_state` 的註解都標了 TRANSITIONAL）：web-v5 的**目標版面**把
`.state.json` 放在 `<name>/`、只換 `<name>/versions/<vid>/`，修訂根本不再碰開關的
檔案，這整個危險在 P2 就不存在。**P2 要做的是刪掉它們，不是繼承它們**——一把活得比
理由久的鎖，就是下一個 reviewer 的謎題。

**可證的量測（把修法拿掉，當場失敗）**：

- 把 carry 搬回 `.env` 複製旁邊 →
  `test_a_toggle_that_lands_before_the_swap_is_carried_across_not_reverted` 拿到
  `{'enabled': True}`（期望 `False`）——**剛被關掉的工具真的被重新打開**；
- 把 `set_enabled` 的鎖拿掉 →
  `test_set_enabled_publishes_under_the_state_publish_lock` 的探針回 `[False]`，
  而 `test_a_toggle_arriving_during_the_swap_waits_for_it_and_still_wins` 裡那條
  `PATCH` 直接撞進「套件已被改名到 backup」的瞬間、回 `False`（路由 404）。

#### R1-2：README 寫的修法有一半根本不成立（P2）

`_read_enabled_state` 把 symlink／FIFO／目錄與截斷／非 JSON／非布林一律列為
unreadable，而 README 把六種情況列在一起、然後說「再按一次開關就好，PATCH 不讀這個
檔案，直接覆蓋成乾淨的一份」。**對一般檔案是真的，對非一般檔案是假的**：
`_write_package_file_atomic` 的 pre-write `lstat` 一律拒絕非一般檔，所以 PATCH 失敗、
路由回 404，那個 FIFO 原封不動地留在那裡。工具被正確地列成無效並停用，但**宣稱的修法
不是修法**。

**裁決：改文件，不改寫入邊界。** 那道拒絕是刻意的寫入邊界性質（`os.replace` 換掉的是
連結本身、從不寫穿它），而 `_remove_reserved_sidecar_path` 的「什麼都 unlink」只適用
於 **staging 裡的 builder 產出**，不適用於正式套件。README 因此把兩類拆開：一般檔案
壞掉 ⇒ 按開關即可修好；非一般檔案 ⇒ 只能自己動手移除該項目（操作者按定義有 shell，
D21）。

**沒有實作、留作建議**：可以在發佈器之外加一條「先 unlink 再 publish」的修復路徑
（例如 `set_enabled` 在確認目標是非一般檔時先移除它）。它會讓 UI 上的開關也能修好
這種套件，代價是把「絕不動非一般檔」這條寫入邊界規則開一個口——那應該是它自己的一次
裁決，不是這一輪順手做掉的事。

#### R1-3：修訂會靜默收窄狀態檔的權限（P3）

發佈器保留既有檔案的低 9 位權限（R7-3／R11 的紀律），但**它是從寫入目標那裡繼承**
的，而修訂寫進的是 staging——那裡按構造沒有 `.state.json`，於是每一次不相干的修訂
都把操作者設的 `0640`（讓同群組的行程讀得到）收窄回預設值。同一個理由讓
README「PATCH 是這個檔案唯一的寫入者」那句話也不成立：修訂的發佈路徑也寫它。

**裁決：沿用同一個機制，不發明第二個。** `_write_package_file_atomic` 多一個
keyword-only 的 `default_mode`（「第一次寫入要用的權限」，正規化規則與繼承來的完全
一樣：低 9 位 OR `_OWNER_RW`），`carry_package_state` 讀正式套件那個檔案的權限、
往下傳。**目標端已經有檔案時仍然是繼承贏**——真的在那裡的東西勝過呼叫端對「本來會
是什麼」的猜測。**正式套件沒有狀態檔、或那裡不是一般檔**時沒有權限可繼承，維持今天
的預設值（`_OWNER_RW`）。

**可證的量測**：把 `default_mode` 的傳遞拿掉 →
`test_a_revise_carries_the_state_files_mode_not_just_its_value[operator-set]`
量到 `0o600`（期望 `0o640`）。
