// App-wide backend reachability, shared by every page. Fed from two sides:
//   - passively by api/client.js, which reports only UNAMBIGUOUS request
//     outcomes: transport failure or request deadline = down, sub-5xx
//     response = up, 5xx = no report at all (a 5xx can be an intermediary
//     answering for a dead upstream -- dev's vite proxy 500s by itself when
//     the backend is down -- so it proves nothing either way). A user action
//     that hits a dead backend thus flips the badge instantly, without a
//     middlebox response ever painting a dead backend green;
//   - actively and AUTHORITATIVELY by api/health.js's probeBackendHealth
//     (triggered by hooks/useConnectivityMonitor.js: 30s poll plus
//     focus/online/visibility pings), which judges /api/health response
//     SEMANTICS -- body says ok = up, anything else = down -- and thereby
//     settles every case the passive layer abstains from, in both
//     directions.
// IMPORTANT: this module must NOT import ../api/client.js -- client.js
// imports the report atoms below to do its passive reporting, so an import
// in the other direction would be a cycle. That is why these atoms are pure
// state with no fetch logic of their own.

import { atom } from "jotai";

// Shape: { reachable: null | boolean }. `null` = not yet determined (nothing
// has resolved or failed since app start), so the UI can show an honest
// "checking" state instead of guessing in either direction before the first
// report lands.
export const backendStatusAtom = atom({ reachable: null });

// Write-only report actions. Each writes only on an actual value change:
// the reporters fire constantly (apiFetch on nearly every request, the
// health probe on every poll tick), and unconditionally writing a fresh
// object each time would re-render every backendStatusAtom subscriber on
// every request even though nothing changed.
export const reportBackendUpAtom = atom(null, (get, set) => {
	if (get(backendStatusAtom).reachable !== true) {
		set(backendStatusAtom, { reachable: true });
	}
});

export const reportBackendDownAtom = atom(null, (get, set) => {
	if (get(backendStatusAtom).reachable !== false) {
		set(backendStatusAtom, { reachable: false });
	}
});
