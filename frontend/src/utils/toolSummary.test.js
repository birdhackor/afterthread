import { QueryClient } from "@tanstack/react-query";
import { describe, expect, it } from "vitest";
import {
	acceptSummaryForVersion,
	buildDiscardRequest,
	buildRegenerateRequest,
	buildReviseRequest,
	deepLinkTarget,
	logLinkSearch,
	ownSummaryBusy,
	summaryErrorRevalidates,
	toolIdentityConsumers,
	toolInstanceKey,
	toolLineageControls,
	toolSummaryKeyPrefix,
	toolSummaryQueryKey,
	versionWriteConflictReaction,
	writeSummaryDetailIfPresent,
} from "./toolSummary.js";

describe("toolSummaryQueryKey / toolSummaryKeyPrefix", () => {
	it("folds the canonical version identity in after the name", () => {
		const instanceKey = toolInstanceKey("kb_search", "20260728T010203Z-abc123");
		expect(toolSummaryQueryKey("kb_search", "20260728T010203Z-abc123")).toEqual(
			["tool-summary", "kb_search", instanceKey],
		);
	});

	it("keeps the prefix a real prefix of the exact key", () => {
		// The invalidate/remove call sites pass the PREFIX and rely on TanStack
		// Query's element-wise partial match to reach every instance of a name.
		// If these two ever stopped lining up, those calls would silently match
		// nothing -- the exact failure mode `exact: true` produced once the key
		// grew a third element.
		const prefix = toolSummaryKeyPrefix("kb_search");
		const exact = toolSummaryQueryKey("kb_search", "20260728T010203Z-abc123");
		expect(exact.slice(0, prefix.length)).toEqual(prefix);
	});

	it("separates two versions of the same tool", () => {
		expect(toolSummaryQueryKey("kb", "20260728T010203Z-abc123")).not.toEqual(
			toolSummaryQueryKey("kb", "20260728T020304Z-def456"),
		);
		// ...and the prefix still gathers both, which is what delete must clear.
		expect(toolSummaryKeyPrefix("kb")).toEqual(toolSummaryKeyPrefix("kb"));
	});
});

describe("Invariant H — all instance consumers share current_vid", () => {
	it("is a string (React keys are not arrays)", () => {
		expect(typeof toolInstanceKey("kb", "20260728T010203Z-abc123")).toBe(
			"string",
		);
	});

	it("changes when only the vid changes and descriptions are byte-identical", () => {
		const versionV = {
			name: "kb",
			description: "逐位元組相同的描述",
			current_vid: "20260728T010203Z-abc123",
		};
		const versionP = {
			...versionV,
			current_vid: "20260727T010203Z-def456",
		};

		expect(toolInstanceKey(versionV.name, versionV.current_vid)).not.toBe(
			toolInstanceKey(versionP.name, versionP.current_vid),
		);
		expect(versionV.description).toBe(versionP.description);
	});

	it("returns row, summary-cache and job attribution from one function", () => {
		const identity = toolIdentityConsumers("kb", "20260728T010203Z-abc123");
		const canonical = toolInstanceKey("kb", "20260728T010203Z-abc123");

		expect(identity).toEqual({
			rowKey: canonical,
			summaryQueryKey: ["tool-summary", "kb", canonical],
			jobAttributionKey: canonical,
		});
		expect(toolSummaryQueryKey("kb", "20260728T010203Z-abc123")).toEqual(
			identity.summaryQueryKey,
		);
	});

	it("separates two different names, and is stable for equal inputs", () => {
		const vid = "20260728T010203Z-abc123";
		expect(toolInstanceKey("a", vid)).not.toBe(toolInstanceKey("b", vid));
		expect(toolInstanceKey("kb", vid)).toBe(toolInstanceKey("kb", vid));
	});

	it("normalizes a missing current vid for the unresolved row", () => {
		expect(toolInstanceKey("kb", null)).toBe(toolInstanceKey("kb", undefined));
	});
});

