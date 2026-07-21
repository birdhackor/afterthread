# Web App v3 計畫（2026-07-17 起）

起始點：`a5ac409`（v2 收尾推送點）。使用者需求 7 項 + 流程指示。決策紀錄接續 `web-v3-decisions.md`（D30 起）。

## 需求對照

1. **CLAIDE.md → CLAUDE.md symlink 修正** → P0（已完成：重建正確名稱 symlink，維持未追蹤；.gitignore 加一行防未來誤 commit）
2. **CLI 改用 typer 重構**（取代 argparse） → P1
3. **日誌記錄金鑰的簡易處理 + loguru 評估/重構** → P3（研究進行中；loguru 是否採用由研究結果裁決）
4. **各種 md 資訊 → zensical 文件站 + GitHub Pages（分支/Actions 部署）+ 手動設定清單** → P6（研究進行中）
5. **hatchling → uv 打包**（若功能等同） → P2（研究 + 實驗進行中；若不等同則記錄理由維持 hatchling——使用者原話「可以的話就用 uv 取代」）
6. **tool-call 實作方式說明 + LangChain 評估**（若更好更簡潔則重構） → P5（研究進行中；評估結論記入決策）
7. **動態 char↔token ratio、改用 token 卡預算** → P4

## Phase 順序與派工

| Phase | 內容 | 模型 | 狀態 |
|---|---|---|---|
| P0 | 計畫/決策文件 + symlink 修正 + .gitignore | 主代理 | ✅ 推送 69483d8 |
| P1 | CLI typer 重構 | sonnet | 實作中 |
| P2 | 打包 uv_build 評估 → **裁決維持 hatchling（D32），無程式變更** | 研究+主代理 | ✅ 已裁決 |
| P3 | 日誌金鑰遮蔽 + JSONL 輕量 rotation（**loguru 不採用，D34**） | opus（設計敏感） | 待 P1 |
| P4 | 動態 token-ratio 預算 | opus（LLM 邊界 invariant） | 待 P3 |
| P5 | tool-call 機制說明文件（**LangChain 不重構，D35**） | sonnet 文件 | 待 P4 |
| P6 | zensical 文件站 + Actions artifact 部署（D33） | sonnet | 待 P5 |
| P7 | 全量 review（vs `a5ac409`）+ 總結 + 手動設定清單 | 主代理 | 待全部 |

## 流程（沿 v2，D23/D27/D28 慣例）

1. 主代理擬 spec → subagent 實作（haiku 瑣碎／sonnet 標準／opus invariant 密集）。
2. 主代理親自看 diff + 跑 gates（backend：ruff/format/ty/pytest；frontend：lint/vitest/build；e2e smoke；打包相關加 wheel_smoke）。
3. 自我審查：修類不修例（程式碼與文件皆然，全面 sweep + grep 驗殘留）。
4. Commit（phase 內不 push）。
5. codex review 只審該 phase 範圍（＝compare to origin，因前 phase 已推送）→ 合理即修 → 再審 → 直到 approve 或僅剩裁決過的 won't-fix。
6. push origin feat/web-app，進下一 phase。全部完成後對 `a5ac409` 全量 review + 總結報告（含使用者手動設定清單）。

## 各 phase 設計要點（隨研究/實作更新）

### P1 typer
- 依賴：`typer`（不帶 rich extra，保持精簡）。console script 入口不變（`afterthread.cli:main`）。
- 必須保留的行為契約（wheel_smoke 端對端釘住）：`--host`/`--port`/`--data-dir`/`--version`；env fallback `AFTERTHREAD_HOST/PORT/DATA_DIR`（typer 原生 `envvar=` 支援，取代手寫 `_int_env`）；`--port 0`（uvicorn 自選埠）；data-dir 在 chdir 前 resolve；新建才 chmod 0700；chdir → load_dotenv(override=False) → DATABASE_URL/TOOLS_DIR 注入；banner 兩事實（data dir、db 路徑）+ flush、絕不印 DATABASE_URL/秘密。
- helper（`_sqlite_url`/`_default_data_dir`）與其測試不動。

### P2 uv_build（待研究裁決）
- 硬需求：(a) gitignored `static/**` 進 wheel+sdist 且 `uv build` 兩段建置可用；(b) 等效驗證 hook（裸 `uv build` 缺 static 必須大聲失敗、editable 豁免）；(c) console script；(d) flat layout。
- 若 (b) 無等效（uv_build 刻意不支援 hook），評估「檢查移進 build-wheel.sh」是否可接受——注意 D02/F4 歷史：裸 build 靜默出壞 wheel 正是當初加 hook 的原因。不等同→記錄理由維持 hatchling。

### P3 日誌金鑰 + loguru（待研究+設計）
- 金鑰遮蔽方向（設計時定案）：「已知值遮蔽」——遮蔽集合 = 自家 `openai_api_key` ∪ 已裝工具 `.env` 值 ∪（若採）安裝表單新增的秘密欄位值；在 llm_log 儲存 choke point 替換為遮蔽標記。安裝時期的主要洩漏（金鑰貼在 instructions 裡）考慮「表單秘密欄位 + 佔位符 + promote 時代入 + 秘密值注入 run_shell env 供實測」組合；規模若過大則先做已知值遮蔽 + 文件引導（金鑰放秘密欄位不放 instructions）。
- loguru：僅在研究確認淨收益（JSONL rotation/retention、組態簡化）超過依賴+攔截複雜度時採用；「部分採用」（只換 app logger + 檔案 sink，ring 不動）為預設傾向。

### P4 token-ratio 預算
- 資料源：每次 attempt 已記錄 usage（prompt_tokens）與 request_chars（llm_log 現成）。
- 估計器：in-memory 滾動視窗（近 N 次有 usage 的 attempt），ratio = Σtokens/Σchars，夾在合理範圍，冷啟動用保守預設（CJK 最壞 ≈1 token/char）。
- 卡點改 token 計價：prompt budget 與 tool conversation budget 改以 token 預算設定，經 ratio 換算字元允許量；char 上限作絕對兜底或全面替換——設計時定案並記錄。

### P5 tool-call 說明 + LangChain 裁決（待研究）
- 說明文件：現行機制（OpenAI tool_calls 協定手作迴圈 + 各資源邊界 + recorder + subprocess runtime）寫成淺顯說明（供 P6 文件站收錄）。
- LangChain：以「是否更好更簡潔且不犧牲七輪 review 換來的 invariant」為準繩裁決；結論與理由記入決策（傾向不重構，待研究佐證後定案）。

### P6 zensical 文件站（待研究）
- 內容來源：根 README、methodology、v2/v3 決策與總結、backend/frontend/e2e README、P5 的 tool-call 說明——改寫成淺顯易懂的站式結構（非直接搬檔）。
- 部署：GitHub Actions + Pages（分支或 artifact 模式依研究建議）；完工後彙整使用者手動設定清單（Pages 開關、來源、權限）。

## 非目標（本輪不做）

- 不引入 tokenizer 依賴（item 7 明示用 ratio 近似）。
- 不做 chat UI。
- 不併 main（等使用者裁決）。
