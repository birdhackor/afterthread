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
## codex review loop 操作教訓(P8–P10 實測)

- **launcher 行程就是 turn 驅動器**:`adversarial-review --background` 的 node(codex-companion.mjs)行程一死,server 端 job 不會自己跑完——job log 直接凍結成孤兒(實例:一次凍在 starting、一次凍在 verifying 中的 pytest)。「--background」不代表 launcher 可以死。
- **啟動方式**:Claude Code 的背景任務會被系統中止(同 session 內兩度殺掉 launcher),所以 review 一律用 `setsid nohup node <codex-companion.mjs> adversarial-review --background … > log 2>&1 < /dev/null &` 完全脫離行程樹啟動。
- **等待方式**:不要用 `--wait` 串流(中繼不可靠,舊教訓)。輪詢 `status --all` 等 job 離開 running,並搭配 stall 偵測:job log(`plugins/data/codex-openai-codex/state/<repo>/jobs/*.log`)mtime 超過 240 秒仍 running 就是凍死,別再等。Claude Code 內用 Monitor 工具跑這個迴圈(前景 sleep 會被擋、背景 bash 會被殺)。
- **取結果**:`result` 只對 finished job 有效;job 還在跑時會回「No job found」——那不是 job 不見了,是還沒完成。
- **孤兒清理**:凍結的 job 用 `cancel <job-id>` 清;被取消的 job 可能留下 bwrap sandbox 行程(pytest 等卡在裡面),每輪結束後 `pgrep -af codex-linux-sandbox` 檢查、照 PID kill。
- **review 期間**:以 committed HEAD 發動(先 commit 再 review),期間不要弄髒 working tree。
- **迴圈收斂**:每輪 prompt 要附「已修了什麼(commit)、已裁決接受什麼與理由」,won't-fix 寫進程式註解或 decisions 文件,並明示「除非證明推理錯誤,勿重提」——否則迴圈會打轉。
- 一輪 adversarial review 約 5–15 分鐘;launch 指令本身可能同步準備超過 2 分鐘才回,是正常的。
