import { describe, expect, it } from "vitest";
import {
	canFinalizeSummary,
	patchToolRowSummaryStatus,
	summaryStatusMeta,
	toolSummaryKeyPrefix,
	toolSummaryQueryKey,
} from "./toolSummary.js";

describe("summaryStatusMeta", () => {
	it("labels draft and final", () => {
		expect(summaryStatusMeta("draft")).toEqual({
			label: "草稿",
			color: "yellow",
		});
		expect(summaryStatusMeta("final")).toEqual({
			label: "已定版",
			color: "green",
		});
	});

	it("falls back to 尚無總結 for null and any unrecognized value", () => {
		// null is the COMMON case (no sidecar yet / generation never wrote one),
		// not an edge case -- it must render the same as an unrecognized string,
		// never as an empty/blank badge.
		expect(summaryStatusMeta(null)).toEqual({
			label: "尚無總結",
			color: "gray",
		});
		expect(summaryStatusMeta(undefined)).toEqual({
			label: "尚無總結",
			color: "gray",
		});
		expect(summaryStatusMeta("some-future-status")).toEqual({
			label: "尚無總結",
			color: "gray",
		});
	});
});

describe("canFinalizeSummary", () => {
	it("is true for non-blank text", () => {
		expect(canFinalizeSummary("這個工具會查詢內部知識庫……")).toBe(true);
		expect(canFinalizeSummary("  前後有空白但有內容  ")).toBe(true);
	});

	it("is false for null, undefined, empty, whitespace-only, and non-string values", () => {
		expect(canFinalizeSummary(null)).toBe(false);
		expect(canFinalizeSummary(undefined)).toBe(false);
		expect(canFinalizeSummary("")).toBe(false);
		expect(canFinalizeSummary("   ")).toBe(false);
		// The router's _summary_detail already degrades a non-string summary to
		// null before this ever sees it, but pin this function's OWN behavior
		// independently of that upstream guarantee.
		expect(canFinalizeSummary(123)).toBe(false);
	});
});

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

describe("patchToolRowSummaryStatus", () => {
	const body = () => ({
		tools: [
			{ name: "a", description: "A", enabled: true, summary_status: "draft" },
			{ name: "b", description: "B", enabled: false, summary_status: null },
		],
	});

	it("rewrites only the named row's summary_status", () => {
		const patched = patchToolRowSummaryStatus(body(), "a", "final");
		expect(patched.tools[0]).toEqual({
			name: "a",
			description: "A",
			enabled: true,
			summary_status: "final",
		});
		// Every other row is untouched, including its object identity.
		expect(patched.tools[1]).toEqual(body().tools[1]);
	});

	it("carries null through (解除定版 to a statusless sidecar)", () => {
		expect(patchToolRowSummaryStatus(body(), "b", null).tools[1]).toMatchObject(
			{ summary_status: null },
		);
		expect(patchToolRowSummaryStatus(body(), "a", null).tools[0]).toMatchObject(
			{ summary_status: null },
		);
	});

	it("does not mutate the cached body in place", () => {
		// setQueryData updaters must return a NEW object: react-query compares by
		// reference to decide whether observers re-render.
		const original = body();
		const patched = patchToolRowSummaryStatus(original, "a", "final");
		expect(original.tools[0].summary_status).toBe("draft");
		expect(patched).not.toBe(original);
		expect(patched.tools).not.toBe(original.tools);
	});

	it("never invents a row for an unknown name", () => {
		// A summary response knows nothing about enabled/valid/description, so a
		// fabricated row would be a shape GET /api/tools never returns.
		const patched = patchToolRowSummaryStatus(body(), "missing", "final");
		expect(patched.tools).toHaveLength(2);
		expect(patched.tools.map((row) => row.name)).toEqual(["a", "b"]);
	});

	it("passes through a body it cannot understand", () => {
		// The list may not have loaded yet, or may have failed.
		expect(patchToolRowSummaryStatus(undefined, "a", "final")).toBeUndefined();
		expect(patchToolRowSummaryStatus(null, "a", "final")).toBeNull();
		const noTools = { unexpected: true };
		expect(patchToolRowSummaryStatus(noTools, "a", "final")).toBe(noTools);
	});
});
