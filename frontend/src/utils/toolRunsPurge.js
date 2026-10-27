// Pure helpers for purging run records (web-v6 §7): the scopes the page offers,
// the request body each one becomes, and the sentences around the preview.
// Nothing is ever deleted automatically -- every purge is the operator's call,
// made after a preview that says how many records it will remove.
//
// A scope is { tool?, outcome?, olderThanDays?, orphansOnly? }, every key
// narrowing. `outcome` is "ok" or "not_ok" (anything but a success, refusals
// included -- a refused call is no success either).

export const toolRunStatsQueryKey = ["tool-run-stats"];

// The 清除紀錄 menu of one tool's 最近執行 (web-v6 §7).
export const TOOL_PURGE_PRESETS = [
	{ key: "all", label: "全部紀錄", scope: {} },
	{ key: "ok", label: "只清成功的", scope: { outcome: "ok" } },
	{ key: "older30", label: "30 天前的", scope: { olderThanDays: 30 } },
];

export const PURGE_OUTCOME_OPTIONS = [
	{ value: "all", label: "全部" },
	{ value: "ok", label: "只清成功的" },
	{ value: "not_ok", label: "只清失敗與被拒的" },
];

export const PURGE_AGE_OPTIONS = [
	{ value: "", label: "不限時間" },
	{ value: "7", label: "7 天前的" },
	{ value: "30", label: "30 天前的" },
	{ value: "90", label: "90 天前的" },
];

const DAY_MS = 24 * 60 * 60 * 1000;

// The purge body for a scope, WITHOUT dry_run/max_id. `now` is taken once, when
// the dialog opens, and `before` is the absolute instant that follows from it:
// the preview and the confirmation must count the same records, and an age
// measured twice moves with the clock.
export function purgeRequestBody(scope, now) {
	const body = {};
	if (scope.tool) {
		body.tool = scope.tool;
	}
	if (scope.outcome && scope.outcome !== "all") {
		body.outcome = scope.outcome;
	}
	if (scope.olderThanDays) {
		body.before = new Date(
			now.getTime() - scope.olderThanDays * DAY_MS,
		).toISOString();
	}
	if (scope.orphansOnly) {
		body.orphans_only = true;
	}
	return body;
}

// What a scope covers, as label/value lines for the confirmation dialog -- a
// list reads plainly where one generated sentence stacks up 的.
export function purgeScopeLines(scope) {
	const outcome =
		PURGE_OUTCOME_OPTIONS.find(
			(option) => option.value === (scope.outcome ?? "all"),
		)?.label ?? "全部";
	const lines = [
		{ label: "工具", value: scope.tool ? `「${scope.tool}」` : "所有工具" },
		{ label: "結果", value: outcome },
		{
			label: "時間",
			value: scope.olderThanDays ? `${scope.olderThanDays} 天前的` : "不限",
		},
	];
	if (scope.orphansOnly) {
		lines.push({ label: "只限", value: "已刪除的工具留下的紀錄" });
	}
	return lines;
}

export function purgePreviewText(matched) {
	return matched > 0
		? `將刪除 ${matched} 筆紀錄，刪除後無法復原。`
		: "沒有符合條件的紀錄。";
}

export function purgeResultMessage(deleted) {
	return deleted > 0 ? `已刪除 ${deleted} 筆執行紀錄` : "沒有刪除任何紀錄";
}

// The 工具 page header line (web-v6 §9): how much is stored, and what of it
// is worth a look -- failures, and records of tools that no longer exist.
export function statsSummaryText(stats) {
	if (!stats || stats.total === 0) {
		return "還沒有工具執行紀錄";
	}
	const details = [`失敗與被拒 ${stats.not_ok} 筆`];
	if (stats.orphans > 0) {
		details.push(`已刪除的工具留下 ${stats.orphans} 筆`);
	}
	return `工具執行紀錄 ${stats.total} 筆（${details.join("；")}）`;
}
