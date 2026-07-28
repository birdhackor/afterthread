// Pure helpers for the 工具 page's AI-summary panel (D40). A SIBLING of
// toolInstall.js rather than an extension of it -- this is genuinely new
// domain (the summary sidecar's content), not the install/job-polling
// flow that file already owns, and "toolInstall" would be a flatly wrong name
// for logic that has nothing to do with installing. Kept out of the component
// for the same reason as toolInstall.js: vitest runs these in node with no
// jsdom, so a pure function is the only unit-testable surface for this page's
// logic (see that file's own header comment).
//
// The last section is the one thing here the 工具 page does not own alone: the
// AI 日誌 deep link it hands out, and the AI 日誌 page's rule for resolving one.
// Both halves live together deliberately -- see that section's header.

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
// old tool's cached summary/AI-日誌 link.
//
// Why `description` specifically: GET /api/tools returns
// {name, description, enabled, valid, error} per row (backend
// schemas.ToolSummary) and carries no install id or timestamp, so there is no
// true instance identity to use. Of those fields, `description` is the only one
// an INSTALL writes -- it comes from the package's tool.json, authored by that
// install's own AI builder session -- while `enabled`/`valid`/`error` say
// nothing about which package this is.
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
// removeQueries, but it cannot un-send a regenerate request already on the
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

// --- which failures prove the server state moved -----------------------------

// Does this failed summary mutation prove the server is no longer what the
// cache says it is? A `true` means refetch the summary AND the list; a `false`
// means the failure says nothing about server state and a refetch would be
// noise (and would hide, not help -- a 502 storm refetching on every retry).
//
// The two that prove the world moved are checked against the routes that can
// raise them:
//
// * 404 -- every summary route resolves the package first and answers the same
//   fixed 404 when it is gone. The tool this panel is showing does not answer
//   to that name any more; the copy tells the user the list may be stale, and
//   this is what makes that remedy real instead of advice.
// * 409 `tool_job_in_progress` -- ANOTHER actor holds the single-flight slot,
//   which is a fact about the world we did not know a moment ago: something
//   outside this page is mid-operation on this backend, and when it finishes it
//   may have replaced the very package we are looking at. This one used to be
//   in the list below, on the ground that "that job has not written anything
//   yet" -- true of the job, and beside the point (D40 P4 r8 inverted it and
//   recorded why). Revalidating does not make the foreign job observable -- we
//   hold no id for it and cannot be told when it ends -- so this only re-reads
//   the CURRENT truth. Acting on a foreign job's COMPLETION is the residual
//   family bounded by 裁決紀錄 #7 (a name-addressed API plus no instance
//   identity in GET /api/tools).
//
// Deliberately NOT refetching, with the reason each time:
//
// * `llm_not_configured` / 502 -- the call failed BEFORE anything could reach
//   the sidecar, so the cache is exactly as right (or wrong) as it was;
// * 5xx and transport failures -- no evidence about server state at all, and a
//   storm of them would refetch on every retry, hiding rather than helping.
export function summaryErrorRevalidates({ status, code } = {}) {
	if (status === 404) {
		return true;
	}
	return status === 409 && code === "tool_job_in_progress";
}

// --- the AI 日誌 deep link (R9-1) --------------------------------------------
//
// Both halves of ONE contract: the 工具 page BUILDS the link (logLinkSearch) and
// the AI 日誌 page RESOLVES it (deepLinkTarget). They live together, in the one
// file of this pair that vitest can reach, because they are a single agreement
// about what `?log=` means -- kept apart, the page that emits a claim and the
// page that acts on it would be free to drift, and only one of them would be
// covered. LlmLogsPage imports its half from here for that reason.
//
// What the claim is for: llm_log ids are a per-process counter over a ring that
// dies with the process, so id 5 today and id 5 from a previous backend run are
// unrelated interactions that share a number. A tool job's outcome card holds
// its `llm_log_id` in a react-query entry that is never refetched once the job
// is terminal, so a tab left open across a restart can still offer a link to an
// id this backend has since re-issued. `logProcess` is what lets the receiving
// page notice, and it is the same `llm_log_process` token the summary sidecar
// has stored beside its own id since D40 overall-r2.

// The `search` object for a 查看 AI 日誌 Link: `{}` (a plain jump to the page),
// `{log}`, or `{log, logProcess}`.
//
// No id -> no `?log=` at all, unchanged: a job whose session never started has
// nothing to point at, and the link is still worth offering as a jump.
//
// An id WITHOUT a token stays a bare `?log=` and is resolved the way it always
// was. That is not an oversight: the only caller that has no token is the
// summary panel, whose id comes from GET /api/tools/{name}/summary -- and the
// backend has nulled that field since overall-r2 unless the id was minted in the
// process ANSWERING that request (routers/tools._summary_detail). A bare link is
// therefore one the backend vouched for when it served it, or one a human typed;
// treating either as proof of a re-issue would be asserting something we do not
// know.
export function logLinkSearch(llmLogId, llmLogProcess) {
	if (llmLogId == null) {
		return {};
	}
	return llmLogProcess == null
		? { log: llmLogId }
		: { log: llmLogId, logProcess: llmLogProcess };
}

// Where a `?log=<id>` deep link can point, given what the AI 日誌 page knows.
// Pure, and split out so the four cases are one expression instead of four
// conditions spread through the render:
//
// * "none" -- no link, or the list has not landed yet (the row may still be in
//   it, and the process token arrives with that same response, so claiming
//   anything in flight would be a lie);
// * "foreign" -- the link CLAIMS a process and it is not this one. The id was
//   minted by a backend run that has ended and its number has been re-issued, so
//   the record now answering to it is a different interaction: the page must not
//   open it, and says why. Checked BEFORE the list is consulted, because
//   FINDING the id here is exactly the misleading part -- that is what made this
//   silent (the row and its detail agree with each other, so the existing
//   started_at staleness guard has nothing to catch);
// * "in-list" -- the row is on this page; the Accordion opens it and the
//   existing per-row detail fetch does the rest;
// * "off-list" -- there IS a target and the newest LIST_LIMIT rows do not
//   contain it. That is NOT the same as "gone": the ring keeps
//   llm_log_max_entries records and the detail endpoint addresses any of them,
//   so the record may be perfectly readable and merely older than this page.
//   Only the fetch can tell those apart, and a 404 from it is the one answer
//   that means evicted.
//
// `String()` on both sides of the token comparison: search params arrive from
// the router's own parser, which coerces an all-digit value to a Number (the
// token is uuid4 hex, so that needs 32 digits and no letters -- rare, and it
// fails toward "foreign", which refuses rather than resolves).
export function deepLinkTarget({
	logId,
	logProcess,
	logs,
	listLoaded,
	processToken,
}) {
	if (logId == null) {
		return { mode: "none", value: null };
	}
	const value = String(logId);
	if (!listLoaded) {
		return { mode: "none", value };
	}
	if (logProcess != null && String(logProcess) !== String(processToken)) {
		return { mode: "foreign", value };
	}
	return {
		mode: logs.some((log) => String(log.id) === value) ? "in-list" : "off-list",
		value,
	};
}
