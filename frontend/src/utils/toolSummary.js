// Pure helpers for the 工具 page's AI-summary panel (D40): the sidecar's
// draft/final vocabulary and whether there is anything to freeze. A SIBLING of
// toolInstall.js rather than an extension of it -- this is genuinely new
// domain (the summary sidecar's status/content), not the install/job-polling
// flow that file already owns, and "toolInstall" would be a flatly wrong name
// for logic that has nothing to do with installing. Kept out of the component
// for the same reason as toolInstall.js: vitest runs these in node with no
// jsdom, so a pure function is the only unit-testable surface for this page's
// logic (see that file's own header comment).

// draft/final -> {label, color} for the summary-status Badge. Mirrors
// constants/labels.js's STATUS_META + StatusBadge shape (a Mantine
// label/color pair keyed by backend vocabulary), but lives here as a
// FUNCTION rather than an exported lookup object with its own Badge
// component: this phase's allowed files do not include constants/labels.js
// or a new components/ file, and a function gives the real, common `null`
// case (a tool with no sidecar yet, or one whose generation never ran/wrote
// anything) one explicit fallback arm instead of a silent lookup miss.
const SUMMARY_STATUS_META = {
	draft: { label: "草稿", color: "yellow" },
	final: { label: "已定版", color: "green" },
};

export function summaryStatusMeta(status) {
	return SUMMARY_STATUS_META[status] ?? { label: "尚無總結", color: "gray" };
}

// Whether a summary has any text worth freezing. Mirrors the backend's own
// finalize gate exactly (PATCH .../summary -> 409 summary_missing covers BOTH
// no sidecar and a sidecar whose summary is absent/empty -- one answer,
// because they are the same answer to the user; see routers.tools'
// `update_tool_summary_status` docstring). Gates ONLY the 定版 direction:
// 解除定版 (final -> draft) is deliberately UNCONDITIONAL on the backend --
// the one recovery path for a sidecar a bad hand-edit or a failed generation
// left with no usable text (docs/web-v4-decisions.md D40 addendum r6) -- so a
// caller must never run that direction's enablement through this function.
export function canFinalizeSummary(summary) {
	return typeof summary === "string" && summary.trim() !== "";
}
