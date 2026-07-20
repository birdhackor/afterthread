// Pure helpers for the 工具 page's install flow. Kept out of the component so
// the poll-stop rule and the URL pre-check are unit-testable without jsdom
// (the vitest ground rule from D07: pure logic only).

// Poll cadence while an install job is still in flight. 2s: an install runs
// for minutes, so this is frequent enough to feel live without hammering a
// backend that is already busy running the builder session.
export const INSTALL_POLL_MS = 2000;

// The two states a job can never leave (see backend tool_builder.InstallJob):
// once reached, polling must stop.
const TERMINAL_STATES = new Set(["succeeded", "failed"]);

export function isTerminalInstallState(state) {
	return TERMINAL_STATES.has(state);
}

// react-query `refetchInterval` function (v5 signature: receives the Query,
// reads its latest data). Falsy/unknown states (undefined data before the
// first poll lands, or a state value a future backend might add) keep
// polling -- stopping is only ever correct once the job is provably terminal.
// A 404 is the other stopping condition: the job is GONE (a backend restart
// forgot it -- jobs are process-local and unpersisted), so polling that dead id
// forever is pure noise. Any OTHER error (500, a transient network blip) keeps
// polling, since those can clear on the next tick.
export function installJobRefetchInterval(query) {
	const state = query?.state?.data?.state;
	if (isTerminalInstallState(state)) {
		return false;
	}
	if (query?.state?.error?.status === 404) {
		return false;
	}
	return INSTALL_POLL_MS;
}

// Whether an install job the form STARTED is still being tracked: while this
// holds, the form stays locked and the progress card keeps polling. Deliberately
// the mirror image of installJobRefetchInterval's stop rule, so the form-lock and
// the poll cadence can never disagree about whether a job is still live: tracking
// ends ONLY when the job reaches a terminal state OR its poll 404s (the job is
// gone -- a backend restart forgot it). Any OTHER poll error (a transient 500, a
// network blip -- `errorStatus` undefined/0) KEEPS the job tracked, because it can
// clear on the next tick while the backend's job is still running; releasing
// "active" on it would let a resubmit fire against that live job, hit the backend's
// 409, and orphan a job we can no longer poll. The pre-first-poll window (a job id
// but no state and no error yet) is active too -- we started a job and simply have
// not heard back. `state` is the latest job state (undefined before the first
// poll); `errorStatus` is the latest poll error's HTTP status (undefined when the
// last poll succeeded).
export function isInstallJobActive({ jobId, state, errorStatus }) {
	if (jobId === null || jobId === undefined) {
		return false;
	}
	if (isTerminalInstallState(state)) {
		return false;
	}
	if (errorStatus === 404) {
		return false;
	}
	return true;
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
