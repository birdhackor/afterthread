import { describe, expect, it } from "vitest";
import { toolApiPath, toolJobApiPath } from "./toolPaths.js";

// A name as the backend lists one whose bytes are not UTF-8: three U+FFFD for
// the byte.
const NOT_UTF8 = `pkg-${String.fromCodePoint(0xfffd).repeat(3)}`;

describe("toolApiPath", () => {
	it("puts a tool's name in the path as one segment, whatever characters it holds", () => {
		const cases = [
			["kb#x", "/api/tools/kb%23x"],
			["kb?x", "/api/tools/kb%3Fx"],
			["kb%x", "/api/tools/kb%25x"],
			["kb x", "/api/tools/kb%20x"],
			["kb/x", "/api/tools/kb%2Fx"],
			[NOT_UTF8, "/api/tools/pkg-%EF%BF%BD%EF%BF%BD%EF%BF%BD"],
		];
		for (const [name, path] of cases) {
			expect(toolApiPath(name)).toBe(path);
			// As a browser resolves it: nothing is cut off into a query or a fragment,
			// and the name comes back whole from its one segment.
			const url = new URL(
				toolApiPath(name, "settings"),
				"http://127.0.0.1:8000",
			);
			expect([url.search, url.hash]).toEqual(["", ""]);
			const segments = url.pathname.split("/");
			expect(segments).toHaveLength(5);
			expect(decodeURIComponent(segments[3])).toBe(name);
			expect(segments[4]).toBe("settings");
		}
	});

	it("adds a route's own words as segments after the name", () => {
		expect(toolApiPath("kb_search", "summary", "regenerate")).toBe(
			"/api/tools/kb_search/summary/regenerate",
		);
		expect(toolApiPath("kb", "versions", "20260728T010203Z-abc123")).toBe(
			"/api/tools/kb/versions/20260728T010203Z-abc123",
		);
	});
});

describe("toolJobApiPath", () => {
	it("puts a job id in the path as one segment", () => {
		expect(toolJobApiPath("0123abcd")).toBe("/api/tools/jobs/0123abcd");
		expect(toolJobApiPath("a#b")).toBe("/api/tools/jobs/a%23b");
	});
});
