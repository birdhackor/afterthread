import { describe, expect, it } from "vitest";
import { formatDuration, formatTimestamp } from "./format.js";

describe("formatTimestamp", () => {
	it("renders a date and a time in the viewer's zone", () => {
		expect(formatTimestamp("2026-10-27T01:02:03Z")).toMatch(
			/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/,
		);
	});

	it("degrades a missing or invalid value to a dash", () => {
		expect(formatTimestamp(null)).toBe("—");
		expect(formatTimestamp("not a date")).toBe("—");
	});
});

describe("formatDuration", () => {
	it("switches to seconds past one second", () => {
		expect(formatDuration(120)).toBe("120 ms");
		expect(formatDuration(12345)).toBe("12.3 s");
	});

	it("degrades a non-number to a dash", () => {
		expect(formatDuration(undefined)).toBe("—");
	});
});
