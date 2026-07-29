# web-v5 裁決紀錄

沿用 web-v4 的體例：`docs/web-v5-plan.md` 寫**打算**做什麼，本文件寫**為什麼最後
不是那樣**、以及實作過程中做了哪些計畫沒寫到的判斷。逐階段接續。

## D41（P1）：`enabled` 移出 `tool.json` → 套件層 `.afterthread-state.json`

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

`.afterthread-state.json` 在且讀得出來 ⇒ 權威。**不存在** ⇒ 退回 `tool.json` 既有的可選
`enabled`（預設 true，與改動前 `_scan_package` 完全一致）。

**那個 fallback 就是遷移的全部**：沒有啟動時的掃描改寫，讀取路徑永遠不寫入
（測試 `test_a_package_with_no_state_file_falls_back_to_its_manifest` 直接釘住
「掃完之後兩個套件都還是沒有狀態檔」）。第一次切換才產生 `.afterthread-state.json`。

> **r5 更正（見下方 r5 附錄 R5-1／R5-3）**：這一節有三處要改。**一**、檔名是
> `.afterthread-state.json`，而「在且讀得出來」要再加一個條件——**帶著所有權標記**；
> 讀得出來但沒有標記的檔案**不是我們的**，答案與「不存在」完全相同。**二**、
> 「那個 fallback 就是遷移的全部」對**絕大多數**既有安裝成立，但對一個**本來就有
> 同名檔案**的套件不成立：那一包的開關從此按不動（`PATCH` 回 404），要操作者自己
> 把檔案改名或加上標記。原本記在計畫與 `tool-calling.md` 的「舊安裝不用做任何事」
> 因此是錯的，兩份都已更正。**三**、`_promote_staging` 現在會在搬檔前**主動寫一份
> 初始狀態**，所以「第一次切換才產生」只對 r5 之前安裝的套件成立；新安裝從第一天
> 就有這個檔案，fallback 因此收斂成它本來的意思——「web-v5 P1 之前裝的」。

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

> **r5 更正（見下方 r5 附錄 R5-1）**：最後那一項的**例子**換人了，規則本身沒變。
> 判準現在是「這是不是**我們的**檔案」：`{}` 與 `{"enabled": "yes"}` 都沒有所有權
> 標記，所以它們現在是 **FOREIGN**（當成沒有狀態檔，退回 manifest），不是
> unreadable。這一節講的 unreadable 收斂成兩種——**完全讀不到**（非一般檔、拒絕
> 讀取、超過上限），以及**帶著標記但 `enabled` 不是布林**。後者就是原本那句話講的
> 那個性質，只是主詞從「任何一個在那裡的檔案」換成「我們自己的那個檔案」。

**修法有兩條，寫進 README 與 tool-calling.md**：再按一次開關（`set_enabled`
不讀這個檔案，直接覆蓋成乾淨的一份），或自己刪掉該檔案退回 fallback。**誠實的
殘留**：前端的開關在 `valid=false` 的列上是停用的（既有行為；r2 動了前端那幾個閘，
但這一個**刻意留著**，見 r2 附錄的「留下的」），所以 UI 上只剩「自己刪檔」這條
——而手改套件檔案是 D21 明文支援的行為。API 兩條都通。

### 寫入：共用發佈紀律，且**不帶** `expected_identity`（R3）

**generalize，不是加一個 sibling**。`_write_sidecar_atomic` 改名為
`_write_package_file_atomic(directory, filename, data, expected_identity)`。它裡面
是一百行分別裁決過的細節（R7-3 的權限保留、R11 的 owner 下限、lstat 拒絕
symlink／非一般檔、fsync、暫存檔清理、R6-2／R6-3 的最後一刻身分比對）；複製一份
用手維持同步，正是本模組在「一邊寫、另一邊讀」的每個地方都反對的漂移。暫存檔前綴
改由 `filename` 導出，所以 `.afterthread-state.json.<x>.tmp` 自動落在同一個保留名域裡。

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

> **r3 更正（見下方 r3 附錄 R3-2）**：上面這段對「保證沒有跟著移除」的敘述是
> **錯的**。發佈器的三個動作只管**最後一段**：`lstat` 不跟隨最後一段，但
> `mkstemp(dir=…)` 與 `os.replace` 跟隨**每一層上層目錄**。而 `set_enabled`
> 在**取鎖之前**就解析完路徑，解析出來的又只是一個會被重新詮釋的字串。這道
> 重驗因此在 r3 被放回鎖內、發佈前一行。

### 執行時的 `enabled` 檢查（R4）——把一個意外變成明講

**這是本階段最容易漏掉的一條。** 改動前，「對話中途被停用的工具不會跑」是**副作用**：
開關位移了 manifest 身分，`_make_handler` 的身分守衛抓到漂移就拒絕——理由是錯的，
但它確實拒絕了。身分不再位移之後，那個意外消失，一個被停用的工具會直接**跑起來**
——那正是 overall-r7 O7-1 的情境 (a) 原樣復活。

所以 handler 在執行當下**自己讀 `.afterthread-state.json`** 並拒絕，而且用**自己的字串**：

```
_TOOL_DISABLED_RESULT = "tool not run: this tool was disabled after it was offered"
```

沿用 `_TOOL_REPLACED_RESULT`（「這個工具的套件在被端出去之後改變了」）會**講一件
不成立的事**：套件一個位元組都沒變，那句話會把模型導去重讀一份根本沒動過的規格。

**兩道檢查都留著，順序是身分在前**，而順序本身有兩個作用：

> **r4 更正（見下方 r4 附錄 R4-1）**：「順序是身分在前」在 r4 之後**只對 handler
> 那一處成立**。`Popen` 前一行是反過來的（開關在前、身分緊貼 `Popen`），因為那個
> 位置只有一個名額，而**改道**比「晚一個 lstat 才看到開關」嚴重。下面第二個項目
> 符號的推論另外早在 r2（R2-1）就被推翻了。

- 被**換掉**的套件要回報「換掉了」，而不是去讀它繼受者的狀態檔；
- 更重要的：身分檢查剛剛證明了 `tool.json` **沒有動過**，所以「狀態檔不存在」
  可證地仍然等於 manifest 當初廣告時說的那個值——對一個已被廣告的工具而言就是
  **啟用**。因此 handler **不需要**在這裡重讀 manifest：身分守衛正是讓「只讀
  `.afterthread-state.json`」成為完整答案的那個前提。

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

> **r5 更正（見下方 r5 附錄 R5-2）**：「兩處自動涵蓋」正確，但它涵蓋的**深度**是
> 錯的。`_STATE_FILENAME` 現在**只保留套件根目錄**那一個，判別式因此多一個必填的
> keyword-only `at_root`，並改由兩個 tuple 決定：根目錄看
> `_RESERVED_PACKAGE_FILENAMES`，其他層級看 `_RESERVED_AT_EVERY_DEPTH`（只有
> `_AI_META_FILENAME`）。理由就是 `_revise_copy_ignore` 自己對巢狀 `.env` 做過的
> 那條裁決（R2-2）。

**函式名保留 `_is_reserved_sidecar_name`**（即使現在涵蓋兩個檔案）：與
`_STALE_BACKUP_RE` 保留 "backup" 字眼同一條理由——D40 的附錄指名這些符號，改名
會讓那些裁決指向不存在的名字，而真正的契約是磁碟上的名字。

