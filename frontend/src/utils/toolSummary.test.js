import { describe, expect, it } from "vitest";
import {
	deepLinkTarget,
	logLinkSearch,
	ownSummaryBusy,
	summaryErrorRevalidates,
	toolInstanceKey,
	toolSummaryKeyPrefix,
	toolSummaryQueryKey,
	writeSummaryDetailIfPresent,
} from "./toolSummary.js";

describe("toolSummaryQueryKey / toolSummaryKeyPrefix", () => {
	it("folds the instance discriminator in after the name", () => {
		expect(toolSummaryQueryKey("kb_search", "查詢內部知識庫")).toEqual([
			"tool-summary",
			"kb_search",
			"查詢內部知識庫",
		]);
	});

	it("keeps the prefix a real prefix of the exact key", () => {
		// The invalidate/remove call sites pass the PREFIX and rely on TanStack
		// Query's element-wise partial match to reach every instance of a name.
		// If these two ever stopped lining up, those calls would silently match
		// nothing -- the exact failure mode `exact: true` produced once the key
		// grew a third element.
		const prefix = toolSummaryKeyPrefix("kb_search");
		const exact = toolSummaryQueryKey("kb_search", "任何描述");
		expect(exact.slice(0, prefix.length)).toEqual(prefix);
	});

	it("separates two tools that differ only by description", () => {
		// The whole point: same NAME, different install -> different cache entry.
		expect(toolSummaryQueryKey("kb", "第一次安裝")).not.toEqual(
			toolSummaryQueryKey("kb", "重裝後的新描述"),
		);
		// ...and the prefix still gathers both, which is what delete must clear.
		expect(toolSummaryKeyPrefix("kb")).toEqual(toolSummaryKeyPrefix("kb"));
	});
});

describe("toolInstanceKey", () => {
	it("is a string (React keys are not arrays)", () => {
		expect(typeof toolInstanceKey("kb", "描述")).toBe("string");
	});

	it("separates the same name under two different descriptions", () => {
		// The whole point of keying the ROW on this: a same-name reinstall must
		// remount the row, so the revise feedback typed for the old instance cannot
		// be submitted against the new one.
		expect(toolInstanceKey("kb", "第一次安裝")).not.toBe(
			toolInstanceKey("kb", "重裝後的新描述"),
		);
	});

	it("separates two different names, and is stable for equal inputs", () => {
		expect(toolInstanceKey("a", "同一段描述")).not.toBe(
			toolInstanceKey("b", "同一段描述"),
		);
		expect(toolInstanceKey("kb", "描述")).toBe(toolInstanceKey("kb", "描述"));
	});

	it("agrees with the summary cache key on every pair it is given", () => {
		// The invariant that makes the row and its summary ONE identity: two rows
		// share a React key exactly when they share a cache entry. Pinned as an
		// equivalence so a future change to either spelling has to break a test.
		const pairs = [
			["kb", "描述 A"],
			["kb", "描述 B"],
			["other", "描述 A"],
			["kb", null],
			["kb", undefined],
			["kb", ""],
		];
		for (const [nameA, descA] of pairs) {
			for (const [nameB, descB] of pairs) {
				const sameKey =
					toolInstanceKey(nameA, descA) === toolInstanceKey(nameB, descB);
				const sameCacheKey =
					JSON.stringify(toolSummaryQueryKey(nameA, descA)) ===
					JSON.stringify(toolSummaryQueryKey(nameB, descB));
				expect(sameKey).toBe(sameCacheKey);
			}
		}
	});

	it("treats a missing description as one identity, not two", () => {
		// GET /api/tools may omit description entirely or send null; both mean
		// "this install wrote no description", so they must not split one tool
		// into two rows/cache entries that flip as the field appears.
		expect(toolInstanceKey("kb", null)).toBe(toolInstanceKey("kb", undefined));
	});
});

