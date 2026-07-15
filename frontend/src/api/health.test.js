import { getDefaultStore } from "jotai";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { backendStatusAtom } from "../atoms/connectivity.js";
import { ApiError, apiGet } from "./client.js";
import { probeBackendHealth } from "./health.js";

// probeBackendHealth's contract is "interpret whatever apiGet settles
// with", so apiGet is stubbed with test-controlled promises and the real
// apiFetch -- with its own passive reporting, which would double-report
// into the same atom mid-test -- stays out of the picture; client.test.js
// covers that layer separately. Everything else from client.js (ApiError)
// stays real.
vi.mock("./client.js", async (importOriginal) => {
	const actual = await importOriginal();
	return { ...actual, apiGet: vi.fn() };
});

// health.js writes through jotai's default store (the app has no
// <Provider>), so assertions read backendStatusAtom from that same store.
const store = getDefaultStore();

// The probe is fire-and-forget (returns nothing), so tests wait for its
// internal promise chain by yielding a macrotask, which runs strictly after
// every already-settled microtask.
function flush() {
	return new Promise((resolve) => {
		setTimeout(resolve, 0);
	});
}

// A promise plus its out-of-band settle handle, for the ordering test.
function deferred() {
	let resolve;
	const promise = new Promise((res) => {
		resolve = res;
	});
	return { promise, resolve };
}

beforeEach(() => {
	// Reset the shared default-store state and the mock's queued results so
	// no test depends on a predecessor's.
	store.set(backendStatusAtom, { reachable: null });
	apiGet.mockReset();
});

describe("probeBackendHealth", () => {
	it("reports up when the health body says ok", async () => {
		apiGet.mockResolvedValueOnce({ status: "ok" });
		probeBackendHealth();
		await flush();
		expect(apiGet).toHaveBeenCalledWith("/api/health");
		expect(store.get(backendStatusAtom)).toEqual({ reachable: true });
	});

	it("reports down on an ApiError 500 -- the dev proxy answers 500 for a dead backend", async () => {
		// This is the exact case the passive layer abstains on: for the
		// trivial /api/health endpoint a 5xx is never a legitimate answer,
		// only a middleman covering for a dead upstream, so the probe must
		// rule it down.
		apiGet.mockRejectedValueOnce(
			new ApiError({ status: 500, message: "proxy error" }),
		);
		store.set(backendStatusAtom, { reachable: true });
		probeBackendHealth();
		await flush();
		expect(store.get(backendStatusAtom)).toEqual({ reachable: false });
	});

	it("reports down on a transport failure (status 0 network_error)", async () => {
		apiGet.mockRejectedValueOnce(
			new ApiError({
				status: 0,
				code: "network_error",
				message: "無法連線伺服器，請確認網路後再試",
			}),
		);
		store.set(backendStatusAtom, { reachable: true });
		probeBackendHealth();
		await flush();
		expect(store.get(backendStatusAtom)).toEqual({ reachable: false });
	});

	it("reports down when the body resolves with the wrong shape", async () => {
		// A 2xx whose body is not the health payload is not our backend
		// talking (captive portal, misrouted proxy) -- it must read as down.
		apiGet.mockResolvedValueOnce({ status: "weird" });
		store.set(backendStatusAtom, { reachable: true });
		probeBackendHealth();
		await flush();
		expect(store.get(backendStatusAtom)).toEqual({ reachable: false });
	});

	it("discards a stale slow probe that settles after a newer one already ruled", async () => {
		// Probe A is slow and would report up; probe B fires later, settles
		// first, and reports down. When A finally settles, its generation is
		// stale (B claimed a newer one), so its up-verdict must be dropped --
		// otherwise a pre-outage "ok" landing late would repaint a freshly
		// confirmed-dead backend green until the next poll tick.
		const slow = deferred();
		apiGet.mockReturnValueOnce(slow.promise); // probe A
		apiGet.mockRejectedValueOnce(
			new ApiError({ status: 500, message: "proxy error" }),
		); // probe B
		probeBackendHealth(); // A claims the older generation
		probeBackendHealth(); // B claims the newer generation
		await flush();
		expect(store.get(backendStatusAtom)).toEqual({ reachable: false });
		slow.resolve({ status: "ok" });
		await flush();
		expect(store.get(backendStatusAtom)).toEqual({ reachable: false });
	});
});
