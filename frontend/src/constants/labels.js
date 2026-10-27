// Single source of truth for the zh-TW status/stage vocabulary from the shared
// UX rules: display label + Mantine color per value, plus ready-made option
// lists for Select controls. Reused by the list filters and the StatusBadge
// primitive so the mapping is never duplicated.

// Ordered by the item lifecycle so Select options read naturally.
export const STATUS_META = {
	"capture-quick": { label: "快速捕捉", color: "gray" },
	"needs-enrichment": { label: "待補齊", color: "orange" },
	active: { label: "進行中", color: "green" },
	waiting: { label: "等待中", color: "blue" },
	parked: { label: "擱置", color: "violet" },
	done: { label: "完成", color: "dark" },
	superseded: { label: "已取代", color: "red" },
};

export const STAGE_META = {
	quick: { label: "快速" },
	full: { label: "完整" },
};

export const STATUS_OPTIONS = Object.entries(STATUS_META).map(
	([value, meta]) => ({
		value,
		label: meta.label,
	}),
);

export const STAGE_OPTIONS = Object.entries(STAGE_META).map(
	([value, meta]) => ({
		value,
		label: meta.label,
	}),
);

// Shown wherever an AI action is unavailable because the backend has no LLM
// endpoint configured: disabled-button tooltips, the shell banner, and the
// capture-page fallback alert. Kept as one constant so the wording never
// drifts between call sites. Names BOTH env vars llm_configured() actually
// requires (afterthread/services/llm.py) -- a base URL alone is a half-configured
// endpoint the backend still reports as unconfigured, so the notice must not
// imply setting only OPENAI_BASE_URL is enough. Deliberately deployment-agnostic
// ("後端" / "見 README") rather than naming backend/.env specifically: which
// file that is depends on dev vs packaged mode (see README) -- a packaged
// install's settings live in `<data-dir>/.env`, not backend/.env.
export const LLM_NOT_CONFIGURED_NOTICE =
	"AI 功能尚未設定：請在後端設定 OPENAI_BASE_URL 與 OPENAI_MODEL 後重啟服務（設定位置見 README）";

// Workflow -> zh-TW label, shared by the AI 日誌 page and the 工具 page's
// 最近執行 (a tool run records the workflow that called it). Mirrors every
// workflow name generate_structured is called with: the three memory ones
// (afterthread/services/memory_ai.py), the builder session shared by an install
// and an AI 修訂 (tool_builder._WORKFLOW), and the summary session that follows
// one or a 重新產生 (tool_meta._SUMMARY_WORKFLOW). The last two are SEPARATE
// names by decision (D40) -- two sessions of one job, split so their log links
// cannot collide -- so labelling only the memory three left every tool session
// reading 其他, which is exactly the distinction the split was made to
// preserve. An unrecognized value still falls back to 其他 rather than showing
// a raw English token.
export const WORKFLOW_LABELS = {
	capture: "快速捕捉",
	enrich: "AI 補齊",
	assist_update: "AI 進度更新",
	tool_install: "工具建置",
	tool_summary: "工具總結",
	// Not LLM workflows: the two ways the backend itself runs a tool's
	// examples.json (web-v6 §10), recorded as such on the tool's run records.
	health_check: "跑一次範例",
	verify: "修正後驗證",
	unknown: "其他",
};
