import { getDefaultStore, type PrimitiveAtom } from "jotai";
import {
	afterEach,
	beforeEach,
	describe,
	expect,
	it,
	type Mock,
	vi,
} from "vitest";
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

// A promise plus its out-of-band settle handles, for the ordering tests.
function deferred<Value>(): {
	promise: Promise<Value>;
	resolve: (value: Value | PromiseLike<Value>) => void;
	reject: (reason?: unknown) => void;
} {
	let resolve!: (value: Value | PromiseLike<Value>) => void;
	let reject!: (reason?: unknown) => void;
	const promise = new Promise<Value>((res, rej) => {
		resolve = res;
		reject = rej;
	});
	return { promise, resolve, reject };
}

beforeEach(() => {
	// Reset the shared default-store state and the mock's queued results so
	// no test depends on a predecessor's.
	store.set(statusAtom, { reachable: null });
	mockedApiFetch.mockReset();
});

afterEach(() => {
	vi.restoreAllMocks();
});

describe("probeBackendHealth", () => {
	it("reports up when the health body says ok", async () => {
		const timeoutController = new AbortController();
		const timeoutSpy = vi
			.spyOn(AbortSignal, "timeout")
			.mockReturnValue(timeoutController.signal);
		mockedApiFetch.mockResolvedValueOnce({ status: "ok" });
		probeBackendHealth();
		await flush();
		// The probe must exempt itself from the passive layer (so its
		// generation-guarded verdict is the only connectivity writer for probe
		// traffic). Its deadline comes from AbortSignal.timeout with a finite,
		// positive budget so a hung server cannot stack pending probes forever;
		// the exact budget remains a tuning knob, not part of the contract.
		expect(timeoutSpy).toHaveBeenCalledOnce();
		const [timeoutMs] = timeoutSpy.mock.calls[0] ?? [];
		expect(
			typeof timeoutMs === "number" &&
				Number.isFinite(timeoutMs) &&
				timeoutMs > 0,
		).toBe(true);
		expect(apiFetch).toHaveBeenCalledWith(
			"/api/health",
			expect.objectContaining({
				method: "GET",
				reportConnectivity: false,
				signal: timeoutController.signal,
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
		// A fetch rejection normalizes into this ApiError shape. The probe's own
		// timeout does NOT -- see the TimeoutError case below.
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

	it("reports down when the probe's own timeout fires", async () => {
		const timeoutController = new AbortController();
		vi.spyOn(AbortSignal, "timeout").mockReturnValue(timeoutController.signal);
		// Observe the signal exactly as apiFetch does: the request rejects only
		// because the probe-created deadline aborts. A pre-rejected fixture would
		// prove the rejection handler but not that the deadline can reach it.
		mockedApiFetch.mockImplementationOnce((_path, options) => {
			return new Promise((_resolve, reject) => {
				const signal = options.signal;
				signal?.addEventListener("abort", () => reject(signal.reason), {
					once: true,
				});
			});
		});
		store.set(statusAtom, { reachable: true });
		probeBackendHealth();
		timeoutController.abort(
			new DOMException("health probe timed out", "TimeoutError"),
		);
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

	it("discards a stale rejection after a newer probe already reported up", async () => {
		// Probe A hangs, then a focus-triggered probe B succeeds and paints the
		// backend green. A's later timeout must not repaint that newer verdict
		// red; the rejection branch needs the same generation guard as success.
		const slow = deferred<HealthResponse>();
		mockedApiFetch.mockReturnValueOnce(slow.promise); // probe A
		mockedApiFetch.mockResolvedValueOnce({ status: "ok" }); // probe B
		probeBackendHealth();
		probeBackendHealth();
		await flush();
		expect(store.get(statusAtom)).toEqual({ reachable: true });
		slow.reject(new DOMException("health probe timed out", "TimeoutError"));
		await flush();
		expect(store.get(statusAtom)).toEqual({ reachable: true });
	});
});
