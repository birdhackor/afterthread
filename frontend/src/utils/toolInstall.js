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

// Submit-side URL pre-check: the backend's HttpUrl validation is authoritative
// (422), but rejecting an obviously-not-http value client-side gives an
// immediate field error instead of a round trip. Requires an http(s) scheme
// AND something after it -- a bare "http://" is not a fetchable document URL.
export function isHttpUrl(value) {
	const trimmed = (value ?? "").trim();
	return /^https?:\/\/.+/i.test(trimmed);
}
