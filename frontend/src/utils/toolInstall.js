// Pure helpers for the 工具 page's install + AI-revise flows. Kept out of the
// component so the poll-stop rule and the URL pre-check are unit-testable
// without jsdom (the vitest ground rule from D07: pure logic only).
//
// The job-state trio right below this comment used to be install-only. D40
// added a second job kind -- an AI revise -- that shares the SAME
// `/api/tools/jobs/{id}` route, job table, and queued/running/succeeded/failed
// state machine (backend tool_builder.JobRecord serves both), so an
// "install"-specific name on shared polling logic would now describe only
// half of what it does. Renamed to generic "tool job" names and reused as-is
// (not forked) by both ToolsPage pollers -- InstallPanel's install job and
// InstalledToolsPanel's revise job. The install-only helpers further down
// (URL/secret validation -- a revise has neither field) keep their original
// names.
//
// Publishing a tool's draft as it is (kind `publish_draft`) is one more kind on the
// same route and state machine, polled by InstalledToolsPanel like a revise;
// toolJobCopy and toolJobOutcomeView word it apart from an install and a revise.

// Poll cadence while a tool job (install, revise or publish of a draft) is still in
// flight. 2s: a job runs for seconds to minutes, so this is frequent enough to
// feel live without hammering a backend that is already busy.
export const TOOL_JOB_POLL_MS = 2000;

// The two states a job can never leave (see backend tool_builder.JobRecord):
// once reached, polling must stop. True of every kind of job alike -- they share
// one state machine.
const TERMINAL_STATES = new Set(["succeeded", "failed"]);

export function isTerminalToolJobState(state) {
	return TERMINAL_STATES.has(state);
}

// What a tool-job card calls the job it shows, by the job's kind (`install`,
// `revise`, `publish_draft`): the verb that titles its outcome, the line while it
// runs, what it says when the job cannot be found any more, the sentence for a
// success, and whether the card links to the AI 日誌. An install and a revise run a
// builder session, and say so; publishing a draft runs none, so its words name
// neither the AI nor a revision, and it has no session to link to. A kind not
// listed (a health check has a card of its own) is worded as an install.
const JOB_COPY = {
	install: {
		verb: "安裝",
		working: "AI 正在安裝工具，可能需要數分鐘……",
		unknownOutcome:
			"找不到這個安裝工作，後端可能已重新啟動。工具可能已經安裝好了——請先看已安裝工具清單，確認之後再決定要不要重裝。",
		succeeded: (name) => `已安裝工具「${name}」。`,
		showsAiLog: true,
	},
	revise: {
		verb: "修訂",
		working: "AI 正在修訂工具，可能需要數分鐘……",
		unknownOutcome:
			"找不到這個修訂工作，後端可能已重新啟動。修訂可能已經完成——請先看上方清單與這個工具的總結，確認之後再決定要不要重送。",
		succeeded: (name) => `已發布工具「${name}」的新版本。`,
		showsAiLog: true,
	},
	publish_draft: {
		verb: "發布草稿",
		working: "正在把草稿發布成新版本……",
		unknownOutcome:
			"找不到這個發布草稿的工作，後端可能已重新啟動。草稿可能已經發布——請先看這個工具的版本與草稿提示，確認之後再決定要不要重送。",
		succeeded: (name) => `已把草稿的檔案發布成工具「${name}」的新版本。`,
		showsAiLog: false,
	},
};

export function toolJobCopy(kind) {
	return Object.hasOwn(JOB_COPY, kind) ? JOB_COPY[kind] : JOB_COPY.install;
}

// The line above a job card that no row of the list owns any more -- the version it
// was sent from is gone from the list, as a published version's is. It names the
// tool, because the card is not under it.
export function orphanedJobHeading(kind, name) {
	const progress =
		kind === "publish_draft" ? "的發布草稿進度" : "的 AI 修訂進度";
	return `工具「${name}」${progress}（送出時的版本已不是目前版本，或工具已被刪除；不再對應下方任何一列）`;
}

