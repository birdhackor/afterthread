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
