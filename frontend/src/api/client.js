// Central fetch wrapper for every backend call. Responsibilities:
//   - JSON serialization / parsing (and 204 No Content handling);
//   - error normalization into a single ApiError shape {status, code,
//     message, fieldErrors} following the shared UX rules, so callers only
//     ever deal with one error type and already-localized (zh-TW) messages;
//   - a query-string helper that skips empty filter values;
//   - passive connectivity reporting: unambiguous call outcomes feed the
//     shared backendStatusAtom (fully delivered sub-5xx response =
//     reachable, transport failure = unreachable; a 5xx reports NOTHING
//     either way -- see the rationale inside apiFetch; opt a call out
//     entirely with `reportConnectivity: false`), so the badge tracks real
//     traffic for free.
// Every page/atom must go through this module rather than calling fetch
// directly.

import { getDefaultStore } from "jotai";
import {
	reportBackendDownAtom,
	reportBackendUpAtom,
} from "../atoms/connectivity.js";

// The app renders without a jotai <Provider>, so components read atoms from
// jotai's default store -- writing the connectivity reports to that same
// store from this non-React module reaches exactly the atoms the UI renders.
// (connectivity.js deliberately imports nothing from this module, keeping
// the dependency edge one-way: client -> atoms, no cycle.)
const store = getDefaultStore();

// Normalized error thrown by every helper below. `status` is the HTTP status
// (0 for a network/transport failure), `code` is the machine code from the
// backend detail object when present, `message` is a user-facing zh-TW string,
// and `fieldErrors` maps a field name to its message for 422 responses.
export class ApiError extends Error {
	constructor({ status, code = null, message, fieldErrors = null }) {
		super(message);
		this.name = "ApiError";
		this.status = status;
		this.code = code;
		this.fieldErrors = fieldErrors;
	}
}

// Same shape as a fetch()-throw transport failure (status 0, network_error,
// zh-TW copy) -- also used when the connection drops mid-response, after
// headers arrive but before the body finishes reading. Either way the
// request never delivered a usable response, so it reads as one error kind.
function networkError() {
	return new ApiError({
		status: 0,
		code: "network_error",
		message: "無法連線伺服器，請確認網路後再試",
	});
}

// Shared deadline for the two lightweight status probes (the /api/health
// connectivity probe in api/health.js and the /api/llm/status load in
// atoms/llm.js). Both endpoints are trivial -- no DB, no LLM call -- so a
// working backend answers them near-instantly and anything slower than this
// generous bound is not usable. Bounding them matters beyond UX: probe-shaped
// requests are re-fired by monitors/recovery transitions, so without a
// deadline a server that accepts connections but never responds would let
// pending requests accumulate without limit (superseded requests are dropped
// via generation counters but never cancelled; the timeout is what puts a
// hard ceiling on how long any of them can hold a connection). The abort
// surfaces as a fetch rejection -> the ordinary networkError path.
export const PROBE_TIMEOUT_MS = 10000;

// Build a `?a=1&b=2` query string from a plain object. null / undefined /
// empty-string values are dropped so a page can hand over its whole filter
// state without pruning cleared fields first. Returns "" when nothing is set.
export function buildQuery(params) {
	const search = new URLSearchParams();
	for (const [key, value] of Object.entries(params ?? {})) {
		if (value === null || value === undefined || value === "") {
			continue;
		}
		search.set(key, String(value));
	}
	const query = search.toString();
	return query ? `?${query}` : "";
}

// Map a (status, code, raw server message) triple to the user-facing zh-TW
// message defined by the shared UX rules. Falls back to the server message,
// then to a generic notice for anything unexpected.
function messageFor(status, code, rawMessage) {
	if (status === 503 && code === "llm_not_configured") {
		return "AI 功能尚未設定";
	}
	if (status === 502) {
		return "AI 服務暫時無法使用，請稍後再試";
	}
	if (status === 409 && code === "conflict") {
		return "項目在 AI 處理期間被修改，請重新整理後再試";
	}
	if (status === 404) {
		return "找不到項目";
	}
	return rawMessage ?? "發生錯誤，請稍後再試";
}

// Turn a non-OK response body into an ApiError. The FastAPI `detail` may be:
//   - a plain string (e.g. CRUD 404 -> "Memory item not found");
//   - a {code, message} object (AI 409 / 502 / 503);
//   - a list of {loc, msg, type} validation errors (422).
function normalizeError(status, body) {
	const detail = body && typeof body === "object" ? body.detail : body;

	if (status === 422 && Array.isArray(detail)) {
		const fieldErrors = {};
		for (const entry of detail) {
			const loc = Array.isArray(entry?.loc) ? entry.loc : [];
			// Drop the leading "body"/"query" segment; keep the field path.
			const field =
				loc.filter((part) => part !== "body" && part !== "query").join(".") ||
				"_";
			if (!(field in fieldErrors)) {
				fieldErrors[field] = entry?.msg ?? "欄位資料有誤";
			}
		}
		return new ApiError({
			status,
			code: "validation_error",
			message: "輸入資料有誤，請檢查後再試",
			fieldErrors,
		});
	}

	let code = null;
	let rawMessage = null;
	if (detail && typeof detail === "object") {
		code = typeof detail.code === "string" ? detail.code : null;
		rawMessage = typeof detail.message === "string" ? detail.message : null;
	} else if (typeof detail === "string") {
		rawMessage = detail;
	}

	return new ApiError({
		status,
		code,
		message: messageFor(status, code, rawMessage),
	});
}