// How a TERMINAL job is shown. `state` has only its two terminal values; the
// backend's `outcome` splits the failure, and the one that matters here is
// `not_published`: the builder itself declined to publish. That is not a red
// failure -- it may be an outside cause the AI found (an expired credential) or a
// fix it could not make, and the card says neither: orange, with the AI's own
// words as the body and a note that nothing changed. Any other non-success
// (including a job from an older backend with no `outcome`) gets the red card. A
// publish of a draft has no builder to decline, so it is only ever green or red:
// it is published or it is not.
export function toolJobOutcomeView(job, kind) {
	const { verb } = toolJobCopy(kind);
	if (job?.state === "succeeded") {
		return { tone: "success", color: "green", title: `${verb}完成` };
	}
	if (kind !== "publish_draft" && job?.outcome === "not_published") {
		return {
			tone: "not_published",
			color: "orange",
			title: kind === "revise" ? "AI 未發布新版本" : "AI 未安裝工具",
			note:
				kind === "revise"
					? "工具維持原本的版本，沒有任何變更。"
					: "沒有安裝任何東西。",
		};
	}
	return { tone: "failed", color: "red", title: `${verb}失敗` };
}

// THE stop rule, stated exactly ONCE. Everything below is an adapter that
// feeds it a different input shape; nothing below re-decides it, so the poll
// cadence, the query's `enabled`, and the form/panel locks are the SAME
// predicate by construction and can never drift apart (that "one rule, N
// consumers" property is the reason this module exists at all).
//
// A job stays live until it is PROVABLY finished: falsy/unknown states
// (undefined data before the first poll lands, or a state value a future
// backend might add) keep it live -- stopping is only ever correct once the
// job is terminal. A 404 is the other stopping condition: the job is GONE (a
// backend restart forgot it -- jobs are process-local and unpersisted, and the
// table is bounded, so an old id is also eventually evicted), so there is
// nothing left to ask about. Any OTHER error (500, a transient network blip)
// keeps it live, since those can clear on the next tick, and dropping a live
// job on a blip would let a resubmit fire against a job still running on the
// backend, hit the 409, and orphan a job that can no longer be polled.
function isLiveToolJob({ state, errorStatus }) {
	if (isTerminalToolJobState(state)) {
		return false;
	}
	if (errorStatus === 404) {
		return false;
	}
	return true;
}

// Adapter: pull the rule's two inputs out of a react-query Query object (v5
// hands the whole Query to `refetchInterval` / a functional `enabled`).
function toolJobSignals(query) {
	return {
		state: query?.state?.data?.state,
		errorStatus: query?.state?.error?.status,
	};
}

// react-query `refetchInterval` function: poll every TOOL_JOB_POLL_MS while
// the job is live, stop the moment it is not.
export function toolJobRefetchInterval(query) {
	return isLiveToolJob(toolJobSignals(query)) ? TOOL_JOB_POLL_MS : false;
}

// react-query `enabled` FACTORY for a tool-job poll query: given the job id the
// component is tracking, returns the functional `enabled` (supported since
// query-core's `QueryBooleanOption = boolean | ((query: Query) => boolean)`,
// resolved lazily against the CURRENT query on every fetch decision --
// including `shouldFetchOnWindowFocus`).
//
// Stopping `refetchInterval` alone was never enough: the app's query defaults
// are `refetchOnWindowFocus: true` with a 5s `staleTime` (see main.jsx), so a
// query left `enabled: true` after its job finished fires a fresh GET on EVERY
// window refocus, forever. That is not merely wasteful -- the job table is
// bounded and process-local, so a job that has since been evicted (or lost to a
// backend restart) answers 404, and a settled 「修訂完成」/「安裝完成」 card
// turns into a 「找不到這個…工作」 error card minutes after the fact, with no
// user action in between. Falling `enabled` to false on the SAME condition that
// stops the poll freezes the last outcome exactly where the user left it (a
// disabled query keeps its cached data and error), and the two can never
// disagree because they are the same function.
export function toolJobQueryEnabled(jobId) {
	return (query) => isToolJobActive({ jobId, ...toolJobSignals(query) });
}

