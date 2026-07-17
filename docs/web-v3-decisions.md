# Web App v3 決策紀錄

接續 `web-v2-decisions.md`（D01–D29），本輪自 D30 起。格式相同：背景 → 選項與優缺點 → 決定 → 依據。
本輪原則（沿使用者指示）：平衡／保守／簡潔／易擴充；選錯會導致大改的才停下來問。

## D30：CLAUDE.md symlink 重建 + 進 .gitignore

- **背景**：使用者確認 `CLAIDE.md` 是打錯字（應為 `CLAUDE.md`），指示重做正確名稱的 symlink。
- **決定**：刪 `CLAIDE.md`、建 `CLAUDE.md -> AGENTS.md`（維持未追蹤，性質同前）；並把 `CLAUDE.md` 加入 `.gitignore`——v2 期間曾發生一次誤 commit（靠 pathspec 排除補救），gitignore 一行把這類事故從根本擋掉。
- **依據**：個人設定檔不屬 repo；gitignore 是比「每次 commit 記得排除」更結構性的防線。

## D31：v3 phase 規劃與研究先行

- **決定**：7 項需求映射為 P0–P7（見 `web-v3-plan.md`）。zensical、uv_build、loguru/LangChain 三題先派研究 agent（sonnet×3，唯讀），計畫中留「待研究裁決」欄位，報告落地後在對應 phase 開工前定案並記錄。
- **依據**：三題都是「事實決定方向」型（工具成熟度/能力邊界），先研究後裁決比先承諾後返工保守；研究與 P1（typer，無前置）並行不浪費牆鐘。

## D32（P2 裁決）：打包維持 hatchling，不遷移 uv_build

- **背景**：使用者條件「如果用 uv 做可以達成同樣的功能嗎? 可以的話就用 uv 取代」。研究 agent 以三重證據定案（uv 原始碼 `BuildBackendSettings` struct 窮舉、官方文件、scratchpad 實驗完整重現）。
- **事實**：四項硬需求中三項 uv_build 完全達成且更簡——(a) gitignored `static/**` 免任何設定自動進 wheel+sdist（`module-root = ""` 一行即可，兩段式建置無 D02 問題）、(c) console script（PEP 621 標準，與 backend 無關）、(d) flat layout 原生支援。**(b) 驗證 hook 無等效**：uv_build 刻意不支援任何 build hook/script（struct 無此欄位；官方文件明言 "when build scripts are required, consider using the hatchling build backend instead"）；實驗實測缺 static 時 `uv build` **exit 0、"Successfully built"、無任何警告**——正是 v2 D24-F4 裁決要修、`hatch_build.py` 存在的理由（該 hook 的設計對象就是「不照文件走、繞過腳本的人」，把檢查移進 build-wheel.sh 只防得到用腳本的人，邏輯循環）。
- **決定**：**維持 hatchling**。遷移收益（設定少兩行）遠小於代價（把已關閉的「裸 build 靜默壞 wheel」class 重新打開；P6 將引入 GitHub Actions，繞過腳本直呼建置指令的風險面只增不減；hatchling 預設 VCS-based 檔案選擇對「誤把雜物打進 wheel」另有一層天然防護，uv_build 的「module 目錄全收」反而少這層）。
- **依據**：使用者自己設的條件是「功能等同」，事實為不等同；「修類不修例」準則下不把 class-level 保證降級為 instance-level。若日後情境改變（接受該降級），研究報告已留完整遷移設定可直接取用。

## D33（P6 預裁決）：zensical 採用 + Actions artifact 部署；alpha 風險與 CJK 搜尋列為驗收條件

- **背景**：研究確認 zensical v0.0.50（alpha、Material for MkDocs 團隊的正式後繼者、近月活躍發版）。官方 FAQ：所需功能已支援即可正式使用；本案所需（admonitions、mermaid、code highlight、zh-Hant UI 翻譯 Complete、明暗主題）全部**預設開啟**。歷史 CJK bug（粗體、路徑、autoreload）皆已修。
- **決定**：
  1. **採用 zensical**（使用者指名；alpha 風險屬使用者選擇，caveat 記錄在案）；全新建站走原生 `zensical.toml`（官方「既有 MkDocs 專案勿改寫」的告誡不適用於新站）。
  2. **部署走官方唯一記載路徑：GitHub Actions artifact 模式**（`actions/deploy-pages`），不用 gh-pages 分支（官方文件完全未載、需額外工具）。workflow 依官方 YAML（action 版本已逐一驗證為當前最新），觸發限 main/master push——**在 feat/web-app 上 commit workflow 不會觸發任何部署**，實際發佈時點完全由使用者掌控（merge + 手動開 Pages）。
  3. **驗收條件**：CJK 搜尋分詞是官方文件唯一未回答的空白（Disco 新引擎、無 issue 討論）——P6 必須以真實 zh-TW 內容本機實測搜尋品質，結果記錄；不佳則記錄 workaround 或降級方案。
  4. **CI 不做 build cache**（官方明言 caching 將大改 + #641 non-deterministic builds 仍 open）。
  5. **手動設定清單**必須含：Pages Source 選 GitHub Actions；**private repo 的 Pages 需付費方案、且發佈內容對外公開**（本 repo 現為私人——公開文件站等於把站內內容公開，使用者決定 merge/開關前需知情）；workflow 權限；uv symlink link-mode 不相容註記。