**排除之後必須補回來，否則會生出一個新缺陷**（計畫文件沒寫到這一步）：修訂的
換裝是**整包替換**目錄，所以把 `.afterthread-state.json` 排除在複製之外、然後什麼都不做的話，
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
afterthread 行程共用同一個 tools 目錄、或操作者手改 `.afterthread-state.json`（D21 明文支援），
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
`.afterthread-state.json` 放在 `<name>/`、只換 `<name>/versions/<vid>/`，修訂根本不再碰開關的
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
的，而修訂寫進的是 staging——那裡按構造沒有 `.afterthread-state.json`，於是每一次不相干的修訂
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

### D41 附錄（P1 review r2）：執行前的檢查問完整的優先序，前端的耦合閘隨根因退場

兩條 finding 指的方向相反，而且**必須**相反：一條說後端的檢查太寬鬆（放行了一個
有效狀態是停用的工具），另一條說前端太嚴格（擋掉一個已經安全的動作）。兩者的共同
根因是同一件事被修掉之後**沒有把所有依賴它的推理重新算過**：R4 的檢查繼承了一個
錯的前提，前端的閘則繼承了一個已經消失的危害。

#### R2-1：ABSENT 是一個**答案**，不是沉默（P2）

兩處 R4 檢查（handler 進入時、`Popen` 前一行）都只寫成
`state.present and not state.enabled`，理由寫在註解裡：「上一行的身分檢查剛證明
`tool.json` 沒動過，所以沒有狀態檔的套件仍然說著它被廣告當時說的那句話，而被廣告
的工具就是啟用的。」

**那句推論自己就寫著它為什麼錯**：那個套件被廣告的時候**並不是**「沒有狀態檔」
——它有一個寫著 true 的狀態檔，而那個檔案**後來被刪掉了**。身分檢查證明的是
`tool.json` 沒動；它不看 `.afterthread-state.json`，也就對那個檔案被移除一事完全沒有發言權。

**具體序列，每一步都是本階段文件自己教的操作**：一個 P1 之前安裝、`tool.json` 的
legacy 欄位寫著 `enabled: false` 的套件 → 一次 `PATCH` 把它打開（產生 `.afterthread-state.json`）
→ 工具被廣告、handler 建好 → 操作者刪掉 `.afterthread-state.json`（那正是 R1-2 寫進兩份 README
的修法之一，也是 D21 明文支援的手改）→ 依 R1 的優先序，有效狀態回到 manifest 的
`false`，但兩處檢查看到 ABSENT 就放行、子行程照樣啟動。這是一個**穩定狀態**，不是
check-then-act 的一瞬間：那個對話剩下的每一次呼叫都會跑。

**裁決：兩處都改問 `package_enabled`**——那個 R1 為此存在的單一前門，也就是掃描
用來回答清單與廣告的同一個答案。**不採**「狀態檔不存在時自己補讀一次 manifest」的
省事路徑：那會是這條優先序的**第二份拼法**，而寫在這兩個函式裡的正是「一份拼法只
做了一半」造成的缺陷。錯的那段推理註解一併刪掉——留著一段被推翻的理由，比留著一個
錯的檢查更難修。

> **r3 更正（見下方 r3 附錄 R3-1）**：上面把選項寫成「呼叫 `package_enabled`
> （＝一整趟掃描）或寫第二份拼法」是一個**假二分**，而且選了貴的那個——`package_enabled`
> 當時就是 `_scan_package(...).enabled`，它**很早**就讀了狀態檔，然後才去讀 manifest、
> 解析 entry、stat 它，所以 `Popen` 前一行用的那個值已經是 0.5 ms 與十幾次檔案操作
> 之前的東西，落在那段裡的 `PATCH` 照樣出貨。第三個選項才是對的：把**優先序本身**
> 抽成一個小函式（`_effective_enabled`），掃描與兩處檢查都呼叫它——一份拼法保住了，
> 而執行前那道檢查貼回它守的動作旁邊。

**三態對照（每一個消費者，包含本來就對的）**：

| 消費者 | 怎麼問 | ABSENT | PRESENT 可讀 | PRESENT 讀不出 |
|---|---|---|---|---|
| `_scan_package`（優先序的**定義**） | ~~inline~~ → `_effective_enabled`（r3；它還需要 `error`，兩份讀取自己已經在手上） | manifest legacy 欄位（預設 true） | 檔案說的值 | false ＋ 列成無效 |
| `list_tools` | `scan.enabled` | 同上 | 同上 | 同上 |
| `enabled_llm_tools` | `scan.valid and scan.enabled` | 同上 | 同上 | 同上（兩個濾條都說不） |
| `_make_handler`（呼叫進入） | `package_enabled` ← **本輪改** | 同上 | 同上 | 同上 |
| `_run_tool_subprocess`（`Popen` 前一行） | `package_enabled` ← **本輪改** | 同上 | 同上 | 同上 |
| `tools.carry_package_state`（修訂換裝） | `package_enabled` | 同上 | 同上 | 同上（帶成停用） |
| `PATCH /api/tools/{name}` → `set_enabled` | **寫入者，不讀** | — | — | — |
| 前端 `Switch` 的 `checked` | `GET /api/tools` 的 `enabled` 欄 | 同掃描 | 同掃描 | 同掃描（該列另外因無效而停用開關） |

**執行路徑的代價（實測，非估算；本機 ext4、從未切換過的套件＝常見情形）**：

```
_read_enabled_state（舊檢查，ABSENT）     11.93 us
package_enabled    （新檢查，ABSENT）    526.38 us
_read_enabled_state（舊檢查，PRESENT）    87.50 us
package_enabled    （新檢查，PRESENT）   579.51 us
一次完整工具呼叫（含子行程）              40.40 ms
```

一處多約 514 us，兩處合計約 1.03 ms，佔一次最小工具呼叫的 **2.5%**。cProfile 顯示
成本的大宗不是那兩次讀檔，而是 `_entry_file_exists` 的 realpath／containment 與每趟
掃描約 38 次 `lstat`。**接受**：執行路徑現在付的，就是廣告同一個工具本來就付過的
那筆；換到的是清單、廣告與執行三處**證得出來**在回答同一個問題。

> **r3 更正**：這筆代價被接受的理由只講了**慢**，漏了真正的問題——那 514 us
> **落在讀完開關之後**。r3 抽出優先序之後新舊並排重測：ABSENT 443 → 95 us、
> PRESENT 530 → 65 us、PRESENT 讀不出 77 → 70 us（本節舊數字取自另一次量測，
> 絕對值受機器雜訊影響，量級一致）。新數字與方法見 r3 附錄。

**可證的量測（把修法換回舊寫法，當場失敗）**：以 `state.present and not
state.enabled` 冒充 `package_enabled` 重跑上面那個序列 → 回 `'ok'`、且子行程真的寫
出了 sentinel（**被停用的工具跑了**）；新寫法回 `_TOOL_DISABLED_RESULT`、sentinel
不存在。

#### R2-2：前端還在擋一個**已經不會發生**的失敗（P2）

`ToolsPage` 的三個閘全部由同一句話背書，而註解到今天還在講那句話：「一次開關會就地
改寫 `tool.json`，把 manifest 身分移走。」P1 之後那句話是假的。

**先驗證每一個窗口真的被接住了，再刪**（讀後端，不是照 finding 的說法）：

- **修訂被開關弄死**：換裝前的重檢是 `tool_builder.py:2274`
  （`_package_identity(target) != package_identity`），而 `_package_identity` 就是
  `tools.package_identity`＝`tool.json` 的 `(dev, ino, ctime)`；`set_enabled`
  （`tools.py:3672`↓）從頭到尾沒有打開 `tool.json`，只經 `write_package_state`
  發佈 `.afterthread-state.json`。身分不可能被切換推走。
