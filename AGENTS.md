# afterthread

## Communication

- Reason internally in English when helpful.
- Communicate with the user in Traditional Chinese unless the user explicitly asks for another language.

## Project Purpose

This repo is a personal afterthread system for preventing architectural knowledge vaporization: the loss of project reasoning, decision context, tradeoffs, and next-action memory after conversations have gone cold. It has two interfaces sharing the same capture / full-enrichment / update / review methodology (`docs/methodology.md`) but NOT sharing data: a primary web app (SQLite-backed) and a supplementary file-based workflow (`memory/**/*.md`).

## Operating Rules

- The web app is the primary surface: `backend/` (FastAPI + SQLite) and `frontend/` (React/Mantine SPA), packaged and launched as one process via the `afterthread` console script (see root `README.md` for install/launch, `backend/README.md` for API/config/dev details, `frontend/README.md` for page/component conventions). Prefer it for any new user-facing capability; this is no longer a file-based-only MVP.
- The OpenCode skill (`.opencode/skills/afterthread/SKILL.md`) and `scripts/afterthread.py` remain supported supplements for terminal/agent-driven capture. They operate on the separate `memory/**/*.md` file store — do not assume they read or write the web app's SQLite database, or vice versa.
- Keep records small, durable, and easy to resume from, regardless of which surface created them.
- When working with the file-based store: run `python3 scripts/afterthread.py validate` after editing memory records, and `python3 scripts/afterthread.py index` after adding or materially updating one.
- When working on the web app's backend/frontend code: run that surface's own gates before considering a change done (see `backend/README.md` / `frontend/README.md` for the exact lint/type/test commands; `e2e/smoke.sh` and `e2e/wheel_smoke.sh` for full-stack checks).
- Ground every change in the actual code and existing docs (`docs/methodology.md`, `docs/web-v2-decisions.md`) rather than inventing workflows or UI that do not exist — in particular, this app has no chat interface; AI features are structured, per-item operations (capture, enrich, assist-update, tool install).

## 測試必須能失敗

- **每一個測試都要有 mutation 證明**：把它宣稱在檢查的東西改壞，某個**具名**測試必須轉紅。沒做過這件事的測試，沒有證據能分辨程式對錯，只會製造被保護的錯覺——那比沒有測試更糟，因為它會終止後續的懷疑。
- **條件式 UI 的 oracle 要三段**：健康時不存在、觸發後出現、恢復後消除。只驗正向的話，把條件改成恆真，整套測試依然全綠。
- **契約是行為就不能用型別斷言**：`expect.any(X)` 只證明「有傳一個 X」。逾時、期限、signal 這類東西要攔截其建構函式、換成自己可控的物件、手動觸發。否則一個永遠不會觸發的 signal 也能通過，整段保護可以被刪掉而無人察覺。
- **不要用假時鐘**，也不要寫結果取決於 wall-clock 排序的測試；用受控 promise 與受控 signal。
- **起始狀態要讓多餘的寫入現形**：可能誤寫成「紅」的案例從已知「綠」開始，反之亦然。從 `null` 或從與待測寫入相同的值開始，無法分辨「沒寫」與「寫了同樣的值」。
- **不要把測試指令接到 `grep`／`tee` 後面判斷成敗**：離開碼會來自最後一個 pipe stage，結果是帶著失敗的測試 commit。先讓指令獨立跑完取離開碼，或用 `${PIPESTATUS[0]}`。

## 修正的完整性

- **改守衛前先問兩個問題**：(1) 同樣形狀還存在於幾個地方？(2) 這個修正自己有沒有帶可證偽性？兩問沒做完就交件，會直接製造下一輪 review。最常見的失敗是「兩個探針、兩個分支、兩個消費者，只修了一個」，以及「新加的守衛自己沒被釘住」。
- **窮舉要機械化**：用 AST 或全量 grep 列舉，再用另一種方式交叉驗證。憑閱讀產出的「我已經掃過了」清單會短報，而短報的掃描比不掃描更危險——它會讓所有人停止再找。
- **備份用 `cp`，還原用 `cp` 覆蓋回去再 `cmp` 驗證。禁用 `git stash`／`checkout`／`restore`／`reset`。** `cp` 若因 cwd 或路徑錯誤靜默失敗，接下來就是在沒有備份的情況下改壞檔案；動手前必須先確認備份檔存在且非空。

## 斷言與宣告紀律

- **禁止寫超出實測範圍的全稱句**：「不可能」「沒有 I/O」「結構上保證」「型別上保證」這類話，除非把該路徑走到底，否則不要寫；把斷言縮到實際量過的範圍。違反的代價不只是結論被推翻後要寫撤回紀錄，而是同一份報告裡其他判斷的可信度一起打折。
- **宣告即承諾**：不要在訊息裡宣告一個本回合沒有實際發動的動作。使用者只看得到一輪的最後一則訊息，宣告而未執行會讓進度停擺到對方主動追問為止。