describe("ownSummaryBusy", () => {
	it("is true for each thing this panel itself started", () => {
		expect(ownSummaryBusy({ regeneratePending: true })).toBe(true);
		expect(ownSummaryBusy({ revisePending: true })).toBe(true);
		expect(ownSummaryBusy({ reviseJobActive: true })).toBe(true);
	});

	it("is false when this panel has nothing in flight", () => {
		expect(
			ownSummaryBusy({
				regeneratePending: false,
				revisePending: false,
				reviseJobActive: false,
			}),
		).toBe(false);
		expect(ownSummaryBusy({})).toBe(false);
	});

	it("cannot be made true by the OTHER tab's flag", () => {
		// THE pin for the mirror echo: whatever the parent hands this panel about
		// the install tab, it is not part of what this panel reports upward. An
		// extra key is ignored by construction -- if someone later folds
		// externalBusy back into this function, this test fails.
		expect(
			ownSummaryBusy({
				regeneratePending: false,
				revisePending: false,
				reviseJobActive: false,
				externalBusy: true,
			}),
		).toBe(false);
	});

	it("returns a real boolean, never a passed-through value", () => {
		// It feeds a `disabled` prop and a state setter; leaking undefined/0/""
		// would make React swap a controlled prop between defined and undefined.
		expect(ownSummaryBusy({ regeneratePending: undefined })).toBe(false);
		expect(ownSummaryBusy({ revisePending: "yes" })).toBe(true);
	});
});

describe("writeSummaryDetailIfPresent", () => {
	const detail = { summary: "新的總結", updated_at: null };

	it("writes the response over an existing entry", () => {
		expect(writeSummaryDetailIfPresent(detail)({ summary: "舊的" })).toBe(
			detail,
		);
		// Including entries whose current value is falsy but PRESENT -- null is a
		// real cached value here, not "no entry".
		expect(writeSummaryDetailIfPresent(detail)(null)).toBe(detail);
	});

	it("returns undefined when there is no entry, so setQueryData creates none", () => {
		// The delete race: removeQueries cleared this tool, but a regenerate
		// response was already on the wire. query-core's setQueryData bails on an
		// `undefined` updater result BEFORE queryCache.build, so this arrival is a
		// no-op instead of resurrecting the deleted tool's detail entry.
		expect(writeSummaryDetailIfPresent(detail)(undefined)).toBeUndefined();
	});
});

describe("summaryErrorRevalidates", () => {
	it("is true when the tool no longer answers to this name", () => {
		// 404: the tool no longer answers to this name (every summary route
		// resolves the package first).
		expect(summaryErrorRevalidates({ status: 404 })).toBe(true);
	});

	it("is true for a foreign job holding the single-flight slot", () => {
		// INVERTED from r4 (see summaryErrorRevalidates' own note). r4 read this
		// as "the job has written nothing yet, so nothing changed" -- true about
		// that job, and beside the point: the 409 tells us something OUTSIDE this
		// page is mid-operation on this backend, which is a fact about the world
		// we did not have a moment ago. Re-reading cannot make the foreign job
		// observable (we hold no id for it), so this only refreshes the CURRENT
		// truth; acting on its COMPLETION is the residual bounded by 裁決紀錄 #7.
		expect(
			summaryErrorRevalidates({ status: 409, code: "tool_job_in_progress" }),
		).toBe(true);
	});

	it("is false for failures that say nothing about tool state", () => {
		// Configuration and upstream health are not tool state, and a regenerate
		// raises both BEFORE the sidecar is touched.
		expect(
			summaryErrorRevalidates({ status: 503, code: "llm_not_configured" }),
		).toBe(false);
		expect(summaryErrorRevalidates({ status: 502 })).toBe(false);
		expect(summaryErrorRevalidates({ status: 500 })).toBe(false);
		// status 0 is the client's transport-failure shape (api/client.js).
		expect(summaryErrorRevalidates({ status: 0, code: "network_error" })).toBe(
			false,
		);
		expect(summaryErrorRevalidates({})).toBe(false);
		expect(summaryErrorRevalidates()).toBe(false);
	});

	it("keys the 409s on the CODE, not on the status alone", () => {
		// A future 409 code must default to "no evidence".
		expect(
			summaryErrorRevalidates({ status: 409, code: "some_future_code" }),
		).toBe(false);
		expect(summaryErrorRevalidates({ status: 409 })).toBe(false);
		// ...and the code does not license a refetch under another status.
		expect(
			summaryErrorRevalidates({
				status: 400,
				code: "tool_job_in_progress",
			}),
		).toBe(false);
	});
});

