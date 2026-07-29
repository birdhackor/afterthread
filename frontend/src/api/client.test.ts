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
import {
	ApiError,
	apiDelete,
	apiFetch,
	apiGet,
	apiPatch,
	apiPost,
} from "./client.js";

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
function stubFetch(implementation: FetchStub): Mock<FetchStub> {
	const spy = vi.fn(implementation);
	globalThis.fetch = spy as unknown as typeof fetch;
	return spy;
}

function expectTransportCall(
	fetchSpy: Mock<FetchStub>,
	expected: {
		path: string;
		method: string;
		body?: unknown;
	},
): void {
	expect(fetchSpy).toHaveBeenCalledTimes(1);
	const [path, options] = fetchSpy.mock.calls[0] ?? [];
	expect(path).toBe(expected.path);
	expect(options?.method).toBe(expected.method);
	if ("body" in expected) {
		expect(options?.body).toBe(JSON.stringify(expected.body));
	} else {
		expect(options).not.toHaveProperty("body");
	}
	const headers = new Headers(options?.headers);
	expect(headers.get("Accept")).toBe("application/json");
	expect(headers.get("Content-Type")).toBe("application/json");
}

async function expectCallerHeaders(
	headers: HeadersInit,
	expected: {
		marker: string;
		accept: string;
		contentType: string;
	},
): Promise<void> {
	const fetchSpy = stubFetch(async () => ({
		ok: true,
		status: 200,
		text: async () => JSON.stringify({ status: "ok" }),
	}));

	await expect(
		apiFetch("/api/health", {
			method: "GET",
			headers,
			reportConnectivity: false,
		}),
	).resolves.toEqual({ status: "ok" });

	expect(fetchSpy).toHaveBeenCalledTimes(1);
	const [, options] = fetchSpy.mock.calls[0] ?? [];
	const sentHeaders = new Headers(options?.headers);
	expect(sentHeaders.get("X-Transport-Contract")).toBe(expected.marker);
	expect(sentHeaders.get("Accept")).toBe(expected.accept);
	expect(sentHeaders.get("Content-Type")).toBe(expected.contentType);
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

function expectNetworkError(error: ApiError): void {
	expect(error).toMatchObject({
		name: "ApiError",
		status: 0,
		code: "network_error",
		message: "無法連線伺服器，請確認網路後再試",
		fieldErrors: null,
	});
}

async function rejectNonJson502(body: string): Promise<ApiError> {
	stubFetch(async () => ({
		ok: false,
		status: 502,
		text: async () => body,
	}));
	return rejectionOf(apiGet("/api/health"));
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

// Component tests intentionally stop at mocked client helpers. These tests
// live at the next boundary down so URL expansion, verbs, bodies, and fetch
// options cannot drift while every caller-facing assertion remains green.
describe("transport contracts", () => {
	it("apiFetch sends one expanded GET with fetch options and no body", async () => {
		const controller = new AbortController();
		const requestInit = {
			cache: "no-store",
			credentials: "include",
			integrity: "sha256-transport-contract",
			keepalive: true,
			mode: "cors",
			priority: "high",
			redirect: "manual",
			referrer: "",
			referrerPolicy: "no-referrer",
			signal: controller.signal,
			window: null,
		} satisfies Omit<RequestInit, "body" | "headers" | "method">;
		const fetchSpy = stubFetch(async () => ({
			ok: true,
			status: 200,
			text: async () => JSON.stringify({ items: [], total: 0 }),
		}));

		await expect(
			apiFetch("/api/items", {
				method: "GET",
				query: { status: "active", q: "", limit: 20, offset: 0 },
				headers: { "X-Transport-Contract": "apiFetch" },
				...requestInit,
				reportConnectivity: false,
			}),
		).resolves.toEqual({ items: [], total: 0 });

		expectTransportCall(fetchSpy, {
			path: "/api/items?status=active&limit=20&offset=0",
			method: "GET",
		});
		const [, options] = fetchSpy.mock.calls[0] ?? [];
		expect(options).toMatchObject(requestInit);
		expect(new Headers(options?.headers).get("X-Transport-Contract")).toBe(
			"apiFetch",
		);
		expect(options).not.toHaveProperty("reportConnectivity");
	});

	it("apiFetch preserves a plain-object HeadersInit and caller media types", async () => {
		await expectCallerHeaders(
			{
				Accept: "application/vnd.afterthread.object+json",
				"Content-Type": "application/problem+json; form=object",
				"X-Transport-Contract": "plain-object",
			},
			{
				marker: "plain-object",
				accept: "application/vnd.afterthread.object+json",
				contentType: "application/problem+json; form=object",
			},
		);
	});

	it("apiFetch preserves a Headers instance and caller media types", async () => {
		await expectCallerHeaders(
			new Headers({
				Accept: "application/vnd.afterthread.headers+json",
				"Content-Type": "application/problem+json; form=headers",
				"X-Transport-Contract": "headers-instance",
			}),
			{
				marker: "headers-instance",
				accept: "application/vnd.afterthread.headers+json",
				contentType: "application/problem+json; form=headers",
			},
		);
	});

	it("apiFetch preserves a tuple-array HeadersInit and caller media types", async () => {
		const headers: HeadersInit = [
			["Accept", "application/vnd.afterthread.tuples+json"],
			["Content-Type", "application/problem+json; form=tuples"],
			["X-Transport-Contract", "tuple-array"],
		];
		await expectCallerHeaders(headers, {
			marker: "tuple-array",
			accept: "application/vnd.afterthread.tuples+json",
			contentType: "application/problem+json; form=tuples",
		});
	});

	it("apiGet sends one expanded GET with no body", async () => {
		const fetchSpy = stubFetch(async () => ({
			ok: true,
			status: 200,
			text: async () =>
				JSON.stringify({
					current_vid: "v1",
					llm_log_id: null,
					summary: "sunny",
					updated_at: null,
				}),
		}));

		await apiGet("/api/tools/{name}/summary", {
			path: { name: "weather/search" },
		});

		expectTransportCall(fetchSpy, {
			path: "/api/tools/weather%2Fsearch/summary",
			method: "GET",
		});
	});

	it("apiPost sends one expanded POST with a JSON body", async () => {
		const fetchSpy = stubFetch(async () => ({
			ok: true,
			status: 201,
			text: async () =>
				JSON.stringify({
					id: 8,
					item_id: 37,
					note: "transport contract",
					date: "2026-07-29T00:00:00Z",
				}),
		}));

		await apiPost("/api/items/{item_id}/progress", {
			path: { item_id: 37 },
			body: { note: "transport contract" },
		});

		expectTransportCall(fetchSpy, {
			path: "/api/items/37/progress",
			method: "POST",
			body: { note: "transport contract" },
		});
	});

	it("apiPatch sends one expanded PATCH with a JSON body", async () => {
		const fetchSpy = stubFetch(async () => ({
			ok: true,
			status: 200,
			text: async () => JSON.stringify({ id: 37, status: "waiting" }),
		}));

		await apiPatch("/api/items/{item_id}", {
			path: { item_id: 37 },
			body: { status: "waiting" },
		});

		expectTransportCall(fetchSpy, {
			path: "/api/items/37",
			method: "PATCH",
			body: { status: "waiting" },
		});
	});

	it("apiDelete sends one expanded DELETE with no body and maps 204 to null", async () => {
		const fetchSpy = stubFetch(async () => ({
			ok: true,
			status: 204,
			text: async () => {
				throw new Error("a 204 body must not be read");
			},
		}));

		await expect(
			apiDelete("/api/items/{item_id}", { path: { item_id: 37 } }),
		).resolves.toBeNull();

		expectTransportCall(fetchSpy, {
			path: "/api/items/37",
			method: "DELETE",
		});
	});

	it("apiFetch builds a caller-facing validation ApiError", async () => {
		const fetchSpy = stubFetch(async () => ({
			ok: false,
			status: 422,
			text: async () =>
				JSON.stringify({
					detail: [
						{
							loc: ["query", "limit"],
							msg: "Input should be greater than or equal to 1",
							type: "greater_than_equal",
						},
					],
				}),
		}));

		const error = await rejectionOf(
			apiFetch("/api/items", {
				method: "GET",
				query: { limit: 0 },
				reportConnectivity: false,
			}),
		);

		expect(error).toMatchObject({
			name: "ApiError",
			status: 422,
			code: "validation_error",
			message: "輸入資料有誤，請檢查後再試",
			fieldErrors: {
				limit: "Input should be greater than or equal to 1",
			},
		});
		expectTransportCall(fetchSpy, {
			path: "/api/items?limit=0",
			method: "GET",
		});
	});
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
		expectNetworkError(error);
		expect(store.get(statusAtom)).toEqual({ reachable: false });
	});

	it("preserves an AbortSignal rejection and does not report a connectivity outage", async () => {
		const controller = new AbortController();
		const fetchSpy = stubFetch(
			(_path, options) =>
				new Promise((_resolve, reject) => {
					const signal = options?.signal;
					if (!(signal instanceof AbortSignal)) {
						reject(new Error("expected apiFetch to forward its AbortSignal"));
						return;
					}
					signal.addEventListener("abort", () => reject(signal.reason), {
						once: true,
					});
				}),
		);
		store.set(statusAtom, { reachable: true });

		const request = apiFetch("/api/health", {
			method: "GET",
			signal: controller.signal,
		});
		const abortReason = new DOMException(
			"operator abandoned request",
			"AbortError",
		);
		controller.abort(abortReason);

		await expect(request).rejects.toBe(abortReason);
		expect(fetchSpy.mock.calls[0]?.[1]?.signal).toBe(controller.signal);
		expect(store.get(statusAtom)).toEqual({ reachable: true });
	});

	it("preserves an abort during body reading and does not report a connectivity outage", async () => {
		const controller = new AbortController();
		let markBodyReadStarted: (() => void) | undefined;
		const bodyReadStarted = new Promise<void>((resolve) => {
			markBodyReadStarted = resolve;
		});
		stubFetch(async () => ({
			ok: true,
			status: 200,
			text: () => {
				markBodyReadStarted?.();
				return new Promise<string>((_resolve, reject) => {
					controller.signal.addEventListener(
						"abort",
						() => reject(controller.signal.reason),
						{ once: true },
					);
				});
			},
		}));
		store.set(statusAtom, { reachable: true });

		const request = apiFetch("/api/health", {
			method: "GET",
			signal: controller.signal,
		});
		await bodyReadStarted;
		const abortReason = new DOMException(
			"operator abandoned response body",
			"AbortError",
		);
		controller.abort(abortReason);

		await expect(request).rejects.toBe(abortReason);
		expect(store.get(statusAtom)).toEqual({ reachable: true });
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
		expectNetworkError(error);
		expect(seen).not.toContain(true);
		expect(store.get(statusAtom)).toEqual({ reachable: false });
	});

	it("turns an HTML 502 proxy page into a caller-facing ApiError", async () => {
		const error = await rejectNonJson502(
			"<!doctype html><title>Bad Gateway</title>",
		);

		expect(error).toMatchObject({
			name: "ApiError",
			status: 502,
			code: null,
			message: "AI 服務暫時無法使用，請稍後再試",
			fieldErrors: null,
		});
		expect(store.get(statusAtom)).toEqual({ reachable: null });
	});

	it("turns an empty 502 body into a caller-facing ApiError", async () => {
		const error = await rejectNonJson502("");

		expect(error).toMatchObject({
			name: "ApiError",
			status: 502,
			code: null,
			message: "AI 服務暫時無法使用，請稍後再試",
			fieldErrors: null,
		});
		expect(store.get(statusAtom)).toEqual({ reachable: null });
	});

	it("turns truncated JSON from a 502 into a caller-facing ApiError", async () => {
		const error = await rejectNonJson502('{"detail":{"code":"upstream_error"');

		expect(error).toMatchObject({
			name: "ApiError",
			status: 502,
			code: null,
			message: "AI 服務暫時無法使用，請稍後再試",
			fieldErrors: null,
		});
		expect(store.get(statusAtom)).toEqual({ reachable: null });
	});

	it("rejects malformed JSON from a body-bearing 2xx as an ApiError", async () => {
		stubFetch(async () => ({
			ok: true,
			status: 200,
			text: async () => '{"status":"ok"',
		}));

		const error = await rejectionOf(apiGet("/api/health"));

		expect(error).toMatchObject({
			name: "ApiError",
			status: 200,
			code: "invalid_response",
			message: "伺服器回應格式有誤，請稍後再試",
			fieldErrors: null,
		});
		expect(store.get(statusAtom)).toEqual({ reachable: true });
	});

	it("rejects an empty body-bearing 2xx as an ApiError", async () => {
		stubFetch(async () => ({
			ok: true,
			status: 200,
			text: async () => "",
		}));

		const error = await rejectionOf(apiGet("/api/health"));

		expect(error).toMatchObject({
			name: "ApiError",
			status: 200,
			code: "invalid_response",
			message: "伺服器回應格式有誤，請稍後再試",
			fieldErrors: null,
		});
		expect(store.get(statusAtom)).toEqual({ reachable: true });
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
		expectNetworkError(error);
		expect(store.get(statusAtom)).toEqual({ reachable: true });
	});
});