describe("Invariant J — wrong-version summaries never enter a cache", () => {
	const versionV = "20260728T010203Z-abc123";
	const versionP = "20260727T010203Z-def456";

	it("returns the exact payload when it belongs to the row", () => {
		const detail = {
			current_vid: versionV,
			summary: "V 的總結",
			updated_at: null,
			llm_log_id: null,
		};
		expect(acceptSummaryForVersion(versionV, detail)).toBe(detail);
	});

	it("rejects a payload that actually read another version", () => {
		const detail = {
			current_vid: versionP,
			summary: "P 的總結",
			updated_at: null,
			llm_log_id: null,
		};
		expect(() => acceptSummaryForVersion(versionV, detail)).toThrowError(
			expect.objectContaining({
				name: "SummaryVersionMismatchError",
				code: "version_mismatch",
			}),
		);
	});

	it("leaves the version-keyed TanStack cache without payload data", async () => {
		const queryClient = new QueryClient({
			defaultOptions: { queries: { retry: false } },
		});
		const queryKey = ["tool-summary", "kb", versionV];
		const detail = {
			current_vid: versionP,
			summary: "P 的總結",
			updated_at: null,
			llm_log_id: null,
		};

		await expect(
			queryClient.fetchQuery({
				queryKey,
				queryFn: () => acceptSummaryForVersion(versionV, detail),
			}),
		).rejects.toMatchObject({ code: "version_mismatch" });
		expect(queryClient.getQueryData(queryKey)).toBeUndefined();
	});
});

describe("Invariant K — every version-specific write carries the vid", () => {
	const name = "kb_search";
	const currentVid = "20260728T010203Z-abc123";

	it("puts expected_vid in the revise body", () => {
		expect(
			buildReviseRequest({ name, currentVid, feedback: "限制為五筆" }),
		).toEqual({
			path: "/api/tools/kb_search/revise",
			body: {
				feedback: "限制為五筆",
				expected_vid: currentVid,
			},
		});
	});

	it("puts expected_vid in the regenerate body", () => {
		expect(buildRegenerateRequest({ name, currentVid })).toEqual({
			path: "/api/tools/kb_search/summary/regenerate",
			body: { expected_vid: currentVid },
		});
	});

	it("puts the expected vid in the discard path", () => {
		expect(buildDiscardRequest({ name, currentVid })).toEqual({
			path: `/api/tools/kb_search/versions/${currentVid}`,
		});
	});
});

describe("versionWriteConflictReaction", () => {
	it.each([
		["version_mismatch", "refresh"],
		["job_busy", "retry"],
		["lineage_unavailable", "delete-tool"],
	])("maps %s to its distinct reaction", (code, reaction) => {
		expect(versionWriteConflictReaction({ status: 409, code })).toBe(reaction);
	});

	it("never branches on a message or on status alone", () => {
		expect(
			versionWriteConflictReaction({
				status: 409,
				message: "工具版本已變更，請重新整理後再試",
			}),
		).toBeNull();
		expect(
			versionWriteConflictReaction({
				status: 400,
				code: "version_mismatch",
			}),
		).toBeNull();
	});
});

describe("toolLineageControls", () => {
	const current_vid = "20260728T010203Z-abc123";

	it.each([
		["sole", "delete-tool"],
		["usable", "discard-version"],
		["broken", "none"],
	])("keeps lineage %s as its own control state", (lineage, discardAction) => {
		expect(toolLineageControls({ current_vid, lineage })).toMatchObject({
			unresolved: false,
			discardAction,
			showBrokenLineage: lineage === "broken",
		});
	});

	it("allows only whole-tool deletion when no current version resolves", () => {
		expect(
			toolLineageControls({ current_vid: null, lineage: "broken" }),
		).toEqual({
			unresolved: true,
			showVersionControls: false,
			discardAction: "none",
			showBrokenLineage: false,
		});
	});

	it("turns a late lineage_unavailable into broken without a refetch", () => {
		expect(
			toolLineageControls({ current_vid, lineage: "usable" }, true),
		).toMatchObject({
			discardAction: "none",
			showBrokenLineage: true,
		});
	});

	it("never offers discard for an out-of-contract lineage value", () => {
		expect(
			toolLineageControls({ current_vid, lineage: "future-state" }),
		).toMatchObject({
			discardAction: "none",
			showBrokenLineage: true,
		});
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

	it("is false for failures that say nothing about tool state", () => {
		expect(summaryErrorRevalidates({ status: 409, code: "job_busy" })).toBe(
			false,
		);
		expect(
			summaryErrorRevalidates({
				status: 409,
				code: "lineage_unavailable",
			}),
		).toBe(false);
		expect(
			summaryErrorRevalidates({ status: 409, code: "version_mismatch" }),
		).toBe(false);
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

	it("does not treat any 409 as the generic broad revalidation path", () => {
		expect(
			summaryErrorRevalidates({ status: 409, code: "some_future_code" }),
		).toBe(false);
		expect(summaryErrorRevalidates({ status: 409 })).toBe(false);
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