- **依據**：使用者指名工具 + 官方路徑優先 + 發佈時點保留給使用者；唯一技術未知（CJK 搜尋）轉為明確驗收步驟而非假設。

## D34（P3 裁決）：不採用 loguru；JSONL sink 以 ~20 行大小檢查做輕量 rotation

- **背景**：使用者問「log 用 loguru 取代是否會更好? 如果你認為是好選擇，那就安排」——判斷權交給主代理。研究（context7 + loguru 原始碼 + GitHub issues 逐一查證）結論：
  - loguru 拿不走的：in-memory ring、`_apply_total_budget`、`_stored_body` UTF-8 閘與截斷、read API——全是應用層資料結構，一行都省不了；INFO 摘要行本來就一行。
  - 唯一真實收益：JSONL 檔的 rotation/retention/compression。但 `serialize=True` 會把我們的 JSON 包進 loguru 自己的 Record envelope（operator 直接讀檔反而更難）；正確用法是 `format="{message}"` 純訊息落檔——即便如此，仍需注意 loguru FileSink 常開檔案（現行逐次開關是刻意的 operator-robust 選擇，需 `watch=True` 補償）。
  - 風險有案可查：`enqueue=True` 與 async/fork 的鎖死 issues（#231/#906/#1335；#836 維護者自承文件不足）——本 app 正是 ASGI + 可能 `--reload`/多 worker 的型態；不開 enqueue 則現行同步寫入本來就每互動一行、量小頻低，無實際痛點。
- **決定**：**不採用 loguru**。JSONL sink 加輕量 rotation：寫入前 `os.path.getsize` 超過門檻（新設定 `llm_log_file_max_bytes`，預設 50MB）就把現檔 rename 成時間戳後綴再開新檔（~20 行、零依賴、保留逐次開關的 robust 語意）；此為 P3 的 loguru 替代交付。
- **依據**：對「secret-free 為 test-pinned 硬不變量」的模組，新依賴舉證責任高；收益（rotation）有零依賴等價物；有記錄的邊界風險不值得為格式統一而背。

## D35（P5 裁決）：不用 LangChain 重構 tool-calling 迴圈；交付機制說明文件

- **背景**：使用者問「用 langchain 串會更好更簡潔嗎? 考慮一下, 如果會的話重構」。研究（LangChain v1.0 GA 2025-10、`create_agent`+middleware 世代、依賴樹、社群遷出訊號皆查證）結論：
  - **直接衝突**：F2「逐 call deadline skip」前提是一輪內工具**依序**啟動；LangGraph `ToolNode` 對同一回覆的多個 tool_calls 預設**並行**派發——保留此不變量必須換掉框架內建路徑、自寫序列化 node，等於把現有迴圈原樣搬進框架殼。
  - **不變量搬家不消失**：進場雙上限（64 筆/256KiB 在任何 O(N) 工作前）、恰好一次修正重試、secret-free 例外分類（`langchain-openai` 底層仍是同一個 openai SDK、同樣的例外洩漏面，框架不消毒、反而多一層要稽核）、recorder 崩潰安全——每條都要在 `wrap_model_call`/`wrap_tool_call` 重刻，行數不減。
  - **用不到的優勢**：多 provider 熱插拔、checkpoint/persistence、human-in-the-loop、per-node streaming、LangSmith——本案單 provider 單 model，一條都不成立。
  - **穩定度**：兩年內 agent API 換過兩個世代（AgentExecutor→create_agent），v1「no breaking until 2.0」承諾距今僅 ~9 個月。
- **決定**：**不重構**。item 6 交付改為：現行機制的淺顯說明文件（OpenAI tool_calls 協定手作迴圈、各資源邊界的存在理由、recorder、subprocess runtime），收錄進 P6 文件站；本 D35 記錄評估全程。
- **依據**：「更好更簡潔」在事實上不成立——是把量身打造的安全不變量換成框架不透明性；唯一重新考慮情境（第二個異協定 provider、LangSmith 可觀測性需求）出現時再另案評估。

## D36（P3 設計）：金鑰處理三件套——已知值遮蔽 + 安裝表單秘密欄位 + JSONL rotation

