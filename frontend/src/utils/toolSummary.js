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

// --- react-query cache keys for one tool's summary ---------------------------

const TOOL_SUMMARY_KEY_ROOT = "tool-summary";

// EVERY cached summary entry for one tool name, whatever instance it belonged
// to. Used for the filter-shaped operations (invalidate after a revise,
// removeQueries after a delete), which are partial-prefix matches in TanStack
// Query -- so they must NOT pass `exact`, and must NOT be spelled by hand: this
// is provably a prefix of toolSummaryQueryKey below because that function is
// built FROM it.
export function toolSummaryKeyPrefix(name) {
	return [TOOL_SUMMARY_KEY_ROOT, name];
}

// The exact key for ONE tool INSTANCE's summary. `description` is an instance
// discriminator, not part of the address: the fetch URL only ever carries the
// name (the backend has no other way to name a tool), exactly as LlmLogsPage
// folds `started_at` into its `["llm-log", id, started_at]` key while fetching
// by id alone.
//
// Why a discriminator is needed at all: a tool NAME is reassignable. Another
// browser tab -- or any process with filesystem access -- can delete a tool and
// install a DIFFERENT one under the same name, and this QueryClient sees none of
// it (deleteMutation's removeQueries only covers deletions THIS client
// performed). Keyed on the name alone, the new tool's panel would open on the
// old tool's cached summary/status/AI-日誌 link.
//
// Why `description` specifically: GET /api/tools returns
// {name, description, enabled, valid, error, summary_status} per row (backend
// schemas.ToolSummary) and carries no install id or timestamp, so there is no
// true instance identity to use. Of those fields, `description` is the only one
// an INSTALL writes -- it comes from the package's tool.json, authored by that
// install's own AI builder session -- while `enabled`/`valid`/`error` say
// nothing about which package this is, and `summary_status` changes under
// ordinary use (定版/解除定版 would churn the key on a tool that never moved).
// It is a heuristic, not a proof: see the residual noted at this key's use site
// in ToolsPage, which is why the panel ALSO guards what it renders.
export function toolSummaryQueryKey(name, description) {
	return [...toolSummaryKeyPrefix(name), description];
}

// The same instance identity as a STRING, for the row's React `key`. Built FROM
// toolSummaryQueryKey rather than re-spelled, because the two are ONE question
// asked in two places: the cache key decides which summary body belongs to this
// row, the React key decides whether the row -- and the unsent revise feedback
// typed into it -- survives or is remounted. Keyed on the NAME alone, a row kept
// its identity across a same-name reinstall while its summary query correctly
// moved to a new entry: the user could type feedback for tool A, have a
// background refetch swap in tool B underneath, and submit that text against B.
// Whatever discriminator this project can prove later, both move together.
//
// JSON.stringify over an array of primitives is byte-for-byte what TanStack
// Query's own hashKey does to such a key (query-core utils.js line 85: a
// JSON.stringify whose replacer only sorts PLAIN OBJECT keys, of which there are
// none here), so two rows collide on the React key exactly when they would
// collide on the cache key -- including the residual where two installs produce
// byte-identical descriptions.
export function toolInstanceKey(name, description) {
	return JSON.stringify(toolSummaryQueryKey(name, description));
}

// --- the panel's OWN busy state ----------------------------------------------

// Everything THIS panel started and is still waiting on -- and deliberately
// NOTHING about the other tab. The 工具 page mirrors each tab's busy flag into
// the other as `externalBusy`; when the value reported UPWARD also contained the
// `externalBusy` it had just been handed, that mirror echoed: the install tab
// said busy during its own POST, the 工具 page fed that in here, this panel
// reported it straight back, and the install form told the user 「已安裝工具」
// had an AI job running -- during the user's own install submit, before any job
// id existed. A panel may only report what it knows first-hand; combining that
// with the other tab's flag is the PARENT's business, and is done at the local
// gate (summaryBusy) instead.
export function ownSummaryBusy({
	regeneratePending,
	revisePending,
	reviseJobActive,
}) {
	return Boolean(regeneratePending || revisePending || reviseJobActive);
}

// --- ["tools"] list-cache patch ----------------------------------------------

// Return the GET /api/tools body with ONE row's `summary_status` replaced, or
// the body untouched when there is nothing to patch. A pure updater for
// `setQueryData`, so the write is testable without a QueryClient.
//
// Deliberately narrow: it only ever REWRITES ONE FIELD OF AN EXISTING ROW. A
// name with no row is left alone rather than appended, because the other five
// fields of a tool row (enabled, valid, error, description...) are not knowable
// from a summary response and a fabricated row would be a shape GET /api/tools
// never produces. An unexpected body (undefined before the list has loaded, or
// anything without a `tools` array) is returned as-is for the same reason.
//
// This exists because 重新產生 and 定版/解除定版 both learn the new status
// authoritatively from their own response, while the row BADGE was left to
// `invalidateQueries(["tools"])` -- whose background refetch error TanStack
// Query swallows by default. One transient GET /api/tools failure was enough to
// leave a green 「已定版」 toast beside a row badge still reading 草稿. The
// invalidation stays as the eventual-consistency backstop for the rest of the
// row; this just stops the one field we already know from lagging behind its
// own success toast.
export function patchToolRowSummaryStatus(listBody, name, status) {
	if (!listBody || !Array.isArray(listBody.tools)) {
		return listBody;
	}
	return {
		...listBody,
		tools: listBody.tools.map((row) =>
			row?.name === name ? { ...row, summary_status: status } : row,
		),
	};
}
