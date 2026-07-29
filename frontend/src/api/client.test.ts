import { getDefaultStore, type PrimitiveAtom } from "jotai";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { backendStatusAtom } from "../atoms/connectivity.js";
import { ApiError, apiDelete, apiFetch, apiGet } from "./client.js";

// client.ts reports connectivity through jotai's default store (the app has
// no <Provider>), so assertions must read backendStatusAtom from that same
// store to observe the reports.
const store = getDefaultStore();
const statusAtom = backendStatusAtom as PrimitiveAtom<{
	reachable: boolean | null;
}>;

const originalFetch = globalThis.fetch;

type FetchStub = (
	input: RequestInfo | URL,
	init?: RequestInit,
) => Promise<Pick<Response, "ok" | "status" | "text">>;

// These unit tests only exercise the three Response members apiFetch reads.
// Keeping the cast in one helper makes that intentionally partial browser
// double explicit while leaving every individual scenario concise.
function stubFetch(implementation: FetchStub): void {
	globalThis.fetch = implementation as typeof fetch;
}

// Await a promise that MUST reject and hand back its rejection reason. A
// plain try/catch around `await` would let a wrongly-resolving call slip
// through with no assertion executed; here an unexpected resolution fails
// the test explicitly.
function rejectionOf(promise: Promise<unknown>): Promise<ApiError> {
	return promise.then<ApiError, ApiError>(
		() => {
			throw new Error("expected the promise to reject");
		},
		(error: unknown) => {
			if (!(error instanceof ApiError)) {
				throw error;
			}
			return error;
		},
	);
}

beforeEach(() => {
	// Reset the shared default-store state so one test's report can neither
	// satisfy nor break the next test's assertion.
	store.set(statusAtom, { reachable: null });
});

afterEach(() => {
	// Undo the per-test fetch stub so no test depends on a predecessor's.
	globalThis.fetch = originalFetch;
});