- **開關被修訂還原**：`tools.carry_package_state(target, staging)` 是鎖內 tail 的
  第一句（`tool_builder.py:2254`），讀的是**正式套件**當下的值；落在它之前的切換
  被帶過去。落在 tail 裡的切換擋在 `tools._STATE_PUBLISH_LOCK`
  （`tools.py:3743`）外面，而 `set_enabled` 在**取鎖之前**就解析完名稱
  （`tools.py:3728`），所以它醒來時寫的是「現在叫這個名字的那一包」＝剛發佈的新
  套件。備份清理刻意在鎖外（`tool_builder.py:2344`↓），那時修訂已經生效。
- **重新產生換到 404**：`tool_meta.regenerate_summary` 在 `_resolve_package`
  （`tool_meta.py:614`）捕捉 manifest 身分，LLM 往返之後由 `_store_meta`
  （`tool_meta.py:689`，`expected_identity=identity`）→ `write_tool_meta` →
  `_write_package_file_atomic` 的 `_still_the_expected_package`（`tools.py:1305`）
  重檢**同一個** `tool.json` 身分。切換不動它。

三個窗口都被接住，**沒有**發現任何一個沒被接住的窗口，所以刪。

**刪掉的**（連同因此變成死綁定的變數 `reviseBusyForThisTool`／`isTogglingThisTool`）：
`Switch` 的 `reviseBusyForThisTool` 與 `isRegenerating`；`重新產生` 與 `送出修訂`
（含 `修訂意見` 欄位）的 `isTogglingThisTool`，以及 `onRegenerate` handler 裡那個
重檢——`disabled` 只是渲染、重檢才是閘，兩者必須同進同退，留下任何一半都是規則被
拆成兩半。

**留下的，各自有與身分無關的理由**：`Switch` 的 `!tool.valid`（無效套件不會被廣告
也不會執行，開關對它沒有可觀察的效果）與 `mutating`（同一列已有 `PATCH`／`DELETE`
在飛——那是重複送出的問題，不是身分問題）；`送出修訂` 的 `writesBlocked`、`isFinal`
與 `displayedMayBeStale`（最後那個講的是「對著可能已被取代的內容寫回饋」，是另一類
危害，D40 R2-4 另行裁決過）。`定版／解除定版` 一個字都不動。

**使用者拿回什麼**：一次修訂或重新產生跑到一半才決定「這個工具必須關掉」時，現在
按得下去——這在本專案自稱主要介面的那個介面裡，原本要等一整個 session。

**測試上的誠實**：這一條**沒有**新增前端測試，理由與 D40 overall r7 O7-2 當初寫下
的一模一樣（那正是本輪退休掉的那組閘）：它是 JSX 的 `disabled` 運算式加一個 handler
內的重檢，屬元件層級，而本專案的 vitest 跑在 node、**沒有 jsdom**，純函式以外的東西
量不到。這裡也**沒有**為了湊覆蓋率把它抽成純函式：一個「不看修訂、不看重新產生」的
述詞，其正確性正在於它**不接收**那兩個輸入，而那是型別與程式碼審查看得見的事，不是
單元測試證得出來的事。後端那半（開關在每個窗口都被接住）由既有的
`test_a_toggle_that_lands_before_the_swap_is_carried_across_not_reverted`、
`test_a_toggle_arriving_during_the_swap_waits_for_it_and_still_wins` 與
`test_an_enabled_toggle_during_the_generation_now_costs_nothing_at_all` 釘住。

### D41 附錄（P1 review r3）：把規則抽出來，而不是把整趟掃描搬到熱路徑上

四條 finding。第一條是 **r2 自己的修法引進的**（而且是被一個假二分推上去的），
第二條是 **P1 退休一道守衛時寫錯的理由**，第三條是**裁決當時描述的載荷已經換人**，
第四條是路由契約與 README 互相矛盾。

#### R3-1：一份拼法可以很便宜——r2 選的是「貴的那一份」（P2）

r2 把兩處執行前檢查改問 `package_enabled`，而當時 `package_enabled` 就是
`_scan_package(...).enabled`。掃描**很早**就讀了狀態檔（`_read_enabled_state` 是
manifest 工作之前的第一件事，這是刻意的：所有失敗路徑都要能回報操作者設的值），
然後才去讀 manifest、解析 entry、做 realpath containment、stat 它。於是 `Popen`
前一行拿到的那個布林，**在子行程啟動時已經是 0.5 ms 與十幾次檔案操作之前的東西**
——一次落在那段裡的 `PATCH` 照樣把剛被關掉的工具送出去，正是 R4 存在要擋的結果，
被 R2-1 的修法重新引進。上一行的身分檢查幫不上忙：它更早。這也不是本子系統以名義
接受的那種 syscall 對。

**r2 的裁決把選項寫成二分（「呼叫 `package_enabled`＝一整趟掃描」對「寫第二份
拼法」），並選了貴的那個以保住單一拼法。那是假二分**——第三個選項是把**優先序
本身**抽出來：

- `_effective_enabled(state, manifest)`：**純函式**，就是那條規則，只回答開關這一題。
  present ⇒ 檔案說的值（讀不出來時它本來就已經是 False）；ABSENT ⇒ manifest 的
  legacy 欄位（預設 true、非布林忽略）。
- `_scan_package` **呼叫它**，把自己**已經在手上**的兩份讀取交給它——不是重讀。
  重讀會讓同一次掃描用 A 次讀到的規格配 B 次讀到的開關，正是本階段要消滅的那一類。
- `package_enabled` 是**讀取端的前門**：它去取那兩份輸入，然後問同一個
  `_effective_enabled`。`tool_builder` 的換裝 carry 與兩處執行前檢查都走它。

**兩者證得出不會分歧**：不是「兩份拼法我們會維持同步」，而是**同一個函式**；
`test_the_scan_and_the_execution_check_answer_the_one_rule_identically` 對八種形狀
（三態、ABSENT 底下 legacy 的兩個方向、manifest 三種提不出 legacy 欄位的壞法）
逐一要求 `_scan_package(d).enabled` 與 `package_enabled(d)` 相同。

**順序才是重點**：`package_enabled` **最後才讀 `.afterthread-state.json`**。狀態檔存在時它是
唯一一次讀取；不存在時先取 manifest 當 fallback、**再把狀態檔讀一次**（一次
ENOENT 的 lstat，約 10 us），所以呼叫端拿到的值與它下一行的動作之間只隔一個比較。
那次重讀只會讓答案**更新**：`PATCH` 若落在 manifest 讀取那一段，它產生的正是這次
重讀要找的檔案，規則會改用它。

> **r4 更正（見下方 r4 附錄 R4-1／R4-3）**：這段有兩處要改。**一**、「只隔一個
> 比較」在 r4 之後是「一個比較、一個 `lstat`、再一個比較」——身分檢查移到了
> `Popen` 的前一行，理由見 R4-1。**二**、把「值是新鮮的」講成一種**保證**是錯的：
> 讀取器選定版本的時點是 `open`，不是 `read`（`_read_regular_file_capped` 先
> `os.open` 再從那個 fd 讀），而發佈是 `os.replace`，所以一次落在 open 之後的
> `PATCH` 會讓這次讀取從一個**已經不是現行版本**的 inode 拿到舊值。真正成立的
> 性質是「拿到的一定是**某一個完整發佈版本**、絕不是撕裂的半份，而那個版本是
> `open` 當下的現行版本」。