## 本機量測限制

- **這台開發機只有一個 CPU core。禁止並行跑多個測試套件。** 三套並行會把機器餓到約 33% CPU，比任何真實 runner 都嚴苛；由此產生的失敗是測量方式造成的假象，必須丟棄而不是拿去修。
- **穩定性要對整套測量，不是對單一檔案**：CI 跑的是整套。標準是循序跑兩輪完整套件。

## review 迴圈的終止

- **迴圈不會自己歸零，要主動喊停。** 當每輪只剩單點且範圍持續收窄，就是收束訊號：每補一個守衛都會露出一個新的未釘住表面，範圍會收斂但數量不會歸零。
- **最後一輪要在 prompt 裡明說**：「這是最後一輪、沒有下一輪、只報會擋 merge 的東西、但不要留一手」。不明說的話，reviewer 會保留材料給下一輪。
- **停損安全的條件**：剩餘修正只動測試檔，且每一項都有實測的 mutation 證明。**mutation 證明本身就是驗證**，所以「有 code change 就要再跑一輪」的規則在此不適用。一旦動到 production code，這個條件就不成立，必須再跑一輪。
- **裁決要寫下判準，不只寫結論**，並明示「除非證明推理錯誤，勿重提」。只寫結論的話，下一輪或未來的自己會重跑同樣的幾輪。won't-fix 寫進程式註解或 `裁決紀錄.md`。
- **每輪 prompt 要附**：已修了什麼（commit）、已裁決接受什麼與理由。
- **明確接受而不修的殘留要逐條列出**，不能默默留著；沒被記下來的殘留，下一個人無法分辨它是決定還是疏漏。

## codex review loop 操作準則

- **launcher 行程必須活到 job 結束**：`adversarial-review --background` 的 node（`codex-companion.mjs`）行程一死，server 端 job 不會自己跑完，job log 直接凍結成孤兒。`--background` 不代表 launcher 可以死。
- **一律完全脫離行程樹啟動**：`setsid nohup node <codex-companion.mjs> adversarial-review --background … > log 2>&1 < /dev/null &`。Claude Code 的背景任務會被系統中止，用一般背景執行會在 session 中途被殺掉 launcher。
- **cwd 必須寫在啟動指令內**：companion 沒有 `-C` 參數，靠當下工作目錄找 repo，所以 `bash -c` 內要自己 `cd <repo>` 再 `node`，不要依賴外層 cwd。違反時 log 只留一行 `This command must run inside a Git repository.`、job 根本沒建立，而 `status --all` 仍顯示上一輪的舊 job，看起來像還在跑。
- **等待要認新 job id**：啟動前先記下已知 id 並排除掉。樣式太寬鬆（如 `grep "^- review-"`）會配到上一輪 completed 的 job 而立刻誤報完成；太嚴則會看不到 job 出現、空轉到逾時。啟動後先確認 log 無錯誤、且 `status` 真的多一個新 id，再進等待迴圈。
- **不要用 `--wait` 串流**，中繼不可靠。輪詢 `status --all` 等 job 離開 running，並加 stall 偵測：job log（`plugins/data/codex-openai-codex/state/<repo>/jobs/*.log`）mtime 超過 240 秒仍 running 即為凍死，別再等。Claude Code 內用 Monitor 工具跑這個迴圈——前景 sleep 會被擋、背景 bash 會被殺。
- **`result` 只對 finished job 有效**：job 還在跑時會回「No job found」，那不是 job 不見了，是還沒完成。
- **每輪結束清孤兒**：凍結的 job 用 `cancel <job-id>`；被取消的 job 會留下 bwrap sandbox 行程（pytest 等卡在裡面），用 `pgrep -af codex-linux-sandbox` 檢查並照 PID kill。
- **以 committed HEAD 發動**（先 commit 再 review），期間不要弄髒 working tree。
- **一輪約 5–15 分鐘**；launch 指令本身同步準備超過 2 分鐘才回是正常的。

## 依賴變更

- **繞過 pnpm 冷卻期（`minimumReleaseAgeExclude`）的豁免，待辦要釘到精確的小時，不能只寫日期。** 提早刪除豁免會讓 `pnpm install --frozen-lockfile` 直接拒絕 lockfile，沒有中間狀態，CI 全紅。刪除後用同一道指令驗證。
- **刻意打開的供應鏈缺口，要在檔案裡寫清楚為什麼接受、以及何時該關上**；否則下一個讀到的人無法分辨那是決定還是疏忽。