// Core request helper. Resolves to the parsed JSON body (or null for 204),
// and throws an ApiError for transport failures and non-OK responses.
// Options pass through to fetch(), except `reportConnectivity` (default
// true): false keeps this call's outcome out of the passive connectivity
// reports entirely, in both directions -- api/health.js sets it for probe
// traffic so its generation-guarded semantic verdict is structurally the
// only connectivity writer for probes.
export async function apiFetch(path, options = {}) {
	const { reportConnectivity = true, ...fetchOptions } = options;
	let response;
	try {
		response = await fetch(path, {
			...fetchOptions,
			headers: {
				// Accept matters beyond content negotiation here: in packaged
				// mode the backend's SPA fallback (app.frontend) treats fetch's
				// own default `Accept: */*` as a browser navigation, so a call
				// to a route the backend doesn't serve would come back as
				// 200 text/html (index.html) instead of a 404 -- and the HTML
				// string would sail through as a "successful" body. Declaring
				// JSON keeps unknown API routes answering 404 JSON, which
				// normalizeError turns into a clean ApiError.
				Accept: "application/json",
				"Content-Type": "application/json",
				...(fetchOptions.headers ?? {}),
			},
		});
	} catch (_cause) {
		if (reportConnectivity) {
			store.set(reportBackendDownAtom);
		}
		throw networkError();
	}

	if (response.status === 204) {
		// A 204 has no body to read: the response is already fully delivered,
		// so it counts as complete up-evidence on its own (and a 204 is
		// sub-5xx by definition -- see the rationale below the body read).
		if (reportConnectivity) {
			store.set(reportBackendUpAtom);
		}
		return null;
	}

	let text;
	try {
		text = await response.text();
	} catch (_cause) {
		// Headers arrived (response.ok / response.status are already known),
		// but the connection dropped before the body finished streaming --
		// still a transport failure from the caller's point of view, so it
		// gets the same normalized shape as the fetch()-throw case above
		// rather than surfacing a raw TypeError. No up-report has fired for
		// this request (up-evidence requires the FULL body, see below), so a
		// request that dies mid-body emits a single, unambiguous down signal.
		if (reportConnectivity) {
			store.set(reportBackendDownAtom);
		}
		throw networkError();
	}

	// Passive up-evidence = a COMPLETELY delivered sub-5xx response, judged
	// only now that the body has arrived:
	//   - sub-5xx, because 2xx/3xx/4xx bodies (including this call's own
	//     ApiError below -- a 404/422 is the backend talking) can only come
	//     from application logic, while a 5xx may be an intermediary
	//     speaking FOR a dead upstream (dev's vite proxy answers 500 itself
	//     when the backend is down). On 5xx this layer abstains entirely: no
	//     up-report (that painted a dead dev backend green), and no
	//     down-report either, because a real backend also legitimately 5xxes
	//     (LLM upstream failures return 502/503) and treating those as
	//     outages would flap the badge during normal AI errors -- the
	//     authoritative /api/health probe (api/health.js) settles what a 5xx
	//     means, and until it rules the badge keeps its last verdict;
	//   - only after the body, because reporting on headers alone let a
	//     request that died mid-body emit a contradictory up-then-down pair,
	//     and while `reachable` was false that transient up could spuriously
	//     trigger the monitor's false -> true LLM recovery reload. A body
	//     that fails JSON.parse below still counts as delivered: non-JSON
	//     content is an application-shape problem, not a connectivity one.
	if (reportConnectivity && response.status < 500) {
		store.set(reportBackendUpAtom);
	}

	let body = null;
	if (text) {
		try {
			body = JSON.parse(text);
		} catch (_parseError) {
			body = text;
		}
	}

	if (!response.ok) {
		throw normalizeError(response.status, body);
	}

	return body;
}

export function apiGet(path) {
	return apiFetch(path, { method: "GET" });
}

export function apiPost(path, body) {
	return apiFetch(path, {
		method: "POST",
		body: body === undefined ? undefined : JSON.stringify(body),
	});
}

export function apiPatch(path, body) {
	return apiFetch(path, {
		method: "PATCH",
		body: body === undefined ? undefined : JSON.stringify(body),
	});
}

export function apiDelete(path) {
	return apiFetch(path, { method: "DELETE" });
}