**實測（同一個行程內新舊並排量、best of 7×2000 次以壓掉雜訊；非估算）**：

```
                              r2（整趟掃描）  r3（抽出規則）  pre-r2 的 _read_enabled_state
ABSENT（從未切換過＝常見情形）      443 us         95 us            10.6 us
PRESENT 可讀                        530 us         65 us            58.3 us
PRESENT 讀不出                       77 us         70 us            63.5 us
ABSENT（12 KiB manifest）           742 us        257 us            10.4 us
一次完整工具呼叫（含子行程）                    37.6 ms
```

（r2 附錄記的 526／579 us 是當時另一次量測，機器雜訊使絕對值不同，量級與結論一致。
「PRESENT 讀不出」在 r2 本來就便宜，因為掃描讀到壞掉的狀態檔就短路了。）

pre-r2 的 10 us 買不回來，也不該買回來——它便宜的原因就是**它回答錯了問題**
（ABSENT 讀成沉默）。r3 的 95／65 us 是「同一條規則、答對、而且值是新鮮的」的價錢：
PRESENT 幾乎就是那次狀態檔讀取本身（65 vs 58 us），兩處合計約 0.19 ms，佔一次最小
工具呼叫 **0.5%**（r2 是 2.5%）。

**符號代價寫明**：`package_enabled` 不再是 `_scan_package` 的一行包裝，所以多了
一個 `_read_manifest_object`（total reader，所有壞法都回 None，與掃描把它們各自
變成 `error` 是同一組事實的兩種用途）。`package_enabled` 也自己擋掉 symlink 的
**套件目錄**，理由與 `_scan_package` 把同一道檢查放第一位一樣：`<link>/.afterthread-state.json`
會跟著連結離開 tools 目錄。它回的是掃描對這種目錄回的同一個答案（預設值），因為
這是**拒絕去看**而不是判斷——那一列本來就 `valid=false`。

> **r4 更正（見下方 r4 附錄 R4-2）**：最後那句「那一列本來就 `valid=false`」是
> **錯的守衛**——執行路徑根本不看 `valid`（handler 是套件還正常時建好的）。兩處
> 的答案在 r4 都改成 `False`。

**窗口本身的量測（`strace` 同一次工具呼叫，數「最後一次碰 `.afterthread-state.json` 到
`vfork` 之間隔了幾個 syscall」）**：

```
r2（package_enabled = 一整趟掃描）        40 個 syscall
r3（抽出規則）                             0 個
```

r2 那 40 個是什麼，值得照抄一段：讀完狀態檔之後還有兩次 `lstat tool.json`、一次
`openat tool.json`，接著是 `_entry_file_exists` 的 realpath——把 `/tmp/…/tools/echo`
與**直譯器絕對路徑**的每一層目錄逐層 `lstat`／`readlink`（`/home`、`~/.local`、
`~/.local/share/uv`…），最後才 `lstat run.py`、`vfork`。r3 的尾巴則是：

```
lstat(".../tool.json")      ← 身分檢查
lstat(".../echo")           ← package_enabled 的 symlink 拒絕
lstat(".../.afterthread-state.json")    ← 第一次讀（ABSENT）
openat(".../tool.json")     ← 取 legacy fallback
lstat(".../.afterthread-state.json")    ← 第二次讀：權威，而且是最後一個
vfork(...)                  ← 子行程
```

> **r4 更正（見下方 r4 附錄 R4-1）**：這張表與這段尾巴在 r4 之後不成立，而且
> 「0 個 syscall」原本就**不是**它被當成的那個保證。r4 把身分檢查移到最後，所以
> 「最後一次碰 `.afterthread-state.json` → `vfork`」變成 **1 個路徑 syscall**（那次身分
> `lstat`）；而「身分檢查 → `vfork`」則從 6～7 個路徑 syscall 變成 **0 個**。
> 新的實測尾巴見 r4 附錄。

**可證的量測（把修法換回 r2 的寫法，當場失敗）**：

- `test_a_toggle_landing_between_the_state_read_and_popen_is_still_caught`
  從 `_read_manifest_object` 內部（也就是那個間隙本身）發動 `PATCH` → r2 寫法回
  `'ok'`（**被停用的工具真的跑了**），r3 寫法回 `_TOOL_DISABLED_RESULT`；
- `test_the_execution_toggle_check_reads_the_state_file_last_and_scans_nothing`
  的軌跡在 r2 寫法下是 `['scan', 'state', 'scan', 'state', 'popen']`——每一次檢查
  都是一整趟掃描；r3 下是 `['state', 'state', 'popen']`。

#### R3-2：P1 退休 containment 重驗時寫的理由是錯的（P2）

P1 移除 `set_enabled` 的寫入邊界 containment 重驗，理由記成「發佈器自己的 `lstat`
結構上就保證了」。**那句話只對一半**：`lstat` 不跟隨**最後一段**（所以 symlink 的
`.afterthread-state.json` 確實擋得住），但它跟隨**每一層上層目錄**，而 `mkstemp(dir=…)` 與
`os.replace` 也一樣。再加上 `set_enabled` **在取鎖之前**就解析完路徑，而解析出來的
是一個會在每次 syscall 被重新詮釋的**字串**，那個等待又可能長達一整個修訂尾段——
於是「套件目錄被改名移開、原位放一個指向別處的 symlink」會讓發佈把 `.afterthread-state.json`
寫進連結目標，並且回報成功。**實測**：拿掉重驗、在取鎖那一刻置換目錄，
`set_enabled` 回 `True`，而 `/tmp/…/elsewhere/.afterthread-state.json` 裡真的躺著
`{"enabled": false}`。

**裁決分兩層，而且要分清楚**：

- **危害本身**屬裁決紀錄 #5 的類別：能在 tools 目錄裡改名、種 symlink 的行為者
  以**服務自身 uid** 執行，直接寫那個檔案更省事——加固這條間接路徑對他零價值。
- **但 P1 寫下的理由是錯的**，而「一道被退休的守衛，其宣稱的替代品並不存在」比
  「留著它」或「誠實地退休它」都糟：下一個讀者會照那句話編列預算。

**所以重驗放回來**，位置是**鎖內、發佈前一行**（本模組其他 last-instant 檢查的同
一個位置），走的是既有的組合解析器 `_resolve_package_dir_no_alias`——那正是上面
那幾行 inline 寫的同三步（名稱 regex ＋ alias 拒絕 ＋ resolve-and-contain），所以
重驗不可能漂成比原檢查弱的版本。**比對的是相等**，於是 r2 分析依賴的那次**合法**
重新詮釋照樣成立：一次修訂換裝之後 `<tools_dir>/<name>` 是**另一個真實目錄**但
**同一個解析後路徑**（普通目錄 resolve 成自己，不論背後是哪個 inode），而種進去的
symlink 不是被 alias 閘擋掉、就是 resolve 到別處。那條合法路徑由既有的
`test_a_toggle_arriving_during_the_swap_waits_for_it_and_still_wins`（對**真的**換裝）
釘住，本輪不另寫一份。

**剩下的窗口，寫明**：重驗與發佈之間是 `lstat` → `mkstemp` → `fchmod` → 寫 → fsync
→ `os.replace`，也就是發佈器自己那串 syscall；那是本模組以名義接受的殘留，
與側檔寫入在同一條線上。

#### R3-3：目錄 fsync——裁決當時描述的載荷已經換人（P3）

