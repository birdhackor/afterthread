// App-wide connectivity monitor, mounted once in RootLayout. It is the
// trigger for the ACTIVE half of the connectivity design (see
// atoms/connectivity.js): the passive half -- api/client.js reporting
// unambiguous request outcomes -- keeps the badge honest while the user is
// doing things, but it goes silent the moment the user goes idle (and it
// abstains on ambiguous 5xx responses), so an idle tab or a dev-proxy 500
// is exactly where a backend outage/recovery would otherwise stay invisible
// forever. So this hook:
//   - fires probeBackendHealth (api/health.js) on a fixed interval while
//     the tab is visible -- the probe interprets the /api/health response
//     itself (ok body = up, anything else = down) and reports straight into
//     backendStatusAtom, so nothing here handles results;
//   - probes immediately on focus / online / tab-becomes-visible, because
//     each of those signals "the user is back and the state may be long
//     stale" -- making them wait out a full poll interval would show a
//     wrong badge to someone actively looking at it;
//   - force-reloads the LLM status when the backend transitions from
//     unreachable back to reachable, so the AI badge recovers together with
//     the backend badge instead of staying frozen on pre-outage data.

import { useAtomValue, useSetAtom } from "jotai";
import { useEffect, useRef } from "react";
import { probeBackendHealth } from "../api/health.js";
import { backendStatusAtom } from "../atoms/connectivity.js";
import { loadLlmStatusAtom } from "../atoms/llm.js";

const HEALTH_POLL_INTERVAL_MS = 30000;

export function useConnectivityMonitor() {
	const { reachable } = useAtomValue(backendStatusAtom);
	const loadLlmStatus = useSetAtom(loadLlmStatusAtom);

	useEffect(() => {
		// Poll ticks skip hidden tabs: browsers throttle background timers
		// anyway and nobody is looking at the badge; the visibilitychange
		// listener below fires a catch-up probe the moment the tab is shown
		// again, so no staleness ever survives being looked at.
		const intervalId = window.setInterval(() => {
			if (document.visibilityState === "visible") {
				probeBackendHealth();
			}
		}, HEALTH_POLL_INTERVAL_MS);

		const probeNow = () => {
			probeBackendHealth();
		};
		const probeWhenVisible = () => {
			if (document.visibilityState === "visible") {
				probeBackendHealth();
			}
		};
		window.addEventListener("focus", probeNow);
		window.addEventListener("online", probeNow);
		document.addEventListener("visibilitychange", probeWhenVisible);

		// Immediate first probe so `reachable` gets determined right at app
		// start even on a route that fires no request of its own. StrictMode
		// runs this effect twice (cleanup in between), which doubles this
		// initial probe -- harmless: the second claims the newer probe
		// generation (see api/health.js), so at worst the first one's verdict
		// is discarded as stale, and the report atoms skip no-change writes.
		probeBackendHealth();

		return () => {
			window.clearInterval(intervalId);
			window.removeEventListener("focus", probeNow);
			window.removeEventListener("online", probeNow);
			document.removeEventListener("visibilitychange", probeWhenVisible);
		};
	}, []);

	// Recovery hook-up: after an outage the AI badge shows whatever the last
	// reachable probe saw, and loadLlmStatusAtom no-ops non-forced calls once
	// `loaded` is true -- so a backend recovery must explicitly force a
	// re-probe or the AI badge stays frozen on pre-outage data. The previous
	// value lives in a ref because only a false -> true TRANSITION may
	// trigger it:
	//   - null -> true (first determination at app start) must NOT force:
	//     RootLayout's mount effect already loads the LLM status once, and
	//     forcing here too would duplicate that request on every clean start;
	//   - StrictMode re-runs this effect with `reachable` unchanged, and
	//     prev === current filters those re-runs out.
	// Rapid down/up flaps can stack forced probes; that is safe -- llm.js's
	// generation counter drops every superseded write.
	const prevReachableRef = useRef(reachable);
	useEffect(() => {
		const prev = prevReachableRef.current;
		prevReachableRef.current = reachable;
		if (prev === false && reachable === true) {
			loadLlmStatus({ force: true });
		}
	}, [reachable, loadLlmStatus]);
}