describe("apiFetch passive connectivity reporting", () => {
	it("returns the parsed body and reports up on an ok response", async () => {
		stubFetch(async () => ({
			ok: true,
			status: 200,
			text: async () => JSON.stringify({ status: "ok" }),
		}));
		await expect(apiGet("/api/health")).resolves.toEqual({ status: "ok" });
		expect(store.get(statusAtom)).toEqual({ reachable: true });
	});

	it("sends Accept and Content-Type: application/json on every request", async () => {
		// Accept is load-bearing, not cosmetic: in packaged mode the backend's
		// SPA fallback (app.frontend) reads fetch's default `Accept: */*` as a
		// browser navigation, so without this header a call to an unknown API
		// route would come back 200 text/html (index.html) instead of a 404
		// JSON -- see the comment in client.ts. Pinning it here means removing
		// the header can never regress silently.
		let seenOptions: RequestInit | undefined;
		stubFetch(async (_path, options) => {
			seenOptions = options;
			return {
				ok: true,
				status: 200,
				text: async () => JSON.stringify({ status: "ok" }),
			};
		});
		await expect(apiGet("/api/health")).resolves.toEqual({ status: "ok" });
		const headers = new Headers(seenOptions?.headers);
		expect(headers.get("Accept")).toBe("application/json");
		expect(headers.get("Content-Type")).toBe("application/json");
	});

	it("materializes typed path parameters and URL-encodes their values", async () => {
		let seenPath: RequestInfo | URL | undefined;
		stubFetch(async (path) => {
			seenPath = path;
			return {
				ok: true,
				status: 200,
				text: async () => JSON.stringify({ id: 1 }),
			};
		});

		await apiGet("/api/tools/{name}/summary", {
			path: { name: "weather/search" },
		});

		expect(seenPath).toBe("/api/tools/weather%2Fsearch/summary");
	});

	it("serializes only schema-named query parameters and drops empty values", async () => {
		let seenPath: RequestInfo | URL | undefined;
		stubFetch(async (path) => {
			seenPath = path;
			return {
				ok: true,
				status: 200,
				text: async () => JSON.stringify({ items: [], total: 0 }),
			};
		});

		await apiGet("/api/items", {
			query: { status: "active", q: "", limit: 20, offset: 0 },
		});

		expect(seenPath).toBe("/api/items?status=active&limit=20&offset=0");
	});

	it("throws ApiError(500) and reports nothing -- a 5xx may be an intermediary, not the backend", async () => {
		stubFetch(async () => ({
			ok: false,
			status: 500,
			text: async () =>
				JSON.stringify({ detail: { code: "boom", message: "伺服器錯誤" } }),
		}));
		// From the undetermined baseline a 5xx must move the state in NEITHER
		// direction: in dev, vite's /api proxy answers 500 itself when the
		// backend is down, so a 5xx proves nothing about reachability -- that
		// ambiguity belongs to the authoritative probe in api/health.ts.
		const error = await rejectionOf(apiGet("/api/health"));
		expect(error).toBeInstanceOf(ApiError);
		expect(error.status).toBe(500);
		expect(store.get(statusAtom)).toEqual({ reachable: null });
		// Abstaining also means a 5xx cannot DOWNGRADE a known-up state: a
		// real backend legitimately 5xxes too (LLM upstream failures return
		// 502/503), and those must not flap the badge during normal AI
		// errors -- it keeps the last verdict until the probe rules.
		store.set(statusAtom, { reachable: true });
		await rejectionOf(apiGet("/api/health"));
		expect(store.get(statusAtom)).toEqual({ reachable: true });
	});

	it("reports up on a 404 -- a sub-5xx response is the application itself answering", async () => {
		stubFetch(async () => ({
			ok: false,
			status: 404,
			text: async () => JSON.stringify({ detail: "Memory item not found" }),
		}));
		// Start from known-down to prove the 404 itself flips the state up:
		// only application logic produces sub-5xx responses, so even an error
		// status is proof of life.
		store.set(statusAtom, { reachable: false });
		const error = await rejectionOf(
			apiGet("/api/items/{item_id}", { path: { item_id: 999 } }),
		);
		expect(error).toBeInstanceOf(ApiError);
		expect(error.status).toBe(404);
		expect(store.get(statusAtom)).toEqual({ reachable: true });
	});

	it("throws the normalized network ApiError and reports down when fetch rejects", async () => {
		stubFetch(async () => {
			throw new TypeError("Failed to fetch");
		});
		// Start from known-up to prove the rejection itself flips it down.
		store.set(statusAtom, { reachable: true });
		const error = await rejectionOf(apiGet("/api/health"));
		expect(error).toBeInstanceOf(ApiError);
		expect(error.status).toBe(0);
		expect(error.code).toBe("network_error");
		expect(store.get(statusAtom)).toEqual({ reachable: false });
	});

	it("emits ONLY a down signal when the connection drops mid-body (no transient up)", async () => {
		stubFetch(async () => ({
			ok: true,
			status: 200,
			text: async () => {
				throw new TypeError("body stream aborted");
			},
		}));
		// Start from known-down and record every state transition: up-evidence
		// requires the FULL body, so the 200 headers alone must never flash
		// `true` -- while reachable is false, such a flash would spuriously
		// trigger the monitor's false -> true LLM recovery reload and emit a
		// contradictory up-then-down pair for one dead request.
		store.set(statusAtom, { reachable: false });
		const seen: Array<boolean | null> = [];
		const unsubscribe = store.sub(statusAtom, () => {
			seen.push(store.get(statusAtom).reachable);
		});
		const error = await rejectionOf(apiGet("/api/health"));
		unsubscribe();
		expect(error).toBeInstanceOf(ApiError);
		expect(error.status).toBe(0);
		expect(error.code).toBe("network_error");
		expect(seen).not.toContain(true);
		expect(store.get(statusAtom)).toEqual({ reachable: false });
	});

	it("returns null and reports up on a 204 -- a bodiless response is already fully delivered", async () => {
		stubFetch(async () => ({
			ok: true,
			status: 204,
			text: async () => "",
		}));
		// Start from known-down to prove the 204 itself flips the state up
		// even though the body-read path (where the usual up-report lives) is
		// skipped entirely.
		store.set(statusAtom, { reachable: false });
		await expect(
			apiDelete("/api/items/{item_id}", { path: { item_id: 1 } }),
		).resolves.toBeNull();
		expect(store.get(statusAtom)).toEqual({ reachable: true });
	});

	it("skips every passive report when reportConnectivity is false", async () => {
		// Success path: a fully delivered ok response must NOT report up --
		// probe traffic (api/health.ts) opts out so its generation-guarded
		// semantic verdict stays the only connectivity writer for probes.
		stubFetch(async () => ({
			ok: true,
			status: 200,
			text: async () => JSON.stringify({ status: "ok" }),
		}));
		await expect(
			apiFetch("/api/health", { method: "GET", reportConnectivity: false }),
		).resolves.toEqual({ status: "ok" });
		expect(store.get(statusAtom)).toEqual({ reachable: null });

		// Failure path: a transport failure must not report down either --
		// opting out silences BOTH directions, not just the up-report.
		stubFetch(async () => {
			throw new TypeError("Failed to fetch");
		});
		store.set(statusAtom, { reachable: true });
		const error = await rejectionOf(
			apiFetch("/api/health", { method: "GET", reportConnectivity: false }),
		);
		expect(error).toBeInstanceOf(ApiError);
		expect(error.status).toBe(0);
		expect(store.get(statusAtom)).toEqual({ reachable: true });
	});
});