`_write_package_file_atomic` 一直寫著「刻意不 fsync 目錄」，理由借的是 cli.py 對
`.env` 的裁決，並補一句「這裡更容易接受，最壞情況只是一份**可重新產生**的總結」。
**同一個發佈器現在還載著啟用狀態**，而它不可重新產生，**遺失的方向是開**：一個
legacy 欄位寫著 `enabled: true` 的舊套件，操作者成功 `PATCH` 成 false，然後在
`os.replace` 回來之後、目錄項落地之前掉電——退回 fallback 就把一個被刻意關掉的
工具重新打開。那與本階段對「讀不出來的狀態檔」刻意選的 fail-closed **方向相反**。

**裁決：fsync 目錄，而且對兩種載荷都做。** 代價本機 ext4 實測一次發佈約
1.2 → 2.5 ms，而發佈只發生在人按開關、一趟總結往返結束、一次修訂換裝——不在掃描、
廣告或呼叫路徑上。**不加 per-caller 旗標**：那等於讓下一個後端自有檔案的耐久性
取決於有沒有人記得開它，而 `_RESERVED_PACKAGE_FILENAMES` 與這個函式的 `filename`
參數當初都是往反方向裁決的。

**它放在 `try` 之外**，因為到那一行 `os.replace` 已經回來、檔案已經發佈：這裡的
失敗**不能**變成 False，否則 `set_enabled` 會把一個確實生效的開關回成 404。
`OSError` 一律吞掉，留下的就是 r3 之前的保證（原子但不耐久）。兩件事各有測試：
`test_the_publish_fsyncs_the_package_directory_after_the_rename`（改名之後被 fsync
的是**那個目錄**，狀態檔與側檔都是）與
`test_a_failed_directory_fsync_does_not_unpublish_a_written_state_file`。

#### R3-4：PATCH 路由的 docstring 還在描述 P1 已經退掉的拒絕（P3）

`routers/tools.py` 的 `update_tool` 仍寫著「manifest 讀不出／寫不進 ⇒ `set_enabled`
回 False ⇒ 404」。P1 之後那條路徑根本不讀 manifest，所以 `tool.json` 是 FIFO、
過大或讀不出來的套件現在**成功切換、回 200**，只是那一列仍是 `valid=false`——
README 早就這樣寫，於是 API 自己的契約與 README 對同一條端點互相矛盾。

**實測（跑真的路由）**：FIFO manifest → 200 `{enabled: false, valid: false,
error: "missing tool.json"}`；過大 → 200 `error: "tool.json is too large"`；
chmod 000 → 200 `error: "tool.json is not a readable regular file"`；
`.afterthread-state.json` 是目錄（發佈失敗）→ 404；不存在的工具 → 404。docstring 改成這份
清單，並寫明「manifest 已不在 404 的理由之列」以免下次又被讀成疏漏。

### D41 附錄（P1 review r4）：兩道檢查搶同一個名額，而「零 syscall」從來不是新鮮度

四條 finding。前兩條都是**執行前檢查**的問題——一條是 r3 自己的修法把 D40 O6-1
的 P1 危害重新打開，一條是一個**拒絕去看**被寫成 fail-open；第三條是 r3 對那道
檢查的**保證**講得比程式碼給得到的多；第四條是兩份使用者文件對同一個修法互相
矛盾。

#### R4-1：`Popen` 前只有一個名額，而它屬於身分檢查（P1）

r3 之後的順序是 `_still_the_expected_package` → `package_enabled` → `Popen`，於是
身分的答案在子行程起來時已經隔了一次狀態檔讀取（ABSENT 路徑還多一次 manifest
讀取）。**這不是本模組接受的那個 lstat/exec 對**，而且它讓「身分檢查就在 `Popen`
上一行」這句話變成假的。

**危害是 D40 overall r6 O6-1 原樣復活**：`cwd` 是核心在 exec 當下從**路徑**解析
的，所以落在那個間隙裡的一次換裝不只是競態，是**改道**——子行程在**新套件**裡
起來，卻帶著舊的 entry argv、模型看到的舊參數 schema、以及**已經組好的舊套件
`.env` 值**；事後看不出異狀（attempt 記的是**名字**，名字沒變）。執行登記幫不上
忙：它延後的是**舊備份的移除**，它不阻止換裝。

**實測（修法前，HEAD `fffedca`）**：從 `Popen` 前那次狀態檔讀取內部發動
`_promote_staging_replace` 形狀的換裝 → 回到模型手上的是 `'NEW'`——**替換進來的
套件真的執行了**。

**兩道檢查不可能都在最後，所以這是一個排序判斷，裁決如下：身分檢查在最後。**
在舊契約下跑**別的套件的程式碼**、還配上舊套件的環境值，比「在操作者剛剛按下的
開關之後幾微秒才停下來」嚴重。反過來排，開關那一側只剩**一個 `lstat`** 的窗口，
那正是本模組到處以名義接受的殘留；而身分那一側的窗口**歸零**。

**修法**：`package_enabled` 排到 `_still_the_expected_package` **之前**，身分檢查
與 `Popen` 之間不留任何東西。

**實測（`strace` 同一次真的工具呼叫，改前／改後並排；本機 ext4）**：

```
                                    r3（身分在前）   r4（開關在前）
最後一次碰 .afterthread-state.json → vfork          6～7 個          1 個（身分 lstat）
身分 lstat → vfork                       6～7 個          0 個
```

（兩邊在 `vfork` 前都還有 3 次 `fstat`，那是 `subprocess.Popen` 對**已經開好的**
三根 pipe 做的，不解析任何路徑。）r4 的尾巴逐行：

```
PRESENT（有狀態檔）                      ABSENT（沒有，走 fallback）
lstat(".../echo")        symlink 拒絕    lstat(".../echo")        symlink 拒絕
lstat(".../.afterthread-state.json") 讀取器的 lstat  lstat(".../.afterthread-state.json") ENOENT（第一次）
openat(".../.afterthread-state.json")＋fstat/read    openat(".../tool.json")＋fstat/read
lstat(".../tool.json")   ← 身分檢查      lstat(".../.afterthread-state.json") ENOENT（權威）
vfork(...)                               lstat(".../tool.json")   ← 身分檢查
                                         vfork(...)
```

**handler 那一處刻意維持相反的順序**（身分在前、開關在後），而且理由不衝突：那裡
**兩道檢查都不貼著任何動作**（後面還有 `.env` 讀取、序列化、無上限的 threadpool
排隊），所以排第二換不到任何東西；而身分在前換得到「已經被換掉的套件回報**換掉
了**，而不是去讀它繼受者的狀態檔」。只有 `Popen` 那一處的「貼著」是稀缺資源。

**寫明的代價**：一個**同時**被換掉又被關掉的套件，在 `Popen` 那一處現在會拿到
`_TOOL_DISABLED_RESULT` 而不是 `_TOOL_REPLACED_RESULT`。兩種都不會跑；handler
自己的身分在前那一對已經在常見情形先回報了換掉；而拒絕字串按契約本來就只講類別。

**可證的量測（把順序換回去，當場失敗）**：
`test_a_revise_landing_between_the_toggle_read_and_popen_is_refused_not_run` 拿到
`'NEW'`；`test_the_identity_check_is_the_last_thing_before_the_subprocess_starts`
的軌跡變成 `['identity', 'enabled', 'identity', 'enabled', 'popen']`。

#### R4-2：symlink 的套件目錄讓停用檢查回 `True`，於是停用的工具跑起來（P2）

