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
// The row is addressed by the INSTANCE identity (toolInstanceKey), NOT by name.
// One summary response drives TWO cache writes -- the detail entry and this row
// badge -- and they must name the same tool or the page shows a mixture no
// request was wrong about. Keyed by name, a response for instance A landing
// after a same-name reinstall stamped A's status onto B's row while the detail
// write (which has always been instance-keyed, see toolSummaryQueryKey) went to
// A's entry: the badge and the panel then disagreed permanently, with no error
// anywhere. Taking the identity as a STRING built by toolInstanceKey -- rather
// than re-deriving `row.description === description` here -- is what makes the
// two consumers provably the same question: if the discriminator ever changes,
// both move with it or neither does.
//
// Deliberately narrow: it only ever REWRITES ONE FIELD OF AN EXISTING ROW. An
// identity with no row is left alone rather than appended, because the other
// five fields of a tool row (enabled, valid, error, description...) are not
// knowable from a summary response and a fabricated row would be a shape
// GET /api/tools never produces. An unexpected body (undefined before the list
// has loaded, or anything without a `tools` array) is returned as-is for the
// same reason -- which also makes this updater write-only-if-present in
// TanStack Query's own terms (returning the same `undefined` it was handed
// makes setQueryData bail before building an entry; see
// writeSummaryDetailIfPresent for the citation).
//
// This exists because 重新產生 and 定版/解除定版 both learn the new status
// authoritatively from their own response, while the row BADGE was left to
// `invalidateQueries(["tools"])` -- whose background refetch error TanStack
// Query swallows by default. One transient GET /api/tools failure was enough to
// leave a green 「已定版」 toast beside a row badge still reading 草稿. The
// invalidation stays as the eventual-consistency backstop for the rest of the
// row -- and for THIS field too whenever no row matches the identity; this just
// stops the one field we already know from lagging behind its own success toast
// for the rows we can prove we are talking about.
export function patchToolRowSummaryStatus(listBody, instanceKey, status) {
	if (!listBody || !Array.isArray(listBody.tools)) {
		return listBody;
	}
	return {
		...listBody,
		tools: listBody.tools.map((row) =>
			toolInstanceKey(row?.name, row?.description) === instanceKey
				? { ...row, summary_status: status }
				: row,
		),
	};
}

// A `setQueryData` updater that writes `detail` ONLY IF the entry already holds
// data -- never one that CREATES it.
//
// Verified against the installed @tanstack/query-core 5.101.2 rather than
// assumed: `queryClient.setQueryData` reads `prevData = query?.state.data`,
// runs the updater through `functionalUpdate` (utils.js lines 6-8: a function
// updater is CALLED with that previous value), and then -- lines 99-101 of
// build/modern/queryClient.js -- `if (data === void 0) return void 0;` BEFORE
// `queryCache.build(...)` on line 102. So returning `undefined` from the
// updater is query-core's own "write only if present": no entry is built, no
// observer is notified.
//
// Why it must not create: deleteMutation clears this tool's entries with
// removeQueries, but it cannot un-send a regenerate/定版 request already on the
// wire. That response then arrived at a plain `setQueryData(key, detail)`,
// which RESURRECTED the deleted tool's detail entry -- and a same-name,
// same-description reinstall inside the 5-minute gc window would open its panel
// on it.
//
// No legitimate write is lost to this. A CREATING write would need the buttons
// to be pressable while the entry has no data, and they never are: the panel's
// query is `enabled: expanded`, and a COLLAPSED row's panel body sits inside the
// hidden React `Activity` that Mantine 9.4.1's Collapse wraps children in at
// keepMounted default (esm/components/Collapse/Collapse.mjs -- `mode: isExited ?
// "hidden" : "visible"`), so it is rendered but not reachable; the instant it
// IS expanded the query fetches and the panel renders a Loader rather than the
// buttons while `data === undefined`. By the time a button can be pressed, its
// row's entry holds data.
export function writeSummaryDetailIfPresent(detail) {
	return (previous) => (previous === undefined ? undefined : detail);
}

// --- write ordering: last write by ISSUE time wins ---------------------------

