import { describe, expect, it, vi } from "vitest";
import { KNOWN_DRAFT_STATES } from "./toolDraft.js";
import {
	failureNoticeView,
	fixDecision,
	fixRefusalMessage,
	requestFix,
	toolFailureGroups,
	toolFailureText,
	toolsJobSearch,
} from "./toolFailures.js";

const VID = "20260728T010203Z-abcdef";
const NEWER = "20260801T010203Z-123456";
const DRAFT_ID = "0123456789abcdef0123456789abcdef";
const JOB_ID = "fedcba9876543210fedcba9876543210";

// A draft as the list carries it, as much of it as these pages read.
const listDraft = (fields) => ({
	draft_id: DRAFT_ID,
	generation: 4,
	state: "open",
	base_vid: VID,
	...fields,
});

// A draft in every state of KNOWN_DRAFT_STATES, one in a state the page does not
// know, and an unusable draft whose manifest cannot be read, so it has no id.
function everyDraft() {
	expect(KNOWN_DRAFT_STATES.length).toBeGreaterThan(0);
	return [
		...[...KNOWN_DRAFT_STATES, "something_new"].map((state) =>
			listDraft({ state }),
		),
		listDraft({
			state: "unusable",
			draft_id: null,
			generation: null,
			base_vid: null,
		}),
	];
}

// What the reminder says when the backend refuses over a draft the list did not
// show (draft_pending); a draft the list shows is said in the same words.
const DRAFT_HERE = fixRefusalMessage(
	{ status: 409, code: "draft_pending" },
	"kb",
);

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
		expect(
			fixDecision(group, [{ name: "kb", current_vid: VID, draft: null }]),
		).toEqual({ kind: "submit", expectedVid: VID });
	});

	it("says the tool has a draft, and sends nothing, for any draft (every state of KNOWN_DRAFT_STATES, an unknown one, one with no id) whichever version failed", () => {
		for (const draft of everyDraft()) {
			const label = `${draft.state} ${draft.draft_id}`;
			expect(
				fixDecision(group, [{ name: "kb", current_vid: VID, draft }]),
				label,
			).toEqual({ kind: "draft", message: DRAFT_HERE });
			// The draft is what is said even when the failure was in an older version.
			expect(
				fixDecision(group, [{ name: "kb", current_vid: NEWER, draft }]),
				label,
			).toEqual({ kind: "draft", message: DRAFT_HERE });
		}
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

	it("sends a draft_pending refusal to the 工具 page in this page's own words", () => {
		const backend =
			"這個工具有一份可以接續的未完成修正（草稿）。請在工具列接續它，或取回需要的檔案後放棄它。";
		const message = fixRefusalMessage(
			{ status: 409, code: "draft_pending", message: backend },
			"kb",
		);
		expect(message).toContain("工具「kb」");
		expect(message).toContain("工具頁");
		expect(message).not.toBe(backend);
		// This page does not show the draft's state, so both ways out are named.
		expect(message).toContain("「接續修正」");
		expect(message).toContain("放棄");
		expect(
			fixRefusalMessage({
				status: 409,
				code: "draft_pending",
				message: backend,
			}),
		).toContain("這個工具");
	});

	it("names a tool deleted between the list read and the submit", () => {
		const gone = { status: 404, code: null, message: "找不到項目" };
		expect(fixRefusalMessage(gone, "kb")).toBe("工具「kb」已不存在。");
		expect(fixRefusalMessage({ status: 500, message: "boom" }, "kb")).toBe(
			"boom",
		);
	});
});