`package_enabled` 對 symlink 的**套件目錄**回 `True`，理由寫著「這是拒絕去看，
不是判斷；那一列本來就 `valid=false`，凡是看 `valid` 的都不會廣告或執行它」。
**執行路徑不看 `valid`**——它的 handler 是套件還正常時建好的，手上只有一個路徑。
而 `package_identity` 跟隨**上層** symlink，所以它照樣看到同一個 `tool.json`
inode，身分檢查也過。

**實測（修法前）**：廣告一個工具 → 用 API 把它關掉（回
`_TOOL_DISABLED_RESULT`，正確）→ 把目錄改名移開、原位種一個同名 symlink 指回去
→ 兩道執行檢查都通過，`Popen` 跟著連結把**停用的工具**跑起來（`'ok'` ＋ sentinel
落地）。一次違反兩個契約：操作者的開關，以及「symlink 的套件一律無效、絕不執行」。

**裁決：放在開關那一題，並且把答案改成 fail-closed。** 三個理由：

1. **不增加任何 syscall**。那個 `lstat`（`directory.is_symlink()`）本來就在
   `package_enabled` 的第一行，改的只是它的**答案**。放進身分檢查則要對
   `_still_the_expected_package` 的**三個**呼叫端各加一次目錄 lstat，而 spec 的
   要求正是「若已經在那裡的檢查答得出來，就不要在 `Popen` 前多加第三次讀檔」。
2. **它修的是根因而不是再加一道拒絕**。原本的 `True` 是被一句**已證明為假**的話
   背書的；本模組對「拒絕去看」的規則到處都是 fail-closed（`_read_enabled_state`
   的 OSError 分支、R2 對讀不出的狀態檔的裁決），只有這一格 fail-open。
3. **一條規則的等式保住**。`_scan_package` 對同一形狀也改回 `False`，所以掃描與
   執行前檢查仍然答得一樣——只是那個共同答案從 `True` 變成 `False`。這正是 R2
   已經裁決過的形狀：**答不出「可不可以跑」的套件，列成無效**且**停用**，於是
   `enabled_llm_tools` 的 `valid AND enabled` 兩個濾條都說不。

**清單上的答案（本輪改變的行為，寫明）**：symlink 的套件目錄那一列，`enabled`
從 `true` 變成 `false`（`valid=false` 與 `error` 不變）。UI 上那顆開關本來就因
`!tool.valid` 而停用，所以操作者看到的是「無效 ＋ 關著 ＋ 一句原因」，三者一致。

**殘留窗口寫明**：symlink 的拒絕現在排在身分檢查**之前**，所以還剩一個 `lstat`
的窗口——要在那一瞬間把 live 目錄改名並種上 symlink。那是裁決紀錄 #5 的等級
（該行為者以**服務自身 uid** 執行，直接動檔案更省事），與 R4-1 接受的殘留同一個。

**回報字串**：走 `package_enabled` 就代表回 `_TOOL_DISABLED_RESULT`。這是對的
方向：對模型而言「這個工具不能跑」正是事實，而告訴它「套件被換掉了」（去重讀
規格）對一個現在是連結的路徑是更差的建議——沒有新規格可讀；操作者那一側則由
那一列的 `error` 講清楚。

**可證的量測（把任一半換回去，當場失敗）**：只還原 `package_enabled` →
`test_a_symlinked_package_directory_cannot_run_a_tool_disabled_through_the_api`
回 `'ok'`（**停用的工具跑了**）；只還原 `_scan_package` →
`test_symlinked_package_dir_listed_invalid` 的 `enabled` 量到 `True`。兩種還原都
會讓 `test_the_scan_and_the_execution_check_answer_the_one_rule_identically`
失敗——**該測試本輪新增了 symlink 目錄這第九種形狀**（r3 當初驗了 13 種、只釘 8
種，這一種不在釘住的那批裡，所以是補上而不是修改既有案例）。

#### R4-3：「最後一次讀到 `vfork` 之間 0 個 syscall」證明不了新鮮度（P2）

r3 用那個 0 佐證「值是新鮮的」。**證不到**：`_read_enabled_state` 走
`_read_regular_file_capped`，那個 helper 先 `os.open`、**再**從那個 fd 讀，所以
**版本在 `open` 就選定了**；而發佈端是 `os.replace`。一次在 runtime 打開舊 inode
**之後**才落地的 `PATCH`，會讓 runtime 從一個**已經不是現行版本**的檔案讀到
`true`。

**裁決：不加鎖，改講法。** 這裡的窗口是 open→read，兩三個 syscall，正是本子系統
**以名義接受**的那個殘留；要關掉它只能在執行路徑上加鎖，那等於讓**每一次工具
呼叫**與**每一次開關**互相序列化。錯的是**宣稱**，不是實作。

**改成的真正性質**（已寫進 `package_enabled` 的 docstring、`backend/README.md`
與 `docs/tool-calling.md`）：讀取器拿到的一定是**某一個完整發佈版本**——絕不是
撕裂的半份、也不是兩份各一半——而那個版本是它 `open` 當下的現行版本。

**便宜就釘住的那一半**：`test_a_reader_that_opened_the_state_file_sees_one_whole_published_version`
先 `os.open` 舊 inode、再跑一次成功的 `set_enabled`，然後從**握著的 fd** 讀——
拿到的是完整、可解析的**舊**文件，而下一次讀取拿到新的。（這條釘的是**性質**，
不是某個修法的守衛，所以沒有「拿掉就失敗」的對照——要讓它失敗得把發佈器換成
truncate-in-place 的寫法。）

#### R4-4：`tool-calling.md` 教的修法在 UI 上做不到（P3）

`docs/tool-calling.md` 告訴操作者：狀態檔壞掉但**還是一般檔案**時「按一次開關就
好」。但那個狀態正是讓該列 `valid=false` 的原因，而前端對**每一個**無效列都停用
那顆 `Switch`（`ToolsPage.jsx:693`，`disabled={!tool.valid || mutating}`）。所以
那條修法只有走 API 或自己改檔案才做得到。`backend/README.md` 早就寫對了，兩份
使用者文件因此互相矛盾。

**裁決：改文件，不改前端。** `tool-calling.md` 現在把「PATCH 覆蓋成乾淨的一份」
與「UI 上那顆開關按不下去」分開講，並指出可行的兩條路（刪掉那個檔案退回
fallback，或直接送 `PATCH`）。

**沒做、留作建議**：讓 `Switch` 在無效列上也可按，是一個關於**所有**無效套件的
產品決定（今天「無效 ⇒ 開關停用」對每一種無效理由一視同仁，而多數無效理由確實
讓開關沒有可觀察的效果），不是順手改掉的事；要做應該連同「哪些 `error` 值下開關
仍有意義」一起裁決。

#### 本輪被駁回的一條

第五條 finding（修訂會吞掉換裝窗口內對 `.afterthread-state.json` 的手改）**駁回**，理由記在
repo 根目錄 `裁決紀錄.md` #9：那段期間對**任何**檔案的手改都會被丟棄（那是「整包
換掉」的定義），行程內的鎖鎖不住編輯器，而 P2 的版面會讓這個窗口連同過渡性的
`_STATE_PUBLISH_LOCK` 一起消失。本輪的修改**不觸碰**該行為。

### D41 附錄（P1 review r5）：這個檔案的名字與內容，都不是後端說了算

四條 finding，而且是**同一個錯誤的四張臉**：P1 在一個**它不擁有的目錄**裡宣告了一個
檔名，然後把每一個叫這個名字的檔案都當成自己的。套件目錄是**工具的**——D21 明文
說操作者手改套件檔案是支援的行為，而一個工具本來就可能在自己的目錄裡放游標、快取
或設定檔。P1 之前 `.state.json` 不是保留名，所以「已經有一個」不是假想。

