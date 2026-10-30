import { describe, expect, it } from "vitest";
import {
	KNOWN_FAILURES,
	reviseStopView,
	toolJobStopReason,
} from "./toolJobStop.js";

// A finished revise as the poll returns it; each test states only the fields it
// is about.
const finished = (fields) => ({
	kind: "revise",
	state: "failed",
	outcome: "not_published",
	stopped_by: null,
	failure: null,
	rounds_used: null,
	max_rounds: null,
	handoff: null,
	operator_action: null,
	...fields,
});

describe("toolJobStopReason", () => {
	it("says nothing when the job carries no measured fact", () => {
		expect(toolJobStopReason(finished({}))).toBeNull();
		expect(toolJobStopReason(undefined)).toBeNull();
	});

	it("names the round budget and its setting", () => {
		expect(
			toolJobStopReason(
				finished({ stopped_by: "rounds", rounds_used: 24, max_rounds: 24 }),
			),
		).toBe("用完 24 次工具回合（TOOL_INSTALL_MAX_ROUNDS）");
	});

	it("still names the round budget without the counts", () => {
		expect(toolJobStopReason(finished({ stopped_by: "rounds" }))).toBe(
			"用完工具回合（TOOL_INSTALL_MAX_ROUNDS）",
		);
	});

	it("names the conversation budget and its setting", () => {
		expect(
			toolJobStopReason(
				finished({
					stopped_by: "conversation",
					rounds_used: 7,
					max_rounds: 24,
				}),
			),
		).toBe("對話大小超過上限（LLM_TOOL_CONVERSATION_BUDGET_TOKENS）");
	});

	it("tells a budget and the failure after it as two facts", () => {
		expect(
			toolJobStopReason(
				finished({
					stopped_by: "rounds",
					failure: "empty_response",
					rounds_used: 24,
					max_rounds: 24,
				}),
			),
		).toBe(
			"用完 24 次工具回合（TOOL_INSTALL_MAX_ROUNDS），收尾時 AI 回了空內容",
		);
		expect(
			toolJobStopReason(
				finished({ stopped_by: "conversation", failure: "invalid_output" }),
			),
		).toBe(
			"對話大小超過上限（LLM_TOOL_CONVERSATION_BUDGET_TOKENS），收尾時 AI 回覆的格式不正確，更正一次後仍然不對",
		);
	});

	it("puts no space before a Chinese failure after a budget", () => {
		expect(
			toolJobStopReason(
				finished({
					stopped_by: "rounds",
					failure: "deadline",
					rounds_used: 24,
					max_rounds: 24,
				}),
			),
		).toBe(
			"用完 24 次工具回合（TOOL_INSTALL_MAX_ROUNDS），收尾時整場的時間用完（TOOL_INSTALL_TIMEOUT_SECONDS）",
		);
	});

	it("names the session clock for the deadline, with how far it got", () => {
		expect(
			toolJobStopReason(
				finished({ failure: "deadline", rounds_used: 12, max_rounds: 24 }),
			),
		).toBe(
			"整場的時間用完（TOOL_INSTALL_TIMEOUT_SECONDS），當時已用 12／24 次工具回合",
		);
	});

	it("names the per-request timeout setting for a request timeout, not the session clock", () => {
		const reason = toolJobStopReason(
			finished({ failure: "request_timeout", rounds_used: 3, max_rounds: 24 }),
		);
		expect(reason).toBe(
			"單一 LLM 請求等候回應逾時（OPENAI_TIMEOUT_SECONDS），整場的時間還沒用完，當時已用 3／24 次工具回合",
		);
		expect(reason).toContain("OPENAI_TIMEOUT_SECONDS");
		expect(reason).not.toContain("TOOL_INSTALL_TIMEOUT_SECONDS");
	});

	it("names TOOL_INSTALL_TIMEOUT_SECONDS for the deadline and for no other failure", () => {
		expect(KNOWN_FAILURES).toContain("deadline");
		expect(KNOWN_FAILURES.length).toBeGreaterThan(1);
		for (const failure of [...KNOWN_FAILURES, "something_new"]) {
			for (const stopped_by of [null, "rounds", "conversation"]) {
				const reason = toolJobStopReason(
					finished({ failure, stopped_by, rounds_used: 5, max_rounds: 24 }),
				);
				expect(reason.includes("TOOL_INSTALL_TIMEOUT_SECONDS")).toBe(
					failure === "deadline",
				);
			}
		}
	});

	it("names OPENAI_TIMEOUT_SECONDS for a request timeout and for no other failure", () => {
		expect(KNOWN_FAILURES).toContain("request_timeout");
		expect(KNOWN_FAILURES.length).toBeGreaterThan(1);
		for (const failure of [...KNOWN_FAILURES, "something_new"]) {
			for (const stopped_by of [null, "rounds", "conversation"]) {
				const reason = toolJobStopReason(
					finished({ failure, stopped_by, rounds_used: 5, max_rounds: 24 }),
				);
				expect(reason.includes("OPENAI_TIMEOUT_SECONDS")).toBe(
					failure === "request_timeout",
				);
			}
		}
	});

	it("reports an upstream failure with how far the session had got", () => {
		expect(
			toolJobStopReason(
				finished({ failure: "upstream", rounds_used: 0, max_rounds: 24 }),
			),
		).toBe("LLM 服務回應錯誤，當時已用 0／24 次工具回合");
	});

	it("leaves the rounds out when the LLM was not configured", () => {
		expect(
			toolJobStopReason(
				finished({ failure: "not_configured", rounds_used: 0, max_rounds: 24 }),
			),
		).toBe("LLM 尚未設定");
	});

	it("reports a failure alone when the counts are missing", () => {
		expect(toolJobStopReason(finished({ failure: "empty_response" }))).toBe(
			"AI 回了空內容",
		);
	});

	it("shows a failure value it does not know as it is", () => {
		expect(
			toolJobStopReason(
				finished({ failure: "something_new", rounds_used: 1, max_rounds: 24 }),
			),
		).toBe("失敗（something_new），當時已用 1／24 次工具回合");
	});

	it("says the AI ended on its own, cut off by no limit, when nothing tripped", () => {
		expect(
			toolJobStopReason(finished({ rounds_used: 5, max_rounds: 24 })),
		).toBe("AI 自行結束，沒有被上限截斷（用了 5／24 次工具回合）");
	});
});

