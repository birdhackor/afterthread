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
