import { getDefaultStore } from "jotai";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, apiGet } from "../api/client.js";
import { llmStatusAtom, loadLlmStatusAtom } from "./llm.js";

// These tests need full control over settle ORDER (an old in-flight status
// response landing after a forced retry already resolved), so apiGet is
// stubbed with test-controlled promises; the real apiFetch (and its passive
// connectivity reporting) stays out of the picture. Everything else from
// client.js (ApiError) stays real.
vi.mock("../api/client.js", async (importOriginal) => {
	const actual = await importOriginal();
	return { ...actual, apiGet: vi.fn() };
});

// llm.js's action atoms run in whatever store invokes them; like the app
// itself (no <Provider>), these tests go through the default store.
const store = getDefaultStore();

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

beforeEach(() => {
	store.set(llmStatusAtom, { ...INITIAL_LLM_STATUS });
	apiGet.mockReset();
});

describe("loadLlmStatusAtom", () => {
	it("force during an in-flight load starts a second request and the stale response is discarded", async () => {
		const oldProbe = deferred();
		const forced = deferred();
		apiGet.mockReturnValueOnce(oldProbe.promise);
		apiGet.mockReturnValueOnce(forced.promise);

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
		expect(apiGet).toHaveBeenCalledTimes(2);

		// The forced (newer) request settles first and owns the atom.
		forced.resolve({ configured: true, model: "glm-5.2" });
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
		oldProbe.reject(
			new ApiError({
				status: 0,
				code: "network_error",
				message: "無法連線伺服器，請確認網路後再試",
			}),
		);
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
		apiGet.mockReturnValueOnce(probe.promise);
		store.set(loadLlmStatusAtom);
		// Duplicate non-forced trigger (e.g. StrictMode's doubled mount
		// effect) must coalesce into the one in-flight request.
		store.set(loadLlmStatusAtom);
		expect(apiGet).toHaveBeenCalledTimes(1);
		probe.resolve({ configured: true, model: "glm-5.2" });
		await flush();
		expect(store.get(llmStatusAtom)).toEqual({
			loaded: true,
			loading: false,
			configured: true,
			model: "glm-5.2",
			error: null,
		});
	});
});