本輪同時把檔名從 `.state.json` 改成 `.afterthread-state.json`，並**就地更新了上面
每一節的檔名**（這個分支還沒發布，磁碟上不存在舊名字；讓附錄指向一個不存在的檔案
比改名本身糟）。

#### R5-1：一個不是我們的檔案，被讀成開關、讀成無效、然後被覆蓋掉（P1）

三種傷害，全部無聲：

1. 一個 manifest 寫著 `enabled: false`、而**自己的** `.state.json` 剛好帶著一個為真的
   `enabled` 的套件，會被**重新廣告並執行**——沒有任何人碰過那個開關；
2. 一個自己的檔案裡沒有布林 `enabled` 的套件，會翻成 `valid=false` **停止運作**；
3. 第一次發佈（一次 `PATCH`，或一次修訂的 carry）會把它換成一份光禿禿的
   `{"enabled": …}`，**摧毀那個工具存在那裡的東西**。

**修法是兩半，缺一不可**：

- **(a) 名字要自己講出它屬於誰**：`.state.json` → `.afterthread-state.json`。這讓
  碰撞變得**不合情理**，但它證明不了任何事——操作者或未來的工具照樣可以在我們挑的
  任何名字上建檔案。
- **(b) 只信任認得出來是自己的那一份**：文件裡的所有權標記
  `{"afterthread": "tool-state", …}`（`_STATE_MARKER_KEY` / `_STATE_MARKER_VALUE`，
  發佈器每次都寫）。**這一半才是真正關上危害的那一半**。

**沒有標記的檔案 = ABSENT**，一個字都不多：退回 manifest fallback，與「這個套件從來
沒有狀態檔」逐字同一個答案。並且**絕不覆蓋**（`set_enabled` 拒絕、回 False → 404）、
**絕不刪除**（修訂的 carry 改成 `shutil.copy2` 逐位元組帶過換裝）。

**每個消費端各自的答案（四種狀態 × 四個消費端）**：

| 那個名字上的東西 | `_read_enabled_state` | 掃描／清單 | `set_enabled` | 修訂 carry |
|---|---|---|---|---|
| 不存在 | ABSENT | manifest fallback | 發佈新檔 | 發佈當下的有效值 |
| 我們的、讀得出來 | 權威 | 檔案說的值 | 覆蓋 | 發佈當下的有效值 |
| 我們的、答不出來（有標記但 `enabled` 不是布林；或完全讀不到） | UNREADABLE | `valid=false` ＋ `enabled=false` | 覆蓋（＝R1-2 寫明的修法） | 帶成停用 |
| **不是我們的**（讀得出來、沒有標記） | FOREIGN | **`valid=true`** ＋ manifest 的值 ＋ `notice` | **拒絕（404）** | **逐位元組複製** |

**幾個刻意的取捨**：

- **FOREIGN 不會讓套件無效**，否則就是傷害 2 換一個入口。它走的是 `_PackageScan`
  新增的 `notice` 欄，`_listed_row` 在沒有致命 `error` 時把它放進列的 `error` 欄。
  兩個欄位在**內部**分開，因為 `validate_package` 是用 `error` 當安裝閘的——把一個
  「什麼都沒壞」的提示折進去，會讓安裝為了一個不影響執行的事實而失敗。
- **UI 上看不到那句話**（誠實寫明）：前端只在 `valid=false` 的列顯示 `error`
  （`ToolsPage.jsx`），所以這個 notice 只在 `GET /api/tools` 看得到，以及操作者按下
  那顆開關時撞到的 404。要讓有效的列也顯示提示，是一個關於**所有**提示的產品決定
  ——與 R4-4 對「無效列的開關能不能按」的處理同一個形狀，同樣留作建議。
- **`set_enabled` 為什麼是拒絕而不是「假裝成功」**：讀取端仍然會從 manifest 回答，
  所以一次「成功」會是一個**可證明沒有生效**的開關。三個選項（照樣覆蓋／假裝成功／
  拒絕）裡只有拒絕讓「告訴操作者的」與「磁碟上的」一致，而且它把問題送到操作者**正在
  動手的那一刻**。代價寫明：那一包在操作者把自己的檔案挪開之前，開關按不動。
- **完全讀不出來的檔案仍然算「我們的」**（fail-closed）。在一個寫著 "afterthread" 的
  名字上，讀不到內容時把它當成自己的壞檔案，是唯一不會意外開啟工具的讀法，也讓 R2
  與 R1-2 的裁決原封不動。**反方向的殘留寫明**：一次把標記也一起毀掉的手改或磁碟
  損壞會落進 FOREIGN，於是退回 manifest——而 manifest 多半是開啟。這是「不認得的
  檔案絕不當成自己的」買來的代價，而 (a) 讓它不合情理。
- **修訂的代價**：一個名字被佔住的套件沒有地方放我們的開關，所以它的有效狀態由
  **manifest** 決定，而 manifest 正是修訂會重寫的東西——一次修訂因此可能改變那一包的
  開關。這與「手改那個 legacy 欄位」是同一件事，寫在 `carry_package_state` 裡。
- **FOREIGN 的判定上限就是 `_STATE_MAX_BYTES`**（4 KiB），所以 carry 那次 `copy2`
  是有界的，`_STATE_PUBLISH_LOCK` 裡不會跑進一個無上限的複製。超過上限的檔案落在
  UNREADABLE，也就是說**它會被一次修訂換掉**——這是為了守住鎖的有界性刻意接受的
  殘留（我們的檔案約 50 位元組，上限是它的 80 倍）。

**執行路徑的代價（本機 ext4 實測，best of 7×2000，與 r3 附錄同一個方法）**：

```
ABSENT（從未切換過）                 92.5 us   （r3 記的是 95 us）
我們的、讀得出來                     63.4 us   （r3 記的是 65 us）
UNREADABLE（超過上限）               52.1 us   （r3 記的是 70 us）
FOREIGN（工具自己的檔案）           167.9 us   ← 本輪新增的形狀
```

前三種**沒有回歸**（標記比對是一次 dict 查找，三個常數答案改成模組層單例，
所以連配置都省掉了）。FOREIGN 貴是因為它是唯一要付**兩次真正 open** 的形狀：
第一次讀出「這不是我們的」，然後取 manifest fallback，最後依「狀態檔最後才讀」
的規則**再讀一次**。兩處執行前檢查合計約 0.34 ms，約佔一次最小工具呼叫
（37.6 ms）的 **0.9%**——只有名字被佔住的那些套件會付，而那第二次讀不能省：
它正是「操作者把自己的檔案挪開、按下開關」在這一瞬間會被看見的機制。

**測試**：`test_a_foreign_file_at_the_state_files_name_is_answered_as_absent`（六種
內容 × 掃描／清單／廣告／執行，並在前後比對位元組）、
`test_set_enabled_refuses_rather_than_destroying_a_foreign_state_file`（拒絕、檔案
不變、沒有暫存檔殘留，而且操作者把檔案挪開之後一切如常）、
`test_a_revise_leaves_a_nested_state_file_alone_and_carries_a_foreign_root_one`。
帶標記的檔案「與今天完全相同」由既有那一整批釘住（fixture 改用 `_state_document`）。

#### R5-2：保留名域縮到根目錄——沿用本模組自己對巢狀 `.env` 的裁決（P2）