- **背景**：使用者要求「log 會記錄金鑰的部分，思考簡易的處理方式」。v2 P6 文件已誠實揭露的洩漏面：金鑰貼在安裝 instructions → 逐字進所有 attempt 的 prompt 紀錄；工具呼叫參數 200 字元預覽可含金鑰；裝好的工具 .env 值可能經工具輸出回聲進紀錄。
- **選項**：
  1. **只做 pattern 遮蔽（sk-.../Bearer...啟發式）**：實作最小，但任意格式的內部 KB key 攔不到，假陰性高。
  2. **只做已知值遮蔽**：能遮自家 key 與已裝工具 .env 值，但「安裝當下貼在 instructions 的 key」在寫進 .env 之前不是已知值——主要洩漏面反而攔不到。
  3. **已知值遮蔽 + 安裝表單秘密欄位（採用）**：表單新增選填 `secret_name`/`secret_value`；值註冊進遮蔽集合（安裝中即生效）、注入 run_shell 子行程 env 供 LLM 實測 API（**LLM 全程看不到值**）、promote 時由後端直接寫進工具 `.env`（LLM 被明示不要自己寫/不要 echo）。金鑰從頭到尾不進 LLM 對話，遮蔽只是縱深防禦的第二層。
- **決定**：選項 3 + JSONL rotation（D34 的 20 行方案，`llm_log_file_max_bytes` 預設 50MB、超限 rename 時間戳後綴）。分層紀律：llm_log 是觀測葉模組、不得 import tools——以 `set_secret_provider(callable)` 反轉依賴（main.py 接線），provider 失敗絕不破壞記錄（no-observer-failure 不變量）。遮蔽在儲存 choke point、於 utf8-safe 與截斷**之前**（跨截斷邊界的金鑰不得半存活）；長度 <6 的值不遮（避免碎化一般文字）。
- **已記錄殘餘限制**：使用者若仍把 key 貼在 instructions（不用欄位），且該值不匹配任何已知秘密，仍會被記錄——文件引導用欄位是第一線，遮蔽是已知值的兜底。
- **第二輪對抗式複審（fd5acba 後）**：1 High + 7 Medium，裁決 7 修 1 won't-fix：
  1. **H1 秘密經 run_shell echo 進 live 對話再流入 summary/job state**（recorder 只遮快照不改 live messages）→ 修類：tools.py 共用 `redact_known_secrets()` 套在 runtime 工具輸出、builder 全部 meta-tool 輸出（進對話前遮，模型只看得到遮蔽標記）、InstallOutcome summary/error 存 job 前。
  2. **422 經 Pydantic `input` 回射 secret_value** → app 層 RequestValidationError handler 全域遞迴剝除 `input`（防未來任何敏感欄位的整類修法）。
  3. **schema 允許 <6 字元但遮蔽器跳過** → schema 對齊 ≥6 + FE 鏡像。
  4. **重疊秘密遮蔽順序**（先短後長留尾巴）→ 長度遞減排序替換（兩處遮蔽器皆改）。
  5. **provider no-raise 未涵蓋迭代/非字串** → `list(provider())` 進 try + isinstance 過濾。
  6. **尾端換行被 strip 而非 422 — won't-fix**：剪貼簿尾端換行是貼上偽影；strip-then-validate（內部換行仍 422）是刻意契約——正規化在 schema 單點發生、env 注入/.env 寫入/遮蔽集合看到同一值，無不一致風險。註解明示。
  7. **.env 只換第一個同名行**（dotenv last-wins 讓模型寫的重複行蓋掉真值）→ 移除全部同名行（含 `export ` 前綴與空白容忍）再附加唯一真值行。
  8. **rotation 秒級後綴 + 無序列化互相覆蓋 segment** → 專用 `_FILE_SINK_LOCK` 序列化整段 stat→rotate→append（刻意不用 ring lock，I/O 不進 ring 臨界區）+ rename 目標存在時遞增後綴。
- **第三輪複審（900c04f 後）**：F2–F5/F7/F8 確認成立；出 3 High + 2 Medium，共同根因是**「截斷先於遮蔽」碎片類**與兩個獨立向量，全修：
  1. **H1 reader 層 cap+1 截斷在遮蔽前**（跨界秘密留前綴碎片進 live 結果）＋ **H2 summary 2000 切片在 outcome 遮蔽前** ＋ **M4 args 200 預覽在 recorder 遮蔽前** → 修類：(a) 兩個遮蔽器（tools/llm_log）都加「尾端前綴碎片防護」——文字結尾若為任一秘密的 ≥6 字元前綴即遮（pre-truncated 輸入的通用兜底）；(b) `_sanitize` 遮蔽先於切片；(c) llm.py 增 `set_tool_args_redactor` hook（main.py 接線、llm 不 import tools 維持分層），args 於預覽切片**前**遮——內部碎片不形成。
  2. **H3 builder 可把 `$SECRET` 展開進 tool.json description/parameters**（未遮、經 /api/tools 與未來每次 LLM tool spec 持久外流）→ `validate_package` 拒絕 raw manifest 含任何已知秘密值（≥6）的套件；拒絕優於遮蔽——嵌金鑰的 manifest 是畸形資料非待清理文字。
  3. **M5 .env 寫入未引號、dotenv 解析可變形**（註冊的是 parsed 值、runtime 工具 echo raw 行可洩原始值）→ 寫入時安全引號 + **dotenv 往返驗證**（寫後 parse 比對不等即拒裝），整類消滅。