describe("reviseStopView", () => {
	const measured = {
		stopped_by: "rounds",
		failure: null,
		rounds_used: 24,
		max_rounds: 24,
	};

	it("gives the reason and both notes for a revise that did not publish", () => {
		expect(
			reviseStopView(
				finished({
					...measured,
					operator_action: "在 config.env 加上 PAGE_SIZE=50",
					handoff: "改了 run.py，第二頁還沒測",
				}),
				"revise",
			),
		).toEqual({
			reason: "用完 24 次工具回合（TOOL_INSTALL_MAX_ROUNDS）",
			operatorAction: "在 config.env 加上 PAGE_SIZE=50",
			handoff: "改了 run.py，第二頁還沒測",
		});
	});

	it("covers a failed revise too, with whatever it has", () => {
		expect(
			reviseStopView(
				finished({
					outcome: "failed",
					failure: "deadline",
					rounds_used: 9,
					max_rounds: 24,
				}),
				"revise",
			),
		).toEqual({
			reason:
				"整場的時間用完（TOOL_INSTALL_TIMEOUT_SECONDS），當時已用 9／24 次工具回合",
			operatorAction: null,
			handoff: null,
		});
	});

	it("says why a revise that did not publish stopped, even when no budget forced it", () => {
		expect(
			reviseStopView(finished({ rounds_used: 5, max_rounds: 24 }), "revise"),
		).toEqual({
			reason: "AI 自行結束，沒有被上限截斷（用了 5／24 次工具回合）",
			operatorAction: null,
			handoff: null,
		});
	});

	it("drops blank notes", () => {
		expect(
			reviseStopView(
				finished({ ...measured, operator_action: "  ", handoff: "\n" }),
				"revise",
			),
		).toEqual({
			reason: "用完 24 次工具回合（TOOL_INSTALL_MAX_ROUNDS）",
			operatorAction: null,
			handoff: null,
		});
	});

	it("is null when there is nothing to show", () => {
		expect(
			reviseStopView(finished({ operator_action: " " }), "revise"),
		).toBeNull();
	});

	// A published revise whose session ran to its own answer: the AI used 5 of
	// 24 rounds and no budget forced anything.
	const publishedOnItsOwn = {
		state: "succeeded",
		outcome: "published",
		stopped_by: null,
		failure: null,
		rounds_used: 5,
		max_rounds: 24,
	};

	it("shows a published revise's notes, and no stop reason when no budget forced it", () => {
		expect(
			reviseStopView(
				finished({
					...publishedOnItsOwn,
					operator_action: "在套件的 config.env 加上 PAGE_SIZE=50",
				}),
				"revise",
			),
		).toEqual({
			reason: null,
			operatorAction: "在套件的 config.env 加上 PAGE_SIZE=50",
			handoff: null,
		});
		expect(
			reviseStopView(
				finished({ ...publishedOnItsOwn, handoff: "第二頁的分頁還可以再測" }),
				"revise",
			),
		).toEqual({
			reason: null,
			operatorAction: null,
			handoff: "第二頁的分頁還可以再測",
		});
	});

	it("is null for a published revise that carries only the measured facts", () => {
		expect(
			reviseStopView(finished({ ...publishedOnItsOwn }), "revise"),
		).toBeNull();
	});

	it("names the budget that forced a published revise's answer", () => {
		expect(
			reviseStopView(
				finished({
					...measured,
					state: "succeeded",
					outcome: "published",
				}),
				"revise",
			),
		).toEqual({
			reason: "用完 24 次工具回合（TOOL_INSTALL_MAX_ROUNDS）",
			operatorAction: null,
			handoff: null,
		});
	});

	it("is null while the revise is still running", () => {
		expect(
			reviseStopView(
				finished({ ...measured, state: "running", outcome: null }),
				"revise",
			),
		).toBeNull();
		expect(reviseStopView(undefined, "revise")).toBeNull();
	});

	it("is null for an install, a health check or the publish of a draft", () => {
		expect(
			reviseStopView(finished({ ...measured, kind: "install" }), "revise"),
		).toBeNull();
		expect(
			reviseStopView(finished({ ...measured, kind: "health_check" }), "revise"),
		).toBeNull();
		expect(
			reviseStopView(
				finished({
					...measured,
					operator_action: "貼上 KEY=value",
					handoff: "還剩第二頁",
					kind: "publish_draft",
				}),
				"revise",
			),
		).toBeNull();
	});

	it("uses the caller's kind until the job names its own", () => {
		const job = { ...finished(measured), kind: undefined };
		expect(reviseStopView(job, "revise")?.reason).toBe(
			"用完 24 次工具回合（TOOL_INSTALL_MAX_ROUNDS）",
		);
		expect(reviseStopView(job, "install")).toBeNull();
	});
});