describe("logLinkSearch", () => {
	it("emits no ?log= at all without an id", () => {
		// The null-id case is a plain jump to the page, unchanged: a job whose
		// session never started has nothing to point at.
		expect(logLinkSearch(null, "tok")).toEqual({});
		expect(logLinkSearch(undefined, undefined)).toEqual({});
	});

	it("carries the id's process alongside it when the caller knows it", () => {
		expect(logLinkSearch(7, "abc123")).toEqual({
			log: 7,
			logProcess: "abc123",
		});
	});

	it("leaves a bare ?log= when there is no process to claim", () => {
		// The summary panel's link: its id is re-filtered against the answering
		// process on every read (backend routers/tools._summary_detail), so it
		// makes no claim and is resolved the way it always was.
		expect(logLinkSearch(7, null)).toEqual({ log: 7 });
		expect(logLinkSearch(7, undefined)).toEqual({ log: 7 });
	});
});

describe("deepLinkTarget", () => {
	const logs = [{ id: 7 }, { id: 6 }];
	const token = "process-a";

	it("decides nothing before the list (and its token) have landed", () => {
		expect(
			deepLinkTarget({
				logId: 7,
				logProcess: "process-b",
				logs: [],
				listLoaded: false,
				processToken: undefined,
			}),
		).toEqual({ mode: "none", value: "7" });
		expect(
			deepLinkTarget({
				logId: null,
				logProcess: null,
				logs,
				listLoaded: true,
				processToken: token,
			}),
		).toEqual({ mode: "none", value: null });
	});

	it("opens the row when the link's process is this one", () => {
		expect(
			deepLinkTarget({
				logId: 7,
				logProcess: token,
				logs,
				listLoaded: true,
				processToken: token,
			}),
		).toEqual({ mode: "in-list", value: "7" });
		// Off-list is still off-list, not foreign: the claim matches, the record
		// is merely older than this page.
		expect(
			deepLinkTarget({
				logId: 3,
				logProcess: token,
				logs,
				listLoaded: true,
				processToken: token,
			}),
		).toEqual({ mode: "off-list", value: "3" });
	});

	it("refuses a link minted by a previous backend run, id in the list or not", () => {
		// The whole point: id 7 IS on this page, and that is exactly what made the
		// misresolution silent -- the row and its detail agree with each other.
		expect(
			deepLinkTarget({
				logId: 7,
				logProcess: "process-b",
				logs,
				listLoaded: true,
				processToken: token,
			}),
		).toEqual({ mode: "foreign", value: "7" });
		expect(
			deepLinkTarget({
				logId: 3,
				logProcess: "process-b",
				logs,
				listLoaded: true,
				processToken: token,
			}),
		).toEqual({ mode: "foreign", value: "3" });
	});

	it("compares the claim as text, whatever the search parser made of it", () => {
		// The router coerces an all-digit search value to a Number (measured
		// against the installed @tanstack/react-router), so both sides are
		// stringified -- and a token that did NOT survive that round trip
		// intact falls on the refusing side.
		expect(
			deepLinkTarget({
				logId: 7,
				logProcess: 12345,
				logs,
				listLoaded: true,
				processToken: "12345",
			}),
		).toEqual({ mode: "in-list", value: "7" });
	});

	it("resolves a link that makes no claim exactly as before", () => {
		// A bare ?log= is the summary panel's link (backend-vouched at serve
		// time) or a URL someone kept; "we cannot say" must not be reported as
		// "we know it is stale".
		expect(
			deepLinkTarget({
				logId: 7,
				logProcess: null,
				logs,
				listLoaded: true,
				processToken: token,
			}),
		).toEqual({ mode: "in-list", value: "7" });
		expect(
			deepLinkTarget({
				logId: 3,
				logProcess: undefined,
				logs,
				listLoaded: true,
				processToken: token,
			}),
		).toEqual({ mode: "off-list", value: "3" });
	});
});
