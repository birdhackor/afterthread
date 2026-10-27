import { describe, expect, it } from "vitest";
import {
	canRunExamples,
	exampleOutcomeMeta,
	verificationStateText,
	verificationView,
} from "./toolChecks.js";

const ran = (runs, overrides = {}) => ({
	status: "ran",
	message: null,
	vid: "20260728T010203Z-abcdef",
	runs,
	completed: runs.filter((run) => !["skipped", "not_run"].includes(run.outcome))
		.length,
	total: runs.length,
	budget_exhausted: false,
	finished: true,
	...overrides,
});

describe("verificationStateText", () => {
	it("says when a version has no examples or was never checked", () => {
		expect(verificationStateText({ state: "no_examples" }).text).toContain(
			"沒有範例",
		);
		expect(verificationStateText({ state: "never", total: 3 }).text).toBe(
			"有 3 個範例，尚未驗證",
		);
	});

	it("colors a pass, a failure and an incomplete run apart", () => {
		const at = "2026-10-27T01:02:03Z";
		expect(
			verificationStateText({
				state: "passed",
				passed: 3,
				total: 3,
				checked_at: at,
			}).color,
		).toBe("green");
		expect(
			verificationStateText({
				state: "failed",
				passed: 1,
				total: 3,
				checked_at: at,
			}).color,
		).toBe("red");
		expect(
			verificationStateText({ state: "incomplete", ran: 2, total: 3 }).text,
		).toBe("驗證未完成（跑了 2/3 個）");
	});

	it("is null with no current version", () => {
		expect(verificationStateText({ state: "no_version" })).toBeNull();
	});
});

describe("canRunExamples", () => {
	it("is offered only when there are examples to run", () => {
		expect(canRunExamples({ state: "never" })).toBe(true);
		expect(canRunExamples({ state: "failed" })).toBe(true);
		expect(canRunExamples({ state: "no_examples" })).toBe(false);
		expect(canRunExamples({ state: "examples_invalid" })).toBe(false);
		expect(canRunExamples(undefined)).toBe(false);
	});
});

describe("exampleOutcomeMeta", () => {
	it("names the two ways an example is never started", () => {
		expect(exampleOutcomeMeta("skipped").label).toContain("略過");
		expect(exampleOutcomeMeta("not_run").label).toContain("預算");
		expect(exampleOutcomeMeta("timeout").label).toBe("逾時");
	});
});

describe("verificationView", () => {
	it("shows progress while the examples run", () => {
		const view = verificationView({
			...ran([{ name: "a", outcome: "ok", run_id: 1 }]),
			total: 3,
			completed: 1,
			finished: false,
		});
		expect(view.tone).toBe("running");
		expect(view.title).toBe("正在跑範例（1/3）");
	});

	it("passes only when every example ran and succeeded", () => {
		expect(
			verificationView(
				ran([
					{ name: "a", outcome: "ok", run_id: 1 },
					{ name: "b", outcome: "ok", run_id: 2 },
				]),
			).tone,
		).toBe("passed");
	});

	it("fails on any failed example and offers its records for a fix", () => {
		const view = verificationView(
			ran([
				{ name: "a", outcome: "ok", run_id: 1 },
				{ name: "b", outcome: "failed", run_id: 2 },
				{ name: "c", outcome: "timeout", run_id: 3 },
				{ name: "d", outcome: "skipped", run_id: null },
			]),
		);
		expect(view.tone).toBe("failed");
		expect(view.title).toBe("範例驗證失敗（1/4 通過）");
		expect(view.failedRunIds).toEqual([2, 3]);
	});

	it("reports an incomplete run as such, budget or not", () => {
		const view = verificationView(
			ran(
				[
					{ name: "a", outcome: "ok", run_id: 1 },
					{ name: "b", outcome: "not_run", run_id: null },
				],
				{ budget_exhausted: true },
			),
		);
		expect(view.tone).toBe("incomplete");
		expect(view.title).toContain("預算用完");
	});

	it("says why nothing ran", () => {
		expect(
			verificationView({ status: "disabled", runs: [], finished: true }).title,
		).toBe("工具已停用，未驗證");
		expect(verificationView(null)).toBeNull();
	});
});
