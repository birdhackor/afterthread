// Authoritative /api/health interpretation, fired by the connectivity
// monitor (hooks/useConnectivityMonitor.js). The passive layer in client.ts
// only reports UNAMBIGUOUS evidence from real traffic (transport failure =
// down, fully delivered sub-5xx response = up) and abstains on 5xx, because
// a 5xx can be an intermediary answering on behalf of a dead upstream: in
// dev, vite's /api proxy responds 500 ITSELF when the backend is down, so
// "got an HTTP response" proves nothing there. This probe settles that ambiguity by
// judging SEMANTICS, and its verdict is authoritative in both directions:
// /api/health is a trivial endpoint a working backend never fails, so
//   - a resolved body of {status: "ok"} is proof of life;
//   - anything else -- an ApiError of any status (a proxy's 500 for a dead
//     upstream, a transport failure's status 0) or a resolved body of the
//     wrong shape (some middlebox's page parsed as not-our-payload) --
//     means the user cannot reach a WORKING backend right now, which is
//     exactly what the red badge is supposed to say.

import { getDefaultStore } from "jotai";
import {
	reportBackendDownAtom,
	reportBackendUpAtom,
} from "../atoms/connectivity.js";
import { apiFetch, PROBE_TIMEOUT_MS } from "./client.js";

// Same default-store rationale as client.ts: the app renders without a
// jotai <Provider>, so writes from this non-React module land in the exact
// store the components read.
const store = getDefaultStore();

// Probes carry their own deadline (PROBE_TIMEOUT_MS, shared with the LLM
// status probe -- see client.ts): a hung server (accepts the TCP connection
// but never sends headers, or stalls mid-body) would otherwise leave every
// probe pending forever -- stacking one unresolved request per poll tick
// while never reporting anything. The abort surfaces as a fetch rejection,
// which apiFetch's existing catch normalizes into the networkError ApiError
// (no dedicated branch needed), so a timed-out probe lands in the rejection
// handler below and honestly reads as down: a backend that cannot answer
// its trivial health endpoint within this budget is not usable.

// Module-level generation counter, same pattern as atoms/llm.js: the
// monitor's interval tick and its focus/online/visibility pings can put two
// probes in flight at once, and they can settle out of order -- a slow old
// response must not overwrite the verdict of a newer probe that already
// settled (e.g. a pre-outage "ok" landing late must not repaint a dead
// backend green). Each call claims an id before awaiting and only reports
// if it is still the newest probe when its result comes back.
let generation = 0;

// Fire-and-forget: never throws and returns nothing, so trigger sites (the
// monitor's interval and event listeners) stay one-liners. Interpretation
// AND reporting both live here so every trigger gets identical judgment.
export function probeBackendHealth(): void {
	const myGeneration = ++generation;
	// reportConnectivity: false -- probe traffic bypasses the passive layer
	// entirely, making the semantic verdict below STRUCTURALLY the only
	// connectivity writer for probes: every probe-driven report passes the
	// generation guard, so a stale slow probe can no longer slip an ungated
	// passive up/down into the atom at fetch-settle time.
	apiFetch("/api/health", {
		method: "GET",
		reportConnectivity: false,
		signal: AbortSignal.timeout(PROBE_TIMEOUT_MS),
	}).then(
		(body) => {
			if (myGeneration !== generation) {
				// A newer probe settled while this one was in flight; its
				// verdict is fresher, so this stale result is dropped.
				return;
			}
			if (body?.status === "ok") {
				store.set(reportBackendUpAtom);
			} else {
				// A 2xx whose body is not the health payload is not our backend
				// talking (captive portal, misrouted proxy) -- reads as down.
				store.set(reportBackendDownAtom);
			}
		},
		() => {
			if (myGeneration !== generation) {
				return;
			}
			// Any ApiError counts as down here -- INCLUDING the 5xx the
			// passive layer abstains on: for this endpoint a 5xx is never a
			// legitimate application answer, only a middleman covering for a
			// dead upstream or a backend too broken to use, and both mean
			// "無法連線" to the user.
			store.set(reportBackendDownAtom);
		},
	);
}
