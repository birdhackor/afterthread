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
## Review 教訓(P8–P10 adversarial review loop 實戰彙整)

跨 0.2.0 / 0.3.0 兩次 release、十餘輪 codex review 修出來的**類別**教訓。寫碼與自審先過這份清單,別讓同類問題再進 review。

### codex review loop 操作(本機環境實測,P8–P10 兩個 session 踩過)

- **launcher 行程就是 turn 驅動器**:`adversarial-review --background` 的 node(codex-companion.mjs)行程一死,server 端 job 不會自己跑完——job log 直接凍結成孤兒(實例:一次凍在 starting、一次凍在 verifying 中的 pytest)。「--background」不代表 launcher 可以死。
- **啟動方式**:Claude Code 的背景任務會被系統中止(同 session 內兩度殺掉 launcher),所以 review 一律用 `setsid nohup node <codex-companion.mjs> adversarial-review --background … > log 2>&1 < /dev/null &` 完全脫離行程樹啟動。
- **等待方式**:不要用 `--wait` 串流(中繼不可靠,舊教訓)。輪詢 `status --all` 等 job 離開 running,並搭配 stall 偵測:job log(`plugins/data/codex-openai-codex/state/<repo>/jobs/*.log`)mtime 超過 240 秒仍 running 就是凍死,別再等。Claude Code 內用 Monitor 工具跑這個迴圈(前景 sleep 會被擋、背景 bash 會被殺)。
- **取結果**:`result` 只對 finished job 有效;job 還在跑時會回「No job found」——那不是 job 不見了,是還沒完成。
- **孤兒清理**:凍結的 job 用 `cancel <job-id>` 清;被取消的 job 可能留下 bwrap sandbox 行程(pytest 等卡在裡面),每輪結束後 `pgrep -af codex-linux-sandbox` 檢查、照 PID kill。
- **review 期間**:以 committed HEAD 發動(先 commit 再 review),期間不要弄髒 working tree。
- 一輪 adversarial review 約 5–15 分鐘;launch 指令本身可能同步準備超過 2 分鐘才回,是正常的。


### 非同步與取消

- 重構比對父提交時,「建構參數 byte-identical」不夠——資源/期限的 **scope 巢狀順序**也是行為(client 建構曾被移出 `asyncio.timeout` 範圍而漏計時)。
- `asyncio.timeout` 只在「真正暫停的 await」處取消;同步阻塞碼(如 TLS/trust-store 初始化)不可搶斷且卡 event loop——要受 deadline 管轄就必須 await 化。
- **取消 waiter ≠ 停止工作**:`run_in_threadpool` 的 waiter 被取消時 anyio 釋放容量 token、底層 thread 照跑,重試即無界長 thread。宣稱有界就要真的有界(單一常駐 daemon worker + queue;cancel-before-start 跳過、遲到結果安全丟棄)。

### 打包與依賴

- runtime 依賴一律要有下限,且下限=「gates 實跑過的版本」;裸依賴會讓遠古版本裝得起來、啟動才 ImportError(模組層 import 新 API 時尤其致命)。
- 驗證機制本身要先驗證:`uv pip install --resolution lowest-direct <wheel路徑>` 的 direct 只有 wheel 自己,floor 全被繞過、檢查形同虛設——要先 `--resolution lowest-direct -r pyproject` 釘 floor,再 `--no-deps` 裝 wheel。
- 文件是 release surface:backend README、`afterthread/env.example`、website `config-reference.md`、`PYPI_README.md` 必須同步;wheel 使用者看不到 repo 內的檔案。

### typer / CLI

- typer 單指令 app 會「塌縮」,加第二個 `@app.command()` 就改變裸指令語意。選項要搬 `@app.callback(invoke_without_command=True)`,並把**所有既有呼叫形式**(裸跑、頂層選項、`--version`、`--help`)全部釘測。
- typer vendor 了自己的 click fork:與 pip `click` 的 enum 比較**永遠 False**(guard 靜默失效)——跨庫 enum 一律用 `.name` 比對。
- 頂層選項在子指令前被解析後丟棄=靜默誤導,明確傳入的要 fail fast;serve-only envvar 的型別轉換不得擋住不相干的子指令(如陳舊 `AFTERTHREAD_PORT` 曾擋掉 `init-env --help`)。

### 檔案寫入(本 repo 既定 hardening 類別,前例 `services/tools.py`)

- 寫設定/敏感檔:symlink 一律拒絕(注意 `O_EXCL|O_NOFOLLOW` 對 symlink 是 **EEXIST 非 ELOOP**)、`os.write` 短寫要迴圈補完並拒絕零進度、0600 用 `fchmod`(`os.open` 的 mode 參數會被 umask 遮蔽)、發布走私有 mkstemp + 原子操作(不覆寫用 `os.link`、覆寫用 `os.replace`)。
- 失敗清理只碰自己的 mkstemp temp;**按路徑 rollback 已公開的 inode 會誤刪並行贏家剛寫好的檔案**。清理錯誤不得遮蔽主要結果(發布成功就回報成功,cleanup 失敗印警告即可)。

### review 迴圈方法

- 修「fix 的 fix」時,每輪先做 collateral 分析:這個修法**自身**引入了什麼(threadpool 修 preemption 就引入了 thread 洩漏)。
- 回歸測試先 RED 再 GREEN;即時完成的假物件測不出 scope 類回歸,「慢」要注入在正確階段(constructor ≠ `__aenter__`,兩者相對 timeout 的位置不同)。
- 新增 operator 可見行為(啟動警告等)必須有測試釘住,否則重構會無聲刪掉它。
- won't-fix 裁決要寫進程式註解或 decisions 文件,且下一輪 review 指示明示「除非證明推理錯誤,勿重提」——迴圈才會收斂而不打轉。

