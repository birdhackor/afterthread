import { getDefaultStore } from "jotai";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { backendStatusAtom } from "../atoms/connectivity.js";
import { ApiError, apiDelete, apiFetch, apiGet } from "./client.js";

// client.js reports connectivity through jotai's default store (the app has
// no <Provider>), so assertions must read backendStatusAtom from that same
// store to observe the reports.
const store = getDefaultStore();

const originalFetch = globalThis.fetch;

// Await a promise that MUST reject and hand back its rejection reason. A
// plain try/catch around `await` would let a wrongly-resolving call slip
// through with no assertion executed; here an unexpected resolution fails
// the test explicitly.
function rejectionOf(promise) {
	return promise.then(
		() => {
			throw new Error("expected the promise to reject");
		},
		(error) => error,
	);
}

beforeEach(() => {
	// Reset the shared default-store state so one test's report can neither
	// satisfy nor break the next test's assertion.
	store.set(backendStatusAtom, { reachable: null });
});

afterEach(() => {
	// Undo the per-test fetch stub so no test depends on a predecessor's.
	globalThis.fetch = originalFetch;
});

describe("apiFetch passive connectivity reporting", () => {
	it("returns the parsed body and reports up on an ok response", async () => {
		globalThis.fetch = async () => ({
			ok: true,
			status: 200,
			text: async () => JSON.stringify({ status: "ok" }),
		});
		await expect(apiGet("/api/health")).resolves.toEqual({ status: "ok" });
		expect(store.get(backendStatusAtom)).toEqual({ reachable: true });
	});

	it("throws ApiError(500) and reports nothing -- a 5xx may be an intermediary, not the backend", async () => {
		globalThis.fetch = async () => ({
			ok: false,
			status: 500,
			text: async () =>
				JSON.stringify({ detail: { code: "boom", message: "伺服器錯誤" } }),
		});
		// From the undetermined baseline a 5xx must move the state in NEITHER
		// direction: in dev, vite's /api proxy answers 500 itself when the
		// backend is down, so a 5xx proves nothing about reachability -- that
		// ambiguity belongs to the authoritative probe in api/health.js.
		const error = await rejectionOf(apiGet("/api/anything"));
		expect(error).toBeInstanceOf(ApiError);
		expect(error.status).toBe(500);
		expect(store.get(backendStatusAtom)).toEqual({ reachable: null });
		// Abstaining also means a 5xx cannot DOWNGRADE a known-up state: a
		// real backend legitimately 5xxes too (LLM upstream failures return
		// 502/503), and those must not flap the badge during normal AI
		// errors -- it keeps the last verdict until the probe rules.
		store.set(backendStatusAtom, { reachable: true });
		await rejectionOf(apiGet("/api/anything"));
		expect(store.get(backendStatusAtom)).toEqual({ reachable: true });
	});

	it("reports up on a 404 -- a sub-5xx response is the application itself answering", async () => {
		globalThis.fetch = async () => ({
			ok: false,
			status: 404,
			text: async () => JSON.stringify({ detail: "Memory item not found" }),
		});
		// Start from known-down to prove the 404 itself flips the state up:
		// only application logic produces sub-5xx responses, so even an error
		// status is proof of life.
		store.set(backendStatusAtom, { reachable: false });
		const error = await rejectionOf(apiGet("/api/items/999"));
		expect(error).toBeInstanceOf(ApiError);
		expect(error.status).toBe(404);
		expect(store.get(backendStatusAtom)).toEqual({ reachable: true });
	});

	it("throws the normalized network ApiError and reports down when fetch rejects", async () => {
		globalThis.fetch = async () => {
			throw new TypeError("Failed to fetch");
		};
		// Start from known-up to prove the rejection itself flips it down.
		store.set(backendStatusAtom, { reachable: true });
		const error = await rejectionOf(apiGet("/api/health"));
		expect(error).toBeInstanceOf(ApiError);
		expect(error.status).toBe(0);
		expect(error.code).toBe("network_error");
		expect(store.get(backendStatusAtom)).toEqual({ reachable: false });
	});

	it("emits ONLY a down signal when the connection drops mid-body (no transient up)", async () => {
		globalThis.fetch = async () => ({
			ok: true,
			status: 200,
			text: async () => {
				throw new TypeError("body stream aborted");
			},
		});
		// Start from known-down and record every state transition: up-evidence
		// requires the FULL body, so the 200 headers alone must never flash
		// `true` -- while reachable is false, such a flash would spuriously
		// trigger the monitor's false -> true LLM recovery reload and emit a
		// contradictory up-then-down pair for one dead request.
		store.set(backendStatusAtom, { reachable: false });
		const seen = [];
		const unsubscribe = store.sub(backendStatusAtom, () => {
			seen.push(store.get(backendStatusAtom).reachable);
		});
		const error = await rejectionOf(apiGet("/api/health"));
		unsubscribe();
		expect(error).toBeInstanceOf(ApiError);
		expect(error.status).toBe(0);
		expect(error.code).toBe("network_error");
		expect(seen).not.toContain(true);
		expect(store.get(backendStatusAtom)).toEqual({ reachable: false });
	});

	it("returns null and reports up on a 204 -- a bodiless response is already fully delivered", async () => {
		globalThis.fetch = async () => ({
			ok: true,
			status: 204,
			text: async () => "",
		});
		// Start from known-down to prove the 204 itself flips the state up
		// even though the body-read path (where the usual up-report lives) is
		// skipped entirely.
		store.set(backendStatusAtom, { reachable: false });
		await expect(apiDelete("/api/items/1")).resolves.toBeNull();
		expect(store.get(backendStatusAtom)).toEqual({ reachable: true });
	});

	it("skips every passive report when reportConnectivity is false", async () => {
		// Success path: a fully delivered ok response must NOT report up --
		// probe traffic (api/health.js) opts out so its generation-guarded
		// semantic verdict stays the only connectivity writer for probes.
		globalThis.fetch = async () => ({
			ok: true,
			status: 200,
			text: async () => JSON.stringify({ status: "ok" }),
		});
		await expect(
			apiFetch("/api/health", { method: "GET", reportConnectivity: false }),
		).resolves.toEqual({ status: "ok" });
		expect(store.get(backendStatusAtom)).toEqual({ reachable: null });

		// Failure path: a transport failure must not report down either --
		// opting out silences BOTH directions, not just the up-report.
		globalThis.fetch = async () => {
			throw new TypeError("Failed to fetch");
		};
		store.set(backendStatusAtom, { reachable: true });
		const error = await rejectionOf(
			apiFetch("/api/health", { method: "GET", reportConnectivity: false }),
		);
		expect(error).toBeInstanceOf(ApiError);
		expect(error.status).toBe(0);
		expect(store.get(backendStatusAtom)).toEqual({ reachable: true });
	});
});
