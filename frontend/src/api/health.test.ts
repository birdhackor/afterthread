import { getDefaultStore, type PrimitiveAtom } from "jotai";
import { beforeEach, describe, expect, it, type Mock, vi } from "vitest";
import { backendStatusAtom } from "../atoms/connectivity.js";
import { ApiError, type ApiSuccessResponse, apiFetch } from "./client.js";
import { probeBackendHealth } from "./health.js";

// probeBackendHealth's contract is "interpret whatever apiFetch settles
// with", so apiFetch is stubbed with test-controlled promises -- the tests
// dictate settle order directly and no real fetch machinery runs (probe
// traffic opts out of passive reporting anyway via reportConnectivity:
// false, which is asserted below). Everything else from client.ts
// (ApiError) stays real.
vi.mock("./client.js", async (importOriginal) => {
	const actual = await importOriginal<typeof import("./client.js")>();
	return { ...actual, apiFetch: vi.fn() };
});

// health.ts writes through jotai's default store (the app has no
// <Provider>), so assertions read backendStatusAtom from that same store.
const store = getDefaultStore();
type HealthResponse = ApiSuccessResponse<"/api/health", "get">;
const statusAtom = backendStatusAtom as PrimitiveAtom<{
	reachable: boolean | null;
}>;
const mockedApiFetch = vi.mocked(apiFetch) as unknown as Mock<
	(path: string, options: RequestInit) => Promise<HealthResponse>
>;

// The probe is fire-and-forget (returns nothing), so tests wait for its
// internal promise chain by yielding a macrotask, which runs strictly after
// every already-settled microtask.
function flush(): Promise<void> {
	return new Promise((resolve) => {
		setTimeout(resolve, 0);
	});
}

// A promise plus its out-of-band settle handle, for the ordering test.
function deferred<Value>(): {
	promise: Promise<Value>;
	resolve: (value: Value | PromiseLike<Value>) => void;
} {
	let resolve!: (value: Value | PromiseLike<Value>) => void;
	const promise = new Promise<Value>((res) => {
		resolve = res;
	});
	return { promise, resolve };
}

beforeEach(() => {
	// Reset the shared default-store state and the mock's queued results so
	// no test depends on a predecessor's.
	store.set(statusAtom, { reachable: null });
	mockedApiFetch.mockReset();
});

describe("probeBackendHealth", () => {
	it("reports up when the health body says ok", async () => {
		mockedApiFetch.mockResolvedValueOnce({ status: "ok" });
		probeBackendHealth();
		await flush();
		// The probe must exempt itself from the passive layer (so its
		// generation-guarded verdict is the only connectivity writer for
		// probe traffic) and carry its own timeout signal (so a hung server
		// cannot stack pending probes forever). The timeout VALUE is a
		// tuning knob, not part of the contract.
		expect(apiFetch).toHaveBeenCalledWith(
			"/api/health",
			expect.objectContaining({
				method: "GET",
				reportConnectivity: false,
				signal: expect.any(AbortSignal),
			}),
		);
		expect(store.get(statusAtom)).toEqual({ reachable: true });
	});

	it("reports down on an ApiError 500 -- the dev proxy answers 500 for a dead backend", async () => {
		// This is the exact case the passive layer abstains on: for the
		// trivial /api/health endpoint a 5xx is never a legitimate answer,
		// only a middleman covering for a dead upstream, so the probe must
		// rule it down.
		mockedApiFetch.mockRejectedValueOnce(
			new ApiError({ status: 500, message: "proxy error" }),
		);
		store.set(statusAtom, { reachable: true });
		probeBackendHealth();
		await flush();
		expect(store.get(statusAtom)).toEqual({ reachable: false });
	});

	it("reports down on a transport failure (status 0 network_error)", async () => {
		// Fetch rejections AND the probe's own AbortSignal timeout both land
		// here: apiFetch's catch normalizes either into this ApiError shape.
		mockedApiFetch.mockRejectedValueOnce(
			new ApiError({
				status: 0,
				code: "network_error",
				message: "無法連線伺服器，請確認網路後再試",
			}),
		);
		store.set(statusAtom, { reachable: true });
		probeBackendHealth();
		await flush();
		expect(store.get(statusAtom)).toEqual({ reachable: false });
	});

	it("reports down when the body resolves with the wrong shape", async () => {
		// A 2xx whose body is not the health payload is not our backend
		// talking (captive portal, misrouted proxy) -- it must read as down.
		mockedApiFetch.mockResolvedValueOnce({ status: "weird" });
		store.set(statusAtom, { reachable: true });
		probeBackendHealth();
		await flush();
		expect(store.get(statusAtom)).toEqual({ reachable: false });
	});

	it("discards a stale slow probe that settles after a newer one already ruled", async () => {
		// Probe A is slow and would report up; probe B fires later, settles
		// first, and reports down. When A finally settles, its generation is
		// stale (B claimed a newer one), so its up-verdict must be dropped --
		// otherwise a pre-outage "ok" landing late would repaint a freshly
		// confirmed-dead backend green until the next poll tick.
		const slow = deferred<HealthResponse>();
		mockedApiFetch.mockReturnValueOnce(slow.promise); // probe A
		mockedApiFetch.mockRejectedValueOnce(
			new ApiError({ status: 500, message: "proxy error" }),
		); // probe B
		probeBackendHealth(); // A claims the older generation
		probeBackendHealth(); // B claims the newer generation
		await flush();
		expect(store.get(statusAtom)).toEqual({ reachable: false });
		slow.resolve({ status: "ok" });
		await flush();
		expect(store.get(statusAtom)).toEqual({ reachable: false });
	});
});
