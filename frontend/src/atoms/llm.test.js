import { getDefaultStore } from "jotai";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { backendStatusAtom } from "./connectivity.js";
import { llmStatusAtom, loadLlmStatusAtom } from "./llm.js";

// llm.js's action atoms run in whatever store invokes them; like the app
// itself (no <Provider>), these tests go through the default store. They stub
// fetch one layer below apiFetch so settle ordering remains controllable while
// passive connectivity reports stay inside the exercised path.
const store = getDefaultStore();
const originalFetch = globalThis.fetch;

// Mirror of llmStatusAtom's initial value, for the per-test reset (the
// default store is a process-wide singleton, so state leaks between tests
// without it). The module-level generation counter in llm.js is NOT reset
// -- it only ever compares captured vs current, so absolute values are
// irrelevant.
const INITIAL_LLM_STATUS = {
	loaded: false,
	loading: false,
	configured: true,
	model: null,
	error: null,
};

// Yield a macrotask so every already-settled promise chain (the action
// atom's internal awaits) runs to completion before assertions.
function flush() {
	return new Promise((resolve) => {
		setTimeout(resolve, 0);
	});
}

// A promise plus its out-of-band settle handles, to dictate settle order.
function deferred() {
	let resolve;
	let reject;
	const promise = new Promise((res, rej) => {
		resolve = res;
		reject = rej;
	});
	return { promise, resolve, reject };
}

function jsonResponse(body) {
	return new Response(JSON.stringify(body), {
		status: 200,
		headers: { "Content-Type": "application/json" },
	});
}

beforeEach(() => {
	store.set(llmStatusAtom, { ...INITIAL_LLM_STATUS });
	store.set(backendStatusAtom, { reachable: null });
});

afterEach(() => {
	globalThis.fetch = originalFetch;
	vi.restoreAllMocks();
});

describe("loadLlmStatusAtom", () => {
	it("force during an in-flight load starts a second request and the stale response is discarded", async () => {
		const oldProbe = deferred();
		const forced = deferred();
		const fetchSpy = vi
			.fn()
			.mockReturnValueOnce(oldProbe.promise)
			.mockReturnValueOnce(forced.promise);
		globalThis.fetch = fetchSpy;

		// The app-start (non-forced) load fires into a dying backend and
		// hangs.
		store.set(loadLlmStatusAtom);
		expect(store.get(llmStatusAtom).loading).toBe(true);

		// The connectivity monitor's false -> true recovery fires a forced
		// reload while that request is still pending. It must start a SECOND
		// request instead of being dropped -- the recovery transition never
		// comes again, so a dropped force would strand the AI badge on
		// pre-outage data until a manual retry.
		store.set(loadLlmStatusAtom, { force: true });
		expect(fetchSpy).toHaveBeenCalledTimes(2);

		// The forced (newer) request settles first and owns the atom.
		forced.resolve(jsonResponse({ configured: true, model: "glm-5.2" }));
		await flush();
		expect(store.get(llmStatusAtom)).toEqual({
			loaded: true,
			loading: false,
			configured: true,
			model: "glm-5.2",
			error: null,
		});

		// The old request settles late, as the transport failure the outage
		// made of it. Its generation is stale (the force claimed a newer
		// one), so its error write must be discarded -- otherwise it would
		// clobber the fresh happy result with error text.
		oldProbe.reject(new TypeError("Failed to fetch"));
		await flush();
		expect(store.get(llmStatusAtom)).toEqual({
			loaded: true,
			loading: false,
			configured: true,
			model: "glm-5.2",
			error: null,
		});
	});

	it("a non-forced call during an in-flight load still no-ops", async () => {
		const probe = deferred();
		const fetchSpy = vi.fn().mockReturnValueOnce(probe.promise);
		globalThis.fetch = fetchSpy;
		store.set(loadLlmStatusAtom);
		// Duplicate non-forced trigger (e.g. StrictMode's doubled mount
		// effect) must coalesce into the one in-flight request.
		store.set(loadLlmStatusAtom);
		expect(fetchSpy).toHaveBeenCalledTimes(1);
		// The status probe must carry its deadline (see PROBE_TIMEOUT_MS in
		// client.js): superseded requests are dropped but never cancelled, so
		// the signal is what bounds how long a hung one can hold a connection.
		expect(fetchSpy).toHaveBeenCalledWith(
			"/api/llm/status",
			expect.objectContaining({
				signal: expect.any(AbortSignal),
			}),
		);
		probe.resolve(jsonResponse({ configured: true, model: "glm-5.2" }));
		await flush();
		expect(store.get(llmStatusAtom)).toEqual({
			loaded: true,
			loading: false,
			configured: true,
			model: "glm-5.2",
			error: null,
		});
	});

	it("a current-generation TimeoutError updates both LLM failure and backend connectivity states", async () => {
		const timeoutController = new AbortController();
		vi.spyOn(AbortSignal, "timeout").mockReturnValue(timeoutController.signal);
		const fetchSpy = vi.fn((_path, options) => {
			return new Promise((_resolve, reject) => {
				options.signal.addEventListener(
					"abort",
					() => reject(options.signal.reason),
					{ once: true },
				);
			});
		});
		globalThis.fetch = fetchSpy;
		store.set(backendStatusAtom, { reachable: true });

		store.set(loadLlmStatusAtom);
		const timeoutReason = new DOMException(
			"LLM status probe timed out",
			"TimeoutError",
		);
		timeoutController.abort(timeoutReason);
		await flush();

		expect(store.get(llmStatusAtom)).toEqual({
			loaded: true,
			loading: false,
			configured: true,
			model: null,
			error: "LLM status probe timed out",
		});
		expect(store.get(backendStatusAtom)).toEqual({ reachable: false });
	});
});