`_strip_builder_sidecars` 與 `_revise_copy_ignore` 在**每一個深度**比對這個 basename，
而 runtime **只讀根目錄那一個**。於是一個 builder 把工具的初始狀態寫在
`data/.state.json`、還用 `run_shell` 驗過能跑，promote 卻把它靜默刪掉：manifest 照樣
通過驗證、安裝照樣回報成功，工具在**第一次真的被呼叫**時才壞掉。修訂則是在 builder
看到工作區之前就把同一個檔案丟掉。

**裁決：只保留根目錄那一個**，理由不是新的——`_revise_copy_ignore` 早就對巢狀 `.env`
做過**逐字同一個**裁決（R2-2：根目錄那個是後端的，巢狀的是普通套件內容，工具以套件
目錄為 cwd，大可自己打開它）。判別式因此多一個**必填 keyword-only** 的 `at_root`
（預設值會變成一個等著被忘記的錯誤分支，同 `_is_preserved_env_name` 的 `exact_present`），
根目錄查 `_RESERVED_PACKAGE_FILENAMES`、其他層級查 `_RESERVED_AT_EVERY_DEPTH`。
系統提示的兩處（安裝的套件契約、修訂的三件事）都加上了這條規則，並明說巢狀的同名
檔案是它自己的。

**根目錄那一個仍然無條件剷除**，這半是承重的：它是 R5-3 發佈初始狀態的位置，而一個
被允許佔住根目錄名字的 builder 可以讓開關**按不動**，然後由它自己寫的 manifest 決定
那個值——正是 R5-3 要擋的東西換一條路進來。

**`.ai_meta.json` 維持各層級，本輪不動**（spec 明文），而**同一條論證對它是否成立，
作為建議寫在這裡**：**不完全成立，因此建議維持現狀**。狀態檔的論證是「後端從不看
根目錄以下」，這對 sidecar 也成立（`read_tool_meta` 只讀根）；但 sidecar 的每層級規則
另有一個與「會不會被讀到」無關的理由——一個巢狀副本會讓**之後每一次修訂**都被
`validate_package` 的 embedded-secret 閘擋下，錯誤訊息指著一個操作者從沒寫過的檔案
（`_strip_builder_sidecars` 的 docstring 原文）。那個理由不隨本輪的改動消失，所以
兩個檔案的深度**本來就該不同**，這不是遺漏。若日後要重看，該一起裁決的是那個閘的
訊息，而不是保留名域。

**測試**：`test_strip_builder_sidecars_covers_the_state_file_namespace`（根目錄剷除、
巢狀保留，並直接對兩個 tuple 斷言）、
`test_a_nested_state_file_survives_an_install_while_the_root_one_is_stripped`、
`test_a_revise_leaves_a_nested_state_file_alone_and_carries_a_foreign_root_one`。

#### R5-3：新安裝仍然可以透過 manifest 的 legacy 欄位自己設開關（P2）

fresh promote 會剷掉 builder 寫的狀態檔，但對 `tool.json` 的 legacy `enabled`
**什麼都沒做**，而 `validate_package` 接受它（舊套件的 manifest 本來就帶著它，所以
非接受不可），遷移 fallback 又會讀它。於是一個模型只要生出一個其他方面完全合格、
但帶著 `"enabled": false` 的 manifest，安裝就會**成功**、回報成功，而那個工具**裝好
就是停用的、從來沒被端給模型**——builder 決定了操作者的開關，正是這整個保留名域
存在要防的事。

**裁決：讓 fallback 只服務真正的既有安裝**——`_promote_staging` 在**搬檔之前**、
注入表單秘密的下一行，把 `{"afterthread": "tool-state", "enabled": true}` 發佈進
staging。新安裝從此**不會落在 ABSENT 這一格**，fallback 因此收斂成它本來的意思：
「web-v5 P1 之前裝的」。

**`true` 是無條件的**：新裝的工具會被端給模型是本 app 的既定預設，而重點就是
**builder 沒有投票權**。操作者要關就自己關，而且現在關得掉——檔案已經在那裡了。

**寫入失敗怎麼辦**：`_ERROR_INSTALL_STATE_WRITE`（「無法寫入工具的啟用狀態，安裝已
取消。」）。**這不會把一次已經成功的安裝變成失敗**：寫入落在 staging、在 `shutil.move`
**之前**，所以失敗當下什麼都還沒安裝，「已取消」是實話——與同一個函式上方
`_inject_secret_into_env` 的失敗處理逐字同一個形狀（那也是驗證之後、搬檔之前的一次
後端自有寫入）。**不取鎖**：那時還沒有任何名字可以被 `PATCH`。

**兩個沒選的方案，理由寫明**：**拒絕**帶 legacy 欄位的 manifest 會為了一個模型的
小毛病失敗掉一次安裝，而操作者根本不是那個 manifest 的作者；**在 promote 時把欄位
剷掉**會改寫 manifest，而不改寫 manifest 正是這一階段的全部（R1）。留著它、忽略它，
與一次開關之後的處置完全一致。

**測試**：`test_a_fresh_install_publishes_its_own_state_and_ignores_the_manifests_legacy_key`
（manifest 寫著 false，裝完是 true，而那個欄位原封不動留在磁碟上）、
`test_a_legacy_package_still_answers_from_its_manifest`（沒走過這條 promote 的套件
兩個方向都照 manifest 讀，而且讀完仍然沒有狀態檔）。

#### R5-4：原子發佈的暫存檔清理追不上一次目錄改名——量測之後只改敘述（P3）

`_write_package_file_atomic` 只留著暫存檔的**路徑名**，所以套件目錄在 `mkstemp` 與
發佈之間被改名移開時，`os.replace` 失敗、接著的 `os.unlink(tmp_path)` 看的是一個已經
不在那裡的路徑。發佈器宣稱的「每一條失敗路徑都會 unlink 暫存檔，失敗的寫入不會在
套件裡留下任何東西」因此講得太滿。

**先量測，不推論**（本機 ext4，從 `mkstemp` 內部發動改名）：

```
(a) delete_tool 因執行中而延後 → 暫存檔在 .<name>.stale-<token>/ 裡
      publish=False，套件原路徑不存在，改名後的目錄內有 .afterthread-state.json.<x>.tmp
      跑一次 _sweep_stale_backups → 該目錄整個消失，tools 目錄乾淨
(b) 修訂換裝失敗並回滾（改名走、又改回來）→ publish=True，暫存檔不存在
      （路徑在 os.replace 時又解析回同一個目錄，發佈直接成功）
(c) 修訂換裝成功 → 暫存檔在 .bak-<token>/ 裡，promote 隨即 rmtree 掉整個備份
```

**結論：殘留已經被收走了**，所以程式不動，只把敘述改對——暫存檔**跟著目錄走**，由
**收走那個目錄的人**收走。兩個會把套件目錄改名移開的行為者都改進同一個有標記的
命名空間，而 `_sweep_stale_backups`（每個工具工作結尾）`rmtree` 整個目錄。**不加
目錄 fd**：要走到那條分支，唯一的辦法是一次已經把檔案交給收集者的改名。

**真正還在的殘留寫明**：`_ERROR_REVISE_UNRECOVERABLE` 留下的 `.bak-` 目錄——清掃
**刻意**不碰它（那是操作者僅存的一份工具副本）。那裡面的一個暫存檔是一個 dot-file
垃圾，躺在一個操作者本來就要手動處理的目錄裡。

**測試**：`test_a_publish_temp_rides_a_renamed_package_into_the_namespace_that_collects_it`
（從 `mkstemp` 內部改名，斷言暫存檔在改名後的目錄裡、套件原路徑什麼都沒留下，然後
一次既有的清掃把它連同目錄收走）。
