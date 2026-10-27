import { describe, expect, it } from "vitest";
import {
	lastRunHint,
	runDetailSections,
	runHasDetails,
	runOutcomeMeta,
	runWorkflowLabel,
	toolRunQueryKey,
	toolRunsQueryKey,
	vidStateMeta,
} from "./toolRuns.js";

const CURRENT = "20260728T010203Z-abcdef";
const OLDER = "20260727T010203Z-123456";

const lastRun = (overrides = {}) => ({
	id: 1,
	outcome: "failed",
	started_at: "2026-10-27T01:02:03Z",
	vid: CURRENT,
	fixable: true,
	...overrides,
});

describe("query keys", () => {
	it("address a tool's list by name and a record by id", () => {
		expect(toolRunsQueryKey("kb")).toEqual(["tool-runs", "kb"]);
		expect(toolRunQueryKey(7)).toEqual(["tool-run", 7]);
	});
});

describe("runOutcomeMeta", () => {
	it("names an overflow kill instead of the exit -9 the model saw", () => {
		expect(runOutcomeMeta("output_overflow").label).toBe("輸出超過上限被終止");
	});

	it("shows a refusal in gray, since nothing failed", () => {
		expect(runOutcomeMeta("refused").color).toBe("gray");
	});

	it("falls back to the raw value for an unknown outcome", () => {
		expect(runOutcomeMeta("new_kind")).toEqual({
			label: "new_kind",
			color: "gray",
		});
	});
});

describe("vidStateMeta and runWorkflowLabel", () => {
	it("label the three version states", () => {
		expect(vidStateMeta("current").label).toBe("目前版本");
		expect(vidStateMeta("older").label).toBe("舊版本");
		expect(vidStateMeta("gone").label).toBe("版本已不存在");
	});

	it("share the AI 日誌 workflow labels, unknown falling back to 其他", () => {
		expect(runWorkflowLabel("capture")).toBe("快速捕捉");
		expect(runWorkflowLabel("something_new")).toBe("其他");
	});
});

describe("lastRunHint", () => {
	it("is null for a tool that has no run yet", () => {
		expect(lastRunHint(null, CURRENT)).toBeNull();
	});

	it("names a backend error as the backend's, not the tool's", () => {
		expect(
			lastRunHint(lastRun({ outcome: "error", fixable: false }), CURRENT),
		).toEqual({
			text: "最近一次呼叫在後端出錯",
			color: "orange",
		});
	});

	it("is quiet for a success and a refusal", () => {
		expect(lastRunHint(lastRun({ outcome: "ok" }), CURRENT).color).toBe(
			"dimmed",
		);
		expect(lastRunHint(lastRun({ outcome: "refused" }), CURRENT).text).toBe(
			"最近一次呼叫被拒，工具沒有執行",
		);
	});

	it("reports a failure of the current version in red", () => {
		expect(lastRunHint(lastRun({ outcome: "timeout" }), CURRENT)).toEqual({
			text: "最近一次執行逾時",
			color: "red",
		});
	});

	it("says when a failure happened on an older version", () => {
		expect(
			lastRunHint(lastRun({ vid: OLDER, fixable: false }), CURRENT),
		).toEqual({
			text: "最近一次執行失敗（發生在舊版本）",
			color: "orange",
		});
	});

	it("does not claim an older version when there is no current one", () => {
		expect(
			lastRunHint(lastRun({ vid: OLDER, fixable: false }), null).text,
		).toBe("最近一次執行失敗");
	});
});

describe("runHasDetails and runDetailSections", () => {
	it("offers details for bodies or for the model's string", () => {
		expect(runHasDetails({ has_details: false, message: null })).toBe(false);
		expect(runHasDetails({ has_details: false, message: "tool not run" })).toBe(
			true,
		);
		expect(runHasDetails({ has_details: true, message: null })).toBe(true);
	});

	it("lists what the record kept, naming which end of each output it kept", () => {
		const sections = runDetailSections({
			message: "tool failed (exit 1): boom",
			args: "{}",
			stderr: "boom",
			stdout: null,
		});
		expect(sections.map((section) => section.key)).toEqual([
			"message",
			"args",
			"stderr",
		]);
		expect(sections[2].label).toContain("結尾");
	});

	it("keeps an output that printed nothing, marked empty", () => {
		const sections = runDetailSections({
			message: "tool failed (exit 2): ",
			args: "{}",
			stderr: "",
			stdout: "",
		});
		expect(sections.map((section) => [section.key, section.empty])).toEqual([
			["message", false],
			["args", false],
			["stderr", true],
			["stdout", true],
		]);
	});
});
