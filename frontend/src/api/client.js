// Central fetch wrapper for every backend call. Responsibilities:
//   - JSON serialization / parsing (and 204 No Content handling);
//   - error normalization into a single ApiError shape {status, code,
//     message, fieldErrors} following the shared UX rules, so callers only
//     ever deal with one error type and already-localized (zh-TW) messages;
//   - a query-string helper that skips empty filter values.
// Every page/atom must go through this module rather than calling fetch
// directly.

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
export async function apiFetch(path, options = {}) {
	let response;
	try {
		response = await fetch(path, {
			...options,
			headers: {
				"Content-Type": "application/json",
				...(options.headers ?? {}),
			},
		});
	} catch (_cause) {
		throw new ApiError({
			status: 0,
			code: "network_error",
			message: "無法連線伺服器，請確認網路後再試",
		});
	}

	if (response.status === 204) {
		return null;
	}

	let body = null;
	const text = await response.text();
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