// Whether a tool job (install, revise or publish of a draft) the caller STARTED is still being
// tracked: while this holds, its controls stay locked and its progress card
// keeps polling. The rule itself is isLiveToolJob above (see there for why a
// non-404 poll error keeps a job tracked); this adds the one term that only a
// CALLER can answer -- whether there is a job id at all. The pre-first-poll
// window (a job id but no state and no error yet) is active: a job was started
// and there is simply no word back yet. `state` is the latest job state
// (undefined before the first poll); `errorStatus` is the latest poll error's
// HTTP status (undefined when the last poll succeeded).
export function isToolJobActive({ jobId, state, errorStatus }) {
	if (jobId === null || jobId === undefined) {
		return false;
	}
	return isLiveToolJob({ state, errorStatus });
}

// Submit-side URL pre-check: the backend's HttpUrl validation is authoritative
// (422), but rejecting an obviously-not-http value client-side gives an
// immediate field error instead of a round trip. Requires an http(s) scheme
// AND something after it -- a bare "http://" is not a fetchable document URL.
export function isHttpUrl(value) {
	const trimmed = (value ?? "").trim();
	return /^https?:\/\/.+/i.test(trimmed);
}

// Client-side mirror of the backend's install-form secret NAME rule (D36): it
// becomes an environment-variable name, so uppercase-first then
// uppercase/digit/underscore, at most 64 chars. The backend re-validates
// authoritatively (422); this just gives an immediate field error, exactly like
// isHttpUrl above.
export function isSecretName(value) {
	return /^[A-Z][A-Z0-9_]{0,63}$/.test((value ?? "").trim());
}

// Presubmit error (a zh-TW string) for the secret NAME field, given BOTH raw
// inputs, or null when acceptable. The pair is OPTIONAL but both-or-neither:
// both empty is fine; otherwise a name is required and must be a valid env-var
// name. Cross-field by design (it reads the value too), mirroring the backend's
// both-or-neither validator so the two never disagree.
export function secretNameError(name, value) {
	const trimmedName = (name ?? "").trim();
	const trimmedValue = (value ?? "").trim();
	if (trimmedName === "" && trimmedValue === "") {
		return null;
	}
	if (trimmedName === "") {
		return "請輸入秘密名稱，或清空秘密值";
	}
	if (!isSecretName(trimmedName)) {
		return "須以大寫字母開頭，僅能有大寫字母、數字與底線，最多 64 字";
	}
	return null;
}

// Presubmit error (a zh-TW string) for the secret VALUE field, given BOTH raw
// inputs, or null when acceptable. The mirror image of secretNameError's
// both-or-neither: a value is required exactly when a name was supplied.
export function secretValueError(name, value) {
	const trimmedName = (name ?? "").trim();
	const trimmedValue = (value ?? "").trim();
	if (trimmedName === "" && trimmedValue === "") {
		return null;
	}
	if (trimmedValue === "") {
		return "請輸入秘密值，或清空秘密名稱";
	}
	// F3: mirror the backend's >= 6-char floor. The redactor skips values shorter
	// than 6 chars, so a shorter secret could never be masked out of the AI 日誌 or
	// a live tool result -- rejecting it up front gives an immediate field error.
	// Only fires for a COMPLETE pair (a name is present): when the name is missing,
	// secretNameError carries the half-pair error and the value field stays clean,
	// mirroring the backend's pair-incomplete-before-length precedence. The backend
	// re-validates authoritatively (422).
	if (trimmedName !== "" && trimmedValue.length < 6) {
		return "秘密值長度至少 6 字元";
	}
	return null;
}