// 重新產生 (POST .../summary/regenerate) and 定版/解除定版 (PATCH .../summary)
// can be in flight at the SAME time by design -- the 定版 button is
// deliberately exempt from the busy gate so an operator can freeze a tool to
// stop an AI iteration mid-flight (D40 P4 r1; the backend supports this on
// purpose, re-checking finalization at write time). Two concurrent writes to
// one cache entry means arrival order decides what the cache ends up holding,
// and the loser is silent: both requests SUCCEEDED, so no banner and no toast
// says the panel is now showing the older of two answers while the row badge
// (refreshed by the trailing list invalidation) shows the newer one.
//
// Serializing them was the other option and is ruled out by that same D40 rule:
// a shared in-flight flag gating BOTH is exactly the lock the backend was built
// not to need, and it would take the escape hatch away at the one moment it
// exists for. So the writes stay concurrent and are ORDERED instead: each takes
// a monotonic stamp when it is issued, and a response is applied only if no
// strictly newer write for the same instance has been applied already.
//
// Same shape as `atoms/llm.js`'s module-level generation counter (and the
// requestId pattern the list pages use): claim at request start, re-check
// before writing. The ledger is per-panel state (a ref) rather than module
// state because the stamps are only ever compared within one panel's own
// writes; `applied` is keyed by toolInstanceKey, so two different tools never
// order each other, and it grows only with the number of distinct instances a
// session actually writes to.
export function createSummaryWriteLedger() {
	return { issued: 0, applied: new Map() };
}

// Claim the next stamp. Called synchronously from the mutation's `onMutate`,
// which query-core invokes before `mutationFn` (mutation.js: `await
// this.options.onMutate?.(...)` precedes `this.#retryer.start()`), so stamps
// are in the wall-clock order the user pressed the buttons -- not the order the
// responses came back.
export function nextSummaryWriteStamp(ledger) {
	ledger.issued += 1;
	return ledger.issued;
}

// May this response still be written for `instanceKey`? Records the claim when
// it may. STRICTLY older writes are refused; re-asking with the SAME stamp
// always answers the same thing, because the caller has to ask twice: once
// before `cancelQueries` (so a stale response never cancels the fresh read a
// newer write just started) and once after that await (because a newer response
// can land inside it, and whichever cancel settles last would otherwise write
// last).
export function claimLatestSummaryWrite(ledger, instanceKey, stamp) {
	const applied = ledger.applied.get(instanceKey);
	if (applied !== undefined && stamp < applied) {
		return false;
	}
	ledger.applied.set(instanceKey, stamp);
	return true;
}

// --- which failures prove the server state moved -----------------------------

// Does this failed summary mutation prove the server is no longer what the
// cache says it is? A `true` means refetch the summary AND the list; a `false`
// means the failure says nothing about server state and a refetch would be
// noise (and would hide, not help -- a 502 storm refetching on every retry).
//
// The row badge is never separated from the summary here: `summary_status` on a
// GET /api/tools row and `status` in the sidecar detail are the same sidecar
// field read twice (backend services/tools._narrowed_summary_status vs
// routers/tools._summary_detail), so anything that proves one stale proves the
// other.
//
// The three that PROVE it, each checked against the route that can raise it
// (backend routers/tools.py):
//
// * 404 -- every summary route resolves the package first and answers the same
//   fixed 404 when it is gone. The tool this panel is showing does not answer
//   to that name any more; the copy tells the user the list may be stale, and
//   this is what makes that remedy real instead of advice.
// * 409 `tool_finalized` (regenerate, revise) -- raised only when the sidecar
//   is ALREADY 已定版. Our controls were enabled, so our cached detail said
//   otherwise: somebody else finalized it. The error names 解除定版 as the
//   remedy, and the 解除定版 button only appears once the panel knows the tool
//   is final -- so without this refetch the prescribed remedy is not reachable.
// * 409 `summary_missing` (PATCH .../summary) -- raised only when there is
//   nothing to freeze. 定版 is enabled by canFinalizeSummary over the CACHED
//   text, so this refusal is proof the cached text is gone.
//
// Deliberately NOT refetching, with the reason each time:
//
// * 409 `tool_job_in_progress` -- someone holds the single-flight slot. A job
//   that has not finished has written nothing; there is nothing new to read.
// * 503 `llm_not_configured` / 502 `llm_upstream_error` -- the regenerate never
//   reached the sidecar (routers/tools.py raises both from the LLM call, before
//   `_store_meta`). Configuration and upstream health are not tool state.
// * 0 (transport) / 5xx / anything else -- no evidence either way. Blanket
//   refetching on every error is how a flaky link turns one failure into a
//   refetch loop, and the two banners already say the view may be stale.
export function summaryErrorRevalidates({ status, code } = {}) {
	if (status === 404) {
		return true;
	}
	return (
		status === 409 && (code === "tool_finalized" || code === "summary_missing")
	);
}
