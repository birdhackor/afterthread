import { describe, expect, it } from "vitest";
import {
	fixDecision,
	fixRefusalMessage,
	toolFailureGroups,
	toolFailureText,
	toolsJobSearch,
} from "./toolFailures.js";

const VID = "20260728T010203Z-abcdef";
const NEWER = "20260801T010203Z-123456";

const failure = (overrides = {}) => ({
	run_id: 1,
	tool_name: "kb",
	vid: VID,
	outcome: "failed",
	recorded: true,
	...overrides,
});

describe("toolFailureGroups", () => {
	it("groups per tool, keeping at most the three newest recorded runs", () => {
		const groups = toolFailureGroups([
			failure({ run_id: 1 }),
			failure({ tool_name: "web", run_id: 2, outcome: "timeout" }),
			failure({ run_id: 3 }),
			failure({ run_id: 4, outcome: "timeout" }),
			failure({ run_id: 5 }),
		]);
		expect(groups.map((group) => group.toolName)).toEqual(["kb", "web"]);
		expect(groups[0].runIds).toEqual([3, 4, 5]);
		expect(groups[0].count).toBe(4);
		expect(groups[0].outcomes).toEqual(["failed", "timeout"]);
	});

	it("counts failures whose record was not written apart", () => {
		const [group] = toolFailureGroups([
			failure({ run_id: null, recorded: false }),
		]);
		expect(group.runIds).toEqual([]);
		expect(group.unrecorded).toBe(1);
	});

	it("is empty without failures", () => {
		expect(toolFailureGroups(undefined)).toEqual([]);
	});
});

describe("toolFailureText", () => {
	it("names the tool, how often and how it failed", () => {
		const [group] = toolFailureGroups([
			failure(),
			failure({ run_id: 2, outcome: "output_overflow" }),
		]);
		expect(toolFailureText(group)).toBe(
			"這次 AI 處理呼叫的工具「kb」執行失敗 2 次（失敗、輸出超過上限被終止），AI 的結果可能因此不完整。",
		);
	});
});

describe("fixDecision", () => {
	const [group] = toolFailureGroups([failure()]);

	it("sends a revise when the failed version is still the current one", () => {
		expect(fixDecision(group, [{ name: "kb", current_vid: VID }])).toEqual({
			kind: "submit",
			expectedVid: VID,
		});
	});

	it("does not send when the tool has moved on or is gone", () => {
		expect(fixDecision(group, [{ name: "kb", current_vid: NEWER }]).kind).toBe(
			"older",
		);
		expect(fixDecision(group, []).kind).toBe("gone");
	});

	it("does not send when no record was written", () => {
		const [unrecorded] = toolFailureGroups([
			failure({ run_id: null, recorded: false }),
		]);
		expect(
			fixDecision(unrecorded, [{ name: "kb", current_vid: VID }]).kind,
		).toBe("unrecorded");
	});
});

describe("fixRefusalMessage", () => {
	it("explains each refusal the revise submit can return", () => {
		expect(fixRefusalMessage({ status: 409, code: "job_busy" })).toContain(
			"其他 AI 工作",
		);
		expect(
			fixRefusalMessage({
				status: 409,
				code: "env_policy",
				message: "移到 config.env",
			}),
		).toBe("移到 config.env");
		expect(fixRefusalMessage({ status: 404, code: "run_not_found" })).toContain(
			"已被清除",
		);
	});

	it("names a tool deleted between the list read and the submit", () => {
		const gone = { status: 404, code: null, message: "找不到項目" };
		expect(fixRefusalMessage(gone, "kb")).toBe("工具「kb」已不存在。");
		expect(fixRefusalMessage({ status: 500, message: "boom" }, "kb")).toBe(
			"boom",
		);
	});
});

describe("toolsJobSearch", () => {
	it("keeps a job id and drops anything else", () => {
		const id = "0123456789abcdef0123456789abcdef";
		expect(toolsJobSearch({ job: id })).toEqual({ job: id });
		expect(toolsJobSearch({ job: "../x" })).toEqual({});
		expect(toolsJobSearch({ job: 12 })).toEqual({});
		expect(toolsJobSearch(undefined)).toEqual({});
	});
});
