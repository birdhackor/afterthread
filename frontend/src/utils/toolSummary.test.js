import { describe, expect, it } from "vitest";
import { canFinalizeSummary, summaryStatusMeta } from "./toolSummary.js";

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