describe("requestFix", () => {
	const [group] = toolFailureGroups([failure()]);
	const tools =
		(...rows) =>
		async () =>
			rows;

	it("sends nothing for a tool with an open draft: it says why and leads to the 工具 page", async () => {
		const post = vi.fn(async () => ({ job_id: JOB_ID }));
		const state = await requestFix(group, {
			readTools: tools({ name: "kb", current_vid: VID, draft: listDraft({}) }),
			post,
		});
		expect(post).not.toHaveBeenCalled();
		expect(state).toEqual({ kind: "refused", message: DRAFT_HERE });
		// What it says: the tool has a draft, and on the 工具 page it is continued or
		// discarded.
		for (const words of [
			"工具「kb」",
			"未完成的修正（草稿）",
			"工具頁",
			"「接續修正」",
			"放棄",
		]) {
			expect(state.message, words).toContain(words);
		}
		expect(failureNoticeView(group, state)).toEqual({
			started: null,
			// Still there: pressed again, it reads the list again.
			fix: { checking: false },
			link: { label: "到工具頁查看", search: {} },
			refusal: DRAFT_HERE,
		});
	});

	it("sends nothing for a tool with any draft (every state of KNOWN_DRAFT_STATES, an unknown one, one with no id)", async () => {
		for (const draft of everyDraft()) {
			const post = vi.fn(async () => ({ job_id: JOB_ID }));
			const state = await requestFix(group, {
				readTools: tools({ name: "kb", current_vid: VID, draft }),
				post,
			});
			expect(post, draft.state).not.toHaveBeenCalled();
			expect(state, draft.state).toEqual({
				kind: "refused",
				message: DRAFT_HERE,
			});
		}
	});

	it("sends the revise of the failed version, naming no draft, for a tool without one", async () => {
		const post = vi.fn(async () => ({ job_id: JOB_ID }));
		const state = await requestFix(group, {
			readTools: tools({ name: "kb", current_vid: VID, draft: null }),
			post,
		});
		expect(post.mock.calls).toEqual([
			["/api/tools/kb/revise", { expected_vid: VID, run_ids: [1] }],
		]);
		expect(state).toEqual({ kind: "started", jobId: JOB_ID });
	});

	it("says a draft the list did not show in the words it uses for one it shows", async () => {
		const post = vi.fn(async () => {
			throw { status: 409, code: "draft_pending", message: "the backend's" };
		});
		expect(
			await requestFix(group, {
				readTools: tools({ name: "kb", current_vid: VID }),
				post,
			}),
		).toEqual({ kind: "refused", message: DRAFT_HERE });
	});

	it("reads the list again once after a version_mismatch, and decides on that list", async () => {
		const mismatch = { status: 409, code: "version_mismatch" };
		const plain = { name: "kb", current_vid: VID };

		const readTools = vi
			.fn()
			.mockResolvedValueOnce([plain])
			.mockResolvedValueOnce([plain]);
		const post = vi
			.fn()
			.mockRejectedValueOnce(mismatch)
			.mockResolvedValueOnce({ job_id: JOB_ID });
		expect(await requestFix(group, { readTools, post })).toEqual({
			kind: "started",
			jobId: JOB_ID,
		});
		expect(readTools).toHaveBeenCalledTimes(2);

		// A draft on the list read again is not sent to.
		const drafted = vi.fn().mockRejectedValueOnce(mismatch);
		const reread = vi
			.fn()
			.mockResolvedValueOnce([plain])
			.mockResolvedValueOnce([{ ...plain, draft: listDraft({}) }]);
		expect(
			await requestFix(group, { readTools: reread, post: drafted }),
		).toEqual({ kind: "refused", message: DRAFT_HERE });
		expect(drafted).toHaveBeenCalledTimes(1);

		// A second mismatch is reported.
		const twice = vi.fn().mockRejectedValue(mismatch);
		expect(
			await requestFix(group, { readTools: tools(plain), post: twice }),
		).toEqual({
			kind: "refused",
			message: "工具版本剛剛又變了，請到工具頁確認後再試。",
		});
		expect(twice).toHaveBeenCalledTimes(2);
	});

	it("sends nothing when the list cannot be read", async () => {
		const post = vi.fn(async () => ({ job_id: JOB_ID }));
		const state = await requestFix(group, {
			readTools: async () => {
				throw { status: 500, message: "boom" };
			},
			post,
		});
		expect(post).not.toHaveBeenCalled();
		expect(state).toEqual({ kind: "refused", message: "boom" });
	});
});

describe("failureNoticeView", () => {
	it("offers 請 AI 修正 only for recorded failures, and always the 工具 page", () => {
		const [recorded] = toolFailureGroups([failure()]);
		const [unrecorded] = toolFailureGroups([
			failure({ run_id: null, recorded: false }),
		]);
		const toTools = { label: "到工具頁查看", search: {} };
		expect(failureNoticeView(recorded, { kind: "idle" })).toEqual({
			started: null,
			fix: { checking: false },
			link: toTools,
			refusal: null,
		});
		expect(failureNoticeView(recorded, { kind: "checking" }).fix).toEqual({
			checking: true,
		});
		expect(failureNoticeView(unrecorded, { kind: "idle" })).toEqual({
			started: null,
			fix: null,
			link: toTools,
			refusal: null,
		});
	});

	it("leads a started fix to its job's progress on the 工具 page, and offers no second fix", () => {
		const [group] = toolFailureGroups([failure()]);
		expect(
			failureNoticeView(group, { kind: "started", jobId: JOB_ID }),
		).toEqual({
			started: "已請 AI 修正「kb」。",
			fix: null,
			link: { label: "到工具頁看進度", search: { job: JOB_ID } },
			refusal: null,
		});
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
