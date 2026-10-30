import { QueryClient, QueryObserver } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";
import {
	buildDraftDiscardRequest,
	continuationNote,
	DRAFT_SUPERSEDED,
	draftContinuation,
	draftDiscardConfirmation,
	draftDiscardFailure,
	draftDiscardNotification,
	draftView,
	jobDraftView,
	KNOWN_DRAFT_STATES,
	recordFixView,
	reviseBlockedByDraft,
	reviseStartedMessage,
	reviseVariables,
} from "./toolDraft.js";
import { fixRefusalMessage } from "./toolFailures.js";
import { KNOWN_FAILURES, toolJobStopReason } from "./toolJobStop.js";
import {
	listReadAfter,
	markListReads,
	readToolList,
	rereadToolList,
} from "./toolList.js";
import { buildReviseRequest, toolIdentityConsumers } from "./toolSummary.js";

// A copy of ToolDraft.state in backend/afterthread/schemas.py, kept by hand: the
// vocabulary test below holds the toolbar's wording to this list, so it is this
// list that has to learn a state the backend gains.
const BACKEND_DRAFT_STATES = [
	"running",
	"unusable",
	"publishing",
	"unknown",
	"published",
	"stale",
	"open",
];

const DRAFT_ID = "0123456789abcdef0123456789abcdef";
const OTHER_DRAFT_ID = "fedcba9876543210fedcba9876543210";
const TOKEN = "f".repeat(64);
const BASE_VID = "20260728T010203Z-abcdef";
const FILES_PATH = "/srv/tools/kb/.afterthread.meta/draft/files-0a1b2c";
const DRAFT_DIR = "/srv/tools/kb/.afterthread.meta/draft";
const KEPT_A = "/srv/tools/kb/.afterthread.meta/draft/files-99aa00";
const KEPT_B = "/srv/tools/kb/.afterthread.meta/draft/files-77bb11";
const RETAINED = "/srv/tools/.staging/0123456789abcdef/build";

const lastRound = (fields) => ({
	stopped_by: null,
	failure: null,
	outcome: "not_published",
	finished_at: "2026-10-29T16:04:08.538337+00:00",
	operator_action: null,
	summary: null,
	omitted: 0,
	...fields,
});

// A draft as the tools list returns it; each test states only what it is about.
const draft = (fields) => ({
	draft_id: DRAFT_ID,
	generation: 3,
	attempts: 2,
	state: "open",
	interrupted: false,
	base_vid: BASE_VID,
	path: FILES_PATH,
	draft_dir: DRAFT_DIR,
	kept: [],
	discard_token: TOKEN,
	last: lastRound({}),
	...fields,
});

// A draft whose manifest cannot be read: nothing about it is known but where it
// is and the token that names it.
const unreadable = () =>
	draft({
		state: "unusable",
		draft_id: null,
		generation: null,
		attempts: null,
		base_vid: null,
		path: DRAFT_DIR,
		last: null,
	});

// A draft whose manifest reads but whose files directory is gone: still
// "unusable", with everything the manifest said.
const filesGone = () => draft({ state: "unusable", path: DRAFT_DIR });

const stubTime = (value) => `<${value}>`;

describe("the draft state vocabulary", () => {
	it("words exactly the states the backend reports", () => {
		expect([...KNOWN_DRAFT_STATES].sort()).toEqual(
			[...BACKEND_DRAFT_STATES].sort(),
		);
	});

	it.each(BACKEND_DRAFT_STATES)("reads %s as a state it knows", (state) => {
		const view = draftView(draft({ state }));
		expect(view.title).not.toContain(state);
		expect(view.note).not.toBe("");
	});

	it("shows a state it does not know as it is, with nothing to press", () => {
		const view = draftView(draft({ state: "something_new" }));
		expect(view.title).toContain("something_new");
		expect(view.buttons).toEqual([]);
		expect(view.path).toBe(FILES_PATH);
	});
});

describe("draftView buttons", () => {
	const OFFERED = {
		open: ["continue", "continue_with_note", "publish", "discard"],
		stale: ["discard"],
		unusable: ["discard"],
		published: ["discard"],
		unknown: ["discard"],
		running: [],
		publishing: [],
	};

	it.each(BACKEND_DRAFT_STATES)("offers exactly what %s allows", (state) => {
		expect(draftView(draft({ state })).buttons).toEqual(OFFERED[state]);
	});

	it("offers the continue buttons for an open draft and for no other state", () => {
		for (const state of BACKEND_DRAFT_STATES) {
			const { buttons } = draftView(draft({ state }));
			expect(buttons.includes("continue"), state).toBe(state === "open");
			expect(buttons.includes("continue_with_note"), state).toBe(
				state === "open",
			);
		}
	});

	it("offers 發布草稿 exactly when it offers the continue buttons", () => {
		const shapes = [
			{},
			{ draft_id: null },
			{ generation: null },
			{ generation: 0 },
			{ base_vid: " " },
		];
		for (const state of BACKEND_DRAFT_STATES) {
			for (const fields of shapes) {
				const { buttons } = draftView(draft({ state, ...fields }));
				const where = `${state} ${JSON.stringify(fields)}`;
				expect(buttons.includes("publish"), where).toBe(
					buttons.includes("continue"),
				);
				expect(buttons.includes("publish"), where).toBe(
					buttons.includes("continue_with_note"),
				);
			}
		}
	});

	it("offers 放棄 and nothing else for the states that can only be discarded", () => {
		for (const state of ["stale", "unusable", "published", "unknown"]) {
			expect(draftView(draft({ state })).buttons).toEqual(["discard"]);
		}
	});

	it("offers nothing while a round works on the draft or a publish is going", () => {
		expect(draftView(draft({ state: "running" })).buttons).toEqual([]);
		expect(draftView(draft({ state: "publishing" })).buttons).toEqual([]);
	});

	it("does not offer to continue an open draft that does not name itself", () => {
		expect(draftView(draft({ draft_id: null })).buttons).toEqual(["discard"]);
		expect(draftView(draft({ base_vid: null })).buttons).toEqual(["discard"]);
	});

	it("is null for a tool without a draft", () => {
		expect(draftView(null)).toBeNull();
		expect(draftView(undefined)).toBeNull();
	});
});

describe("draftView consequence", () => {
	it("says an open draft is where the tool's fixes go on from", () => {
		const { consequence } = draftView(draft({ state: "open" }));
		expect(consequence).toContain("接著做");
		expect(consequence).toContain("放棄");
	});

	it("says any other draft is in the way until it is discarded", () => {
		for (const state of ["stale", "unusable", "published", "unknown"]) {
			const { consequence } = draftView(draft({ state }));
			expect(consequence, state).toBe(
				"放棄這份草稿之前，這個工具不能送出新的修正。",
			);
		}
		expect(draftView(draft({ state: "running" })).consequence).toBeNull();
	});
});

describe("draftContinuation", () => {
	it("names an open draft the way a round's request names it", () => {
		expect(draftContinuation(draft({}))).toEqual({
			draftId: DRAFT_ID,
			generation: 3,
			baseVid: BASE_VID,
		});
	});

	it("is null for a draft in any other state", () => {
		for (const state of BACKEND_DRAFT_STATES.filter((s) => s !== "open")) {
			expect(draftContinuation(draft({ state })), state).toBeNull();
		}
		expect(draftContinuation(draft({ state: "something_new" }))).toBeNull();
		expect(draftContinuation(null)).toBeNull();
	});

	it("is null for an open draft missing its id, generation or base version", () => {
		expect(draftContinuation(draft({ draft_id: null }))).toBeNull();
		expect(draftContinuation(draft({ generation: null }))).toBeNull();
		expect(draftContinuation(draft({ generation: 0 }))).toBeNull();
		expect(draftContinuation(draft({ base_vid: " " }))).toBeNull();
	});
});

describe("continuationNote", () => {
	it("sends the trimmed words, and nothing when there are none", () => {
		expect(continuationNote("  先試第二頁  ")).toEqual({
			feedback: "先試第二頁",
			error: null,
		});
		expect(continuationNote("")).toEqual({ feedback: "", error: null });
		expect(continuationNote(undefined)).toEqual({ feedback: "", error: null });
	});

	it("bounds the words by code point, like every other AI input", () => {
		// 20,000 characters outside the BMP are 40,000 UTF-16 units: still allowed.
		expect(continuationNote("😀".repeat(20_000)).error).toBeNull();
		const tooLong = continuationNote("字".repeat(20_001));
		expect(tooLong.error).toContain("20000");
		expect(tooLong.feedback).toBeNull();
	});
});

// The backend refuses a revise that starts over for a tool that has a draft,
// whatever the draft's state (409 draft_pending). This says so before the click,
// and every control that would send one draws its answer.
describe("reviseBlockedByDraft", () => {
	it("lets a tool without a draft send a revise", () => {
		expect(reviseBlockedByDraft(null)).toBeNull();
		expect(reviseBlockedByDraft(undefined)).toBeNull();
	});

	it("blocks a revise for a draft in every state of KNOWN_DRAFT_STATES", () => {
		expect(KNOWN_DRAFT_STATES.length).toBeGreaterThan(0);
		for (const state of KNOWN_DRAFT_STATES) {
			expect(reviseBlockedByDraft(draft({ state })), state).not.toBeNull();
		}
	});

	it("blocks a revise for a draft in a state it does not know", () => {
		expect(
			reviseBlockedByDraft(draft({ state: "something_new" })),
		).not.toBeNull();
	});

	it("blocks a revise for an unusable draft that has no id", () => {
		expect(unreadable().draft_id).toBeNull();
		expect(reviseBlockedByDraft(unreadable())).not.toBeNull();
		expect(reviseBlockedByDraft(filesGone())).not.toBeNull();
	});

	it("points an open draft at 接續修正, and no other draft", () => {
		expect(reviseBlockedByDraft(draft({ state: "open" }))).toContain(
			"「接續修正」",
		);
		for (const state of KNOWN_DRAFT_STATES.filter((s) => s !== "open")) {
			expect(reviseBlockedByDraft(draft({ state })), state).not.toContain(
				"接續",
			);
		}
	});

	it("says what to do now, and names no button the draft's state does not offer", () => {
		for (const state of KNOWN_DRAFT_STATES) {
			const reason = reviseBlockedByDraft(draft({ state }));
			expect(reason, state).toContain("路徑");
			expect(reason, state).toContain("放棄");
			// Only an open draft has 發布草稿.
			if (state !== "open") {
				expect(reason, state).not.toContain("發布草稿");
			}
		}
	});

	it("uses the words the draft_pending refusal uses", () => {
		const refusal = fixRefusalMessage(
			{ status: 409, code: "draft_pending" },
			"kb",
		);
		for (const state of ["open", "stale"]) {
			const reason = reviseBlockedByDraft(draft({ state }));
			for (const phrase of ["未完成的修正（草稿）", "取回", "放棄"]) {
				expect(refusal, phrase).toContain(phrase);
				expect(reason, `${state} ${phrase}`).toContain(phrase);
			}
		}
		expect(refusal).toContain("「接續修正」");
	});
});

describe("recordFixView", () => {
	it("is the fix that starts a revise for a tool without a draft", () => {
		expect(recordFixView(null)).toMatchObject({
			label: "請 AI 修正",
			noteSubmit: "送出修正",
			continuation: null,
			blockedReason: null,
		});
	});

	it("continues an open draft and attaches the record", () => {
		const fix = recordFixView(draft({}));
		expect(fix.label).toBe("接續修正（附上這筆）");
		expect(fix.continuation).toEqual(draftContinuation(draft({})));
		expect(fix.blockedReason).toBeNull();
		expect(fix.noteTitle).toContain("接續");
		expect(fix.noteSubmit).toBe("接續修正");
	});

	it("keeps the plain fix, off and with the reason, for a draft in any other state", () => {
		for (const state of KNOWN_DRAFT_STATES.filter((s) => s !== "open")) {
			const fix = recordFixView(draft({ state }));
			expect(fix.label, state).toBe("請 AI 修正");
			expect(fix.continuation, state).toBeNull();
			expect(fix.blockedReason, state).toBe(
				reviseBlockedByDraft(draft({ state })),
			);
		}
		expect(recordFixView(unreadable()).blockedReason).not.toBeNull();
	});
});

describe("reviseVariables: a dialog sends the revise it opened on", () => {
	// A row of the tools list, carrying `draftOnRow`.
	const row = (draftOnRow) => ({
		name: "kb",
		current_vid: BASE_VID,
		draft: draftOnRow,
	});
	// The request the page's mutation makes of what is sent.
	const sent = (tool, target, feedback) =>
		buildReviseRequest(reviseVariables(tool, target, feedback));
	// What the dialogs keep when they open: 加上說明後接續… the draft on screen, a
	// record's 加上說明… what recordFixView says for the draft on screen, with the
	// record.
	const continueDialog = (onScreen) => ({
		continuation: draftContinuation(onScreen),
		runIds: [],
	});
	const recordDialog = (onScreen, runId) => ({
		continuation: recordFixView(onScreen).continuation,
		runIds: [runId],
	});

	it("continues the draft it opened on when the list shows another one now", () => {
		const opened = continueDialog(draft({}));
		// Discarded in another tab, and another made from the same version.
		const now = row(draft({ draft_id: OTHER_DRAFT_ID, generation: 1 }));
		expect(sent(now, opened, "written for the first")).toEqual({
			path: "/api/tools/kb/revise",
			body: {
				expected_vid: BASE_VID,
				draft_id: DRAFT_ID,
				draft_generation: 3,
				feedback: "written for the first",
			},
		});
	});

	it("continues the draft it opened on when the list shows no draft now", () => {
		const opened = recordDialog(draft({}), 7);
		expect(sent(row(null), opened, "written for it")).toEqual({
			path: "/api/tools/kb/revise",
			body: {
				expected_vid: BASE_VID,
				draft_id: DRAFT_ID,
				draft_generation: 3,
				feedback: "written for it",
				run_ids: [7],
			},
		});
	});

	it("starts over, naming no draft, when it opened on none and the list shows one now", () => {
		const opened = recordDialog(null, 7);
		expect(sent(row(draft({})), opened, "no draft then")).toEqual({
			path: "/api/tools/kb/revise",
			body: {
				expected_vid: BASE_VID,
				feedback: "no draft then",
				run_ids: [7],
			},
		});
	});

	it("names the row's tool and keys the job card to that row", () => {
		const opened = continueDialog(draft({}));
		expect(reviseVariables(row(draft({})), opened, "")).toEqual({
			name: "kb",
			currentVid: BASE_VID,
			jobAttributionKey: toolIdentityConsumers("kb", BASE_VID)
				.jobAttributionKey,
			feedback: "",
			runIds: [],
			continuation: draftContinuation(draft({})),
		});
	});
});

describe("reviseStartedMessage", () => {
	it("says which revise was accepted", () => {
		expect(
			reviseStartedMessage("kb", { continuation: null, runIds: [7] }),
		).toBe("已請 AI 修正「kb」，進度顯示在這個工具下方");
		const continuation = draftContinuation(draft({}));
		expect(reviseStartedMessage("kb", { continuation, runIds: [] })).toBe(
			"已接續「kb」的草稿，進度顯示在這個工具下方",
		);
		expect(reviseStartedMessage("kb", { continuation, runIds: [7] })).toBe(
			"已接續「kb」的草稿並附上紀錄，進度顯示在這個工具下方",
		);
	});
});

describe("draftView text", () => {
	it("names the version a published draft became", () => {
		const view = draftView(draft({ state: "published" }), {
			currentVid: "20260801T000000Z-aaaaaa",
		});
		expect(view.title).toBe("草稿已發布");
		expect(view.note).toContain("20260801T000000Z-aaaaaa");
	});

	it("does not invent a version for a published draft when the row has none", () => {
		const view = draftView(draft({ state: "published" }), { currentVid: null });
		expect(view.note).not.toContain("null");
		expect(view.note).not.toContain("undefined");
	});

	// A published draft stays also when it holds more than the round published, so
	// its note sends the operator to fetch what they need before discarding.
	it("does not say discarding is all a published draft needs", () => {
		const view = draftView(draft({ state: "published" }), {
			currentVid: "20260801T000000Z-aaaaaa",
		});
		expect(view.note).not.toContain("放棄它即可");
		expect(view.note).toContain("可能另有沒發布的修改");
		expect(view.note).toContain("先到下方路徑取回");
		expect(view.buttons).toEqual(["discard"]);
	});

	it("names the base version a stale draft was made from", () => {
		const view = draftView(draft({ state: "stale" }));
		expect(view.title).toBe("草稿已過期");
		expect(view.note).toContain(BASE_VID);
	});

	it("does not say which round an open draft's files are from", () => {
		const { note } = draftView(draft({ state: "open", attempts: 4 }));
		expect(note).toContain("接續修正");
		expect(note).not.toMatch(/第 ?\d+ ?輪/);
		expect(note).not.toContain("上一輪");
	});

	it("points an unusable draft's path at the draft directory", () => {
		const view = draftView(unreadable());
		expect(view.path).toBe(DRAFT_DIR);
		expect(view.pathLabel).toBe("草稿目錄");
		expect(draftView(draft({})).pathLabel).toBe("草稿檔案");
	});

	it("says an interrupted round's work is not in the draft, and not which round the files are from", () => {
		for (const attempts of [4, null]) {
			expect(
				draftView(draft({ interrupted: true, attempts })).interruption,
			).toBe("上一輪沒有正常結束，它的進度沒有存進草稿");
		}
		expect(draftView(draft({ interrupted: false })).interruption).toBeNull();
	});

	it("counts rounds and says when the last one ended", () => {
		const view = draftView(draft({ attempts: 2 }), { formatTime: stubTime });
		expect(view.rounds).toBe(
			"已經過 2 輪，上一輪結束於 <2026-10-29T16:04:08.538337+00:00>",
		);
	});

	it("leaves out what it does not know of the rounds", () => {
		expect(draftView(unreadable()).rounds).toBeNull();
		const view = draftView(draft({ attempts: 1, last: null }));
		expect(view.rounds).toBe("已經過 1 輪");
	});

	it("shows a last round recorded as not ended normally, with no end time", () => {
		const formatted = [];
		const view = draftView(
			draft({
				attempts: 3,
				last: lastRound({ outcome: "interrupted", finished_at: null }),
			}),
			{
				formatTime: (value) => {
					formatted.push(value);
					return `<${value}>`;
				},
			},
		);
		expect(view.lastOutcome).toBe("沒有正常結束（沒有留下結束紀錄）");
		expect(view.rounds).toBe("已經過 3 輪");
		expect(view.stopReason).toBeNull();
		expect(formatted).toEqual([]);
	});

	it("states the last round's outcome in the revise card's own words", () => {
		const outcomeOf = (outcome) =>
			draftView(draft({ last: lastRound({ outcome }) })).lastOutcome;
		expect(outcomeOf("not_published")).toBe("AI 未發布新版本");
		expect(outcomeOf("failed")).toBe("修訂失敗");
		expect(outcomeOf(null)).toBeNull();
		expect(outcomeOf("published")).toBeNull();
	});

	it("passes the model's notes through as its words and drops blank ones", () => {
		const view = draftView(
			draft({
				last: lastRound({
					operator_action: "在 config.env 加上 PAGE_SIZE=50",
					summary: "改了 run.py，第二頁還沒測",
				}),
			}),
		);
		expect(view.operatorAction).toBe("在 config.env 加上 PAGE_SIZE=50");
		expect(view.summary).toBe("改了 run.py，第二頁還沒測");
		const blank = draftView(
			draft({ last: lastRound({ operator_action: "  ", summary: "\n" }) }),
		);
		expect(blank.operatorAction).toBeNull();
		expect(blank.summary).toBeNull();
	});

	it("counts the paths the last round did not keep", () => {
		expect(
			draftView(draft({ last: lastRound({ omitted: 3 }) })).omitted,
		).toContain("3 個路徑");
		expect(draftView(draft({ last: lastRound({ omitted: 0 }) })).omitted).toBe(
			null,
		);
	});

	it("shows an unusable draft's last round when its manifest still reads", () => {
		const view = draftView(
			draft({
				state: "unusable",
				path: DRAFT_DIR,
				last: lastRound({ operator_action: "貼上 KEY=value" }),
			}),
		);
		expect(view.operatorAction).toBe("貼上 KEY=value");
		expect(view.buttons).toEqual(["discard"]);
	});
});

describe("draftView paths", () => {
	it("lists the whole draft directory beside the files the draft names", () => {
		const view = draftView(draft({}));
		expect(view.path).toBe(FILES_PATH);
		expect(view.draftDir).toBe(DRAFT_DIR);
	});

	it("does not list the draft directory twice when it is the path", () => {
		const view = draftView(unreadable());
		expect(view.path).toBe(DRAFT_DIR);
		expect(view.draftDir).toBeNull();
	});

	it("lists each kept directory and says what they are", () => {
		const view = draftView(draft({ kept: [KEPT_A, KEPT_B] }));
		expect(view.kept).toEqual([KEPT_A, KEPT_B]);
		expect(view.keptNote).toContain("舊檔案目錄");
		expect(view.keptNote).toContain("取回");
	});

	it("lists no kept directory when there is none, and drops blank ones", () => {
		expect(draftView(draft({ kept: [] })).kept).toEqual([]);
		expect(draftView(draft({ kept: [] })).keptNote).toBeNull();
		expect(draftView(draft({ kept: undefined })).kept).toEqual([]);
		expect(draftView(draft({ kept: [" ", KEPT_A] })).kept).toEqual([KEPT_A]);
	});

	it("says the draft directory may hold directories nobody listed", () => {
		for (const view of [draftView(draft({})), draftView(unreadable())]) {
			expect(view.unlisted).toContain("沒列出的目錄");
			expect(view.unlisted).toContain("整個草稿目錄");
		}
	});
});

describe("draftView stop reason", () => {
	const reasonOf = (fields) =>
		draftView(draft({ last: lastRound(fields) })).stopReason;

	it("is worded by the revise card's own stop-reason vocabulary", () => {
		expect(reasonOf({ stopped_by: "rounds" })).toBe(
			"用完工具回合（TOOL_INSTALL_MAX_ROUNDS）",
		);
		expect(reasonOf({ stopped_by: "rounds", failure: "empty_response" })).toBe(
			"用完工具回合（TOOL_INSTALL_MAX_ROUNDS），收尾時 AI 回了空內容",
		);
		expect(reasonOf({ stopped_by: "conversation" })).toBe(
			"對話大小超過上限（LLM_TOOL_CONVERSATION_BUDGET_TOKENS）",
		);
	});

	it("words every failure the card words, the same way", () => {
		expect(KNOWN_FAILURES.length).toBeGreaterThan(1);
		for (const failure of [...KNOWN_FAILURES, "something_new"]) {
			expect(reasonOf({ failure })).toBe(toolJobStopReason({ failure }));
			expect(reasonOf({ failure })).not.toBeNull();
		}
	});

	it("keeps the two timeouts' settings apart", () => {
		expect(reasonOf({ failure: "deadline" })).toContain(
			"TOOL_INSTALL_TIMEOUT_SECONDS",
		);
		expect(reasonOf({ failure: "request_timeout" })).toContain(
			"OPENAI_TIMEOUT_SECONDS",
		);
		expect(reasonOf({ failure: "request_timeout" })).not.toContain(
			"TOOL_INSTALL_TIMEOUT_SECONDS",
		);
	});

	it("says nothing when no limit and no failure ended the round", () => {
		expect(reasonOf({})).toBeNull();
		expect(draftView(unreadable()).stopReason).toBeNull();
	});
});

describe("buildDraftDiscardRequest", () => {
	it("names a readable draft by its id and generation", () => {
		expect(buildDraftDiscardRequest({ name: "kb", draft: draft({}) })).toEqual({
			path: `/api/tools/kb/draft?draft_id=${DRAFT_ID}&generation=3`,
		});
	});

	it("names a draft whose manifest cannot be read by its token", () => {
		const { path } = buildDraftDiscardRequest({
			name: "kb",
			draft: unreadable(),
		});
		expect(path).toBe(`/api/tools/kb/draft?discard_token=${TOKEN}`);
		expect(path).not.toContain("draft_id");
		expect(path).not.toContain("generation");
	});

	it("names an unusable draft by its token even when its manifest still reads", () => {
		const { path } = buildDraftDiscardRequest({
			name: "kb",
			draft: filesGone(),
		});
		expect(path).toBe(`/api/tools/kb/draft?discard_token=${TOKEN}`);
	});

	it("sends one way of naming the draft, never both", () => {
		for (const state of BACKEND_DRAFT_STATES) {
			const { path } = buildDraftDiscardRequest({
				name: "kb",
				draft: draft({ state }),
			});
			expect(path.includes("discard_token")).toBe(state === "unusable");
			expect(path.includes("draft_id")).toBe(state !== "unusable");
		}
	});

	it("never sends an id it does not have", () => {
		const { path } = buildDraftDiscardRequest({
			name: "kb",
			draft: draft({ draft_id: null, generation: null }),
		});
		expect(path).toBe(`/api/tools/kb/draft?discard_token=${TOKEN}`);
	});
});

describe("draftDiscardFailure", () => {
	it("re-reads the list and explains a draft that is no longer the one confirmed", () => {
		const failure = draftDiscardFailure({ status: 409, code: "draft_changed" });
		expect(failure.refresh).toBe(true);
		expect(failure.notification.color).toBe("orange");
		expect(failure.notification.message).toContain("已經變更");
		expect(failure.notification.message).toContain("重新讀取");
	});

	it("re-reads the list when the draft is already gone", () => {
		const failure = draftDiscardFailure({
			status: 404,
			code: "draft_not_found",
		});
		expect(failure.refresh).toBe(true);
		expect(failure.notification.message).toContain("沒有草稿");
	});

	it("asks to try again later without re-reading when another task holds the slot", () => {
		const failure = draftDiscardFailure({ status: 409, code: "job_busy" });
		expect(failure.refresh).toBe(false);
		expect(failure.notification.message).toContain("稍後再試");
	});

	it("shows the backend's own words for a draft not seen gone, keeps them up and re-reads the list", () => {
		const message =
			"沒能確認草稿已刪除，它可能仍在：/tools/kb/.afterthread.meta/draft。請檢查權限與檔案系統狀態後再放棄一次。";
		const failure = draftDiscardFailure({
			status: 500,
			code: "draft_not_removed",
			message,
		});
		expect(failure).not.toBeNull();
		expect(failure.refresh).toBe(true);
		expect(failure.notification.message).toBe(message);
		expect(failure.notification.autoClose).toBe(false);
	});

	it("still says the draft may be there when a draft not seen gone comes without words", () => {
		const failure = draftDiscardFailure({
			status: 500,
			code: "draft_not_removed",
			message: " ",
		});
		expect(failure).not.toBeNull();
		expect(failure.refresh).toBe(true);
		expect(failure.notification.message).toContain("可能仍在");
		expect(failure.notification.autoClose).toBe(false);
	});

	it("leaves every other error to the ordinary handling", () => {
		expect(draftDiscardFailure({ status: 404, code: null })).toBeNull();
		expect(
			draftDiscardFailure({ status: 500, code: "tools_lock_unavailable" }),
		).toBeNull();
		expect(
			draftDiscardFailure({ status: 409, code: "version_mismatch" }),
		).toBeNull();
		expect(
			draftDiscardFailure({ status: 0, code: "network_error" }),
		).toBeNull();
		expect(draftDiscardFailure(undefined)).toBeNull();
	});

	it("goes by the code and the status, never by the message", () => {
		expect(
			draftDiscardFailure({
				status: 409,
				message: "草稿已經變更，請重新整理後再確認",
			}),
		).toBeNull();
		expect(
			draftDiscardFailure({ status: 400, code: "draft_changed" }),
		).toBeNull();
		expect(
			draftDiscardFailure({ status: 409, code: "draft_not_found" }),
		).toBeNull();
		expect(
			draftDiscardFailure({ status: 409, code: "draft_not_removed" }),
		).toBeNull();
	});
});

describe("draftDiscardNotification", () => {
	it("reports a clean discard plainly", () => {
		expect(draftDiscardNotification("kb", { residual_path: null })).toEqual({
			color: "green",
			message: "已放棄「kb」的草稿",
		});
	});

	it("says where what could not be removed is", () => {
		const residual =
			"/srv/tools/kb/.afterthread.meta/draft.discarded-0123456789abcdef";
		const notice = draftDiscardNotification("kb", { residual_path: residual });
		expect(notice.color).toBe("orange");
		expect(notice.message).toContain(residual);
		expect(notice.autoClose).toBe(false);
	});

	it("treats a blank residual path as none", () => {
		expect(draftDiscardNotification("kb", { residual_path: "  " }).color).toBe(
			"green",
		);
		expect(draftDiscardNotification("kb", undefined).color).toBe("green");
	});
});

describe("draftDiscardConfirmation", () => {
	it("says the whole draft directory goes, kept directories and all, and to fetch first", () => {
		const text = draftDiscardConfirmation(
			"kb",
			draft({ kept: [KEPT_A], draft_dir: DRAFT_DIR }),
		);
		expect(text).toContain("「kb」");
		expect(text).toContain(`整個草稿目錄（${DRAFT_DIR}）都會刪掉`);
		expect(text).toContain("包括保留下來的舊目錄與其他內容");
		expect(text).toContain("無法復原");
		expect(text).toContain("需要的請先取回");
	});

	it("still says the whole directory goes when the draft names none", () => {
		const text = draftDiscardConfirmation("kb", draft({ draft_dir: null }));
		expect(text).toContain("整個草稿目錄都會刪掉");
		expect(text).not.toContain("null");
		expect(draftDiscardConfirmation("kb")).toContain("整個草稿目錄都會刪掉");
	});

	it("says a busy slot or a changed draft deletes nothing", () => {
		const text = draftDiscardConfirmation("kb", draft({}));
		expect(text).toContain("其他工具任務");
		expect(text).toContain("已經變動");
	});
});

// A revise job's `draft` (backend ToolJobDraft) and the job around it.
const jobDraft = (fields) => ({
	saved: true,
	draft_id: DRAFT_ID,
	generation: 4,
	attempts: 3,
	reason: null,
	retained_path: null,
	...fields,
});
const job = (fields) => ({
	kind: "revise",
	state: "failed",
	outcome: "not_published",
	draft: jobDraft({}),
	...fields,
});
// The list's draft that this card's round left: same id, same generation.
const leftByRound = (fields) =>
	draft({ generation: 4, attempts: 3, ...fields });
// The card's word on the list: its row's draft, read after the job ended.
const onTheList = (fields) => ({
	hasRow: true,
	listFresh: true,
	listDraft: leftByRound({}),
	...fields,
});
// What a card offers for a draft that can be continued.
const CARD_BUTTONS = ["continue", "continue_with_note", "publish"];

describe("jobDraftView: the round's own result", () => {
	it("says the progress was kept, with the round it is", () => {
		expect(jobDraftView(job({}))).toMatchObject({
			tone: "saved",
			text: "進度已保存（第 3 輪）",
			path: null,
			note: null,
			retained: null,
		});
	});

	it("still says the progress was kept when the round count is not known", () => {
		expect(
			jobDraftView(job({ draft: jobDraft({ attempts: null }) })).text,
		).toBe("進度已保存");
	});

	it("adds the backend's note to a kept round", () => {
		const reason = `保留了上一份檔案目錄（${KEPT_A}）：那一輪進行中它被改過。`;
		const view = jobDraftView(job({ draft: jobDraft({ reason }) }));
		expect(view.text).toBe("進度已保存（第 3 輪）");
		expect(view.note).toBe(reason);
	});

	it("says why the progress was not kept", () => {
		expect(
			jobDraftView(
				job({
					draft: jobDraft({
						saved: false,
						draft_id: null,
						generation: null,
						attempts: null,
						reason: "無法取得工具目錄的鎖",
					}),
				}),
			),
		).toMatchObject({
			tone: "not_saved",
			text: "未能保存（無法取得工具目錄的鎖）",
			path: null,
			retained: null,
		});
	});

	it("does not leave a parenthesis empty when no reason came with it", () => {
		const view = jobDraftView(
			job({ draft: jobDraft({ saved: false, reason: null }) }),
		);
		expect(view.text).toBe("未能保存");
	});

	it("says where the work is when the draft does not hold it", () => {
		const view = jobDraftView(
			job({
				state: "failed",
				outcome: "failed",
				draft: jobDraft({
					saved: false,
					draft_id: null,
					generation: null,
					attempts: null,
					reason: "無法取得工具目錄的鎖",
					retained_path: RETAINED,
				}),
			}),
		);
		expect(view.tone).toBe("retained");
		expect(view.path).toBe(RETAINED);
		expect(view.text).toBe(
			`成果留在 ${RETAINED}，未能存成草稿（無法取得工具目錄的鎖）`,
		);
	});

	it("says where the work is even without a reason, and not 'not saved' alone", () => {
		const view = jobDraftView(
			job({ draft: jobDraft({ saved: false, retained_path: "/x/build" }) }),
		);
		expect(view.text).toBe("成果留在 /x/build，未能存成草稿");
		expect(view.text).not.toContain("未能保存");
	});

	it("treats a blank retained path as none", () => {
		const view = jobDraftView(
			job({
				draft: jobDraft({
					saved: false,
					reason: "秘密掃描失敗",
					retained_path: " ",
				}),
			}),
		);
		expect(view.tone).toBe("not_saved");
		expect(view.retained).toBeNull();
	});

	it("says a kept round's work was also left elsewhere, never that it was not saved", () => {
		const view = jobDraftView(
			job({ draft: jobDraft({ saved: true, retained_path: RETAINED }) }),
		);
		expect(view.tone).toBe("saved");
		expect(view.text).toBe("進度已保存（第 3 輪）");
		expect(view.retained).toEqual({
			text: `沒能確認草稿已經安全寫入磁碟，所以這一輪的成果另外留了一份在 ${RETAINED}`,
			path: RETAINED,
		});
		expect(JSON.stringify(view)).not.toContain("未能存成草稿");
		expect(JSON.stringify(view)).not.toContain("未能保存");
	});

	it("words the three ways a round's work ends up apart", () => {
		const savedAndLeft = jobDraftView(
			job({
				draft: jobDraft({
					saved: true,
					retained_path: RETAINED,
					reason: "保留了上一份檔案目錄",
				}),
			}),
		);
		const notSavedAndLeft = jobDraftView(
			job({
				draft: jobDraft({
					saved: false,
					retained_path: RETAINED,
					reason: "說明檔寫入失敗",
				}),
			}),
		);
		const published = jobDraftView(
			job({
				state: "succeeded",
				outcome: "published",
				draft: jobDraft({
					saved: false,
					reason: "已發布；這一輪沒存進草稿，請取回草稿裡需要的內容再放棄。",
				}),
			}),
		);
		const say = (view) =>
			[view.text, view.note, view.retained?.text].filter(Boolean).join("|");
		expect(say(savedAndLeft)).toBe(
			`進度已保存（第 3 輪）|保留了上一份檔案目錄|沒能確認草稿已經安全寫入磁碟，所以這一輪的成果另外留了一份在 ${RETAINED}`,
		);
		expect(say(notSavedAndLeft)).toBe(
			`成果留在 ${RETAINED}，未能存成草稿（說明檔寫入失敗）`,
		);
		expect(say(published)).toBe(
			"已發布；這一輪沒存進草稿，請取回草稿裡需要的內容再放棄。",
		);
		expect(published.tone).toBe("note");
		expect(
			new Set([say(savedAndLeft), say(notSavedAndLeft), say(published)]).size,
		).toBe(3);
	});

	it("shows only why the draft is still there for a published revise", () => {
		const reason = "已發布，但未能刪除這一輪的草稿，請在工具列放棄它。";
		expect(
			jobDraftView(
				job({
					state: "succeeded",
					outcome: "published",
					draft: jobDraft({ saved: true, reason }),
				}),
			),
		).toMatchObject({ tone: "note", text: reason, path: null, note: null });
	});

	it("says nothing of progress for a published revise with no note", () => {
		expect(
			jobDraftView(
				job({
					state: "succeeded",
					outcome: "published",
					draft: jobDraft({ saved: true, reason: null }),
				}),
			),
		).toBeNull();
	});

	it("is null when the round had nothing to keep", () => {
		expect(jobDraftView(job({ draft: null }))).toBeNull();
		expect(jobDraftView(job({ draft: undefined }))).toBeNull();
	});

	it("is null while the job is running, and for jobs that make no draft", () => {
		expect(jobDraftView(job({ state: "running" }))).toBeNull();
		expect(jobDraftView(job({ kind: "install" }))).toBeNull();
		expect(jobDraftView(job({ kind: "health_check" }))).toBeNull();
		expect(jobDraftView(undefined, "revise")).toBeNull();
	});

	it("uses the caller's kind until the job names its own", () => {
		const unnamed = { ...job({}), kind: undefined };
		expect(jobDraftView(unnamed, "revise")?.tone).toBe("saved");
		expect(jobDraftView(unnamed, "install")).toBeNull();
	});
});

describe("jobDraftView: the tool's current draft", () => {
	it("offers to continue the draft its round left, once the list read after the end says so", () => {
		const view = jobDraftView(job({}), "revise", onTheList({}));
		expect(view.buttons).toEqual(CARD_BUTTONS);
		expect(view.current).toBeNull();
		// ...whatever became of the round's files.
		const notSaved = jobDraftView(
			job({ draft: jobDraft({ saved: false, reason: "秘密掃描失敗" }) }),
			"revise",
			onTheList({}),
		);
		expect(notSaved.text).toBe("未能保存（秘密掃描失敗）");
		expect(notSaved.buttons).toEqual(CARD_BUTTONS);
	});

	it.each([
		["the list was not read after the job ended", { listFresh: false }],
		["the card has no row", { hasRow: false }],
		[
			"the list's draft is another one",
			{ listDraft: leftByRound({ generation: 5 }) },
		],
		["the tool has no draft now", { listDraft: null }],
	])("keeps the round's own result and path, and offers nothing, when %s", (_when, fields) => {
		for (const saved of [true, false]) {
			const view = jobDraftView(
				job({
					draft: jobDraft({ saved, retained_path: RETAINED }),
				}),
				"revise",
				onTheList(fields),
			);
			const shown = view.retained?.path ?? view.path;
			expect(shown, `saved=${saved}`).toBe(RETAINED);
			expect(view.text, `saved=${saved}`).toBe(
				saved ? "進度已保存（第 3 輪）" : `成果留在 ${RETAINED}，未能存成草稿`,
			);
			expect(view.buttons, `saved=${saved}`).toEqual([]);
		}
	});

	it("keeps the round's own result when no list is given at all", () => {
		const view = jobDraftView(
			job({ draft: jobDraft({ retained_path: RETAINED }) }),
			"revise",
		);
		expect(view.retained.path).toBe(RETAINED);
		expect(view.buttons).toEqual([]);
		expect(view.current).toBeNull();
	});

	it("says the current draft is another one when the generation differs, and offers nothing", () => {
		const view = jobDraftView(
			job({}),
			"revise",
			onTheList({ listDraft: leftByRound({ generation: 5 }) }),
		);
		expect(view.current).toBe(DRAFT_SUPERSEDED);
		expect(view.current).toBe("目前的草稿已不是這張卡片對應的那一份");
		expect(view.buttons).toEqual([]);
	});

	it("claims nothing about the current draft when the tool has none now", () => {
		const view = jobDraftView(
			job({}),
			"revise",
			onTheList({ listDraft: null }),
		);
		expect(view.text).toBe("進度已保存（第 3 輪）");
		expect(view.current).toBeNull();
		expect(view.buttons).toEqual([]);
	});

	it("says the same when another draft took its place", () => {
		const view = jobDraftView(
			job({}),
			"revise",
			onTheList({ listDraft: leftByRound({ draft_id: OTHER_DRAFT_ID }) }),
		);
		expect(view.current).toBe(DRAFT_SUPERSEDED);
		expect(view.buttons).toEqual([]);
	});

	it("claims nothing about the current draft before the list read after the end has landed", () => {
		const view = jobDraftView(
			job({}),
			"revise",
			onTheList({
				listFresh: false,
				listDraft: leftByRound({ generation: 5 }),
			}),
		);
		expect(view.current).toBeNull();
		expect(view.buttons).toEqual([]);
	});

	it("offers nothing and claims nothing on a card no row owns", () => {
		for (const listDraft of [leftByRound({}), leftByRound({ generation: 5 })]) {
			const view = jobDraftView(
				job({}),
				"revise",
				onTheList({ hasRow: false, listDraft }),
			);
			expect(view.buttons).toEqual([]);
			expect(view.current).toBeNull();
		}
	});

	it("claims nothing when either side cannot name its draft", () => {
		const unnamedRound = jobDraftView(
			job({
				draft: jobDraft({
					saved: false,
					draft_id: null,
					generation: null,
					attempts: null,
					reason: "寫入失敗",
				}),
			}),
			"revise",
			onTheList({}),
		);
		expect(unnamedRound.current).toBeNull();
		expect(unnamedRound.buttons).toEqual([]);
		const unreadableNow = jobDraftView(job({}), "revise", {
			...onTheList({}),
			listDraft: unreadable(),
		});
		expect(unreadableNow.current).toBeNull();
		expect(unreadableNow.buttons).toEqual([]);
	});

	it("offers to continue only an open draft", () => {
		for (const state of BACKEND_DRAFT_STATES) {
			const view = jobDraftView(
				job({}),
				"revise",
				onTheList({ listDraft: leftByRound({ state }) }),
			);
			expect(view.buttons, state).toEqual(state === "open" ? CARD_BUTTONS : []);
			expect(view.current, state).toBeNull();
		}
	});

	it("has something to show for a published revise only when the list's draft is another one", () => {
		// After a publish the draft that stays reads as published (or stale): it
		// cannot be continued, so the same draft adds nothing.
		const published = job({
			state: "succeeded",
			outcome: "published",
			draft: jobDraft({ saved: true, reason: null }),
		});
		for (const state of ["published", "stale"]) {
			expect(
				jobDraftView(
					published,
					"revise",
					onTheList({ listDraft: leftByRound({ state }) }),
				),
				state,
			).toBeNull();
		}
		const superseded = jobDraftView(
			published,
			"revise",
			onTheList({ listDraft: leftByRound({ generation: 9 }) }),
		);
		expect(superseded.text).toBeNull();
		expect(superseded.current).toBe(DRAFT_SUPERSEDED);
	});
});

// A publish_draft job's `draft_publish` (backend ToolJobDraftPublish) and the job
// around it. A publish is not a round, so the job's `draft` is always null.
const RETAINED_VERSION = "/srv/tools/kb/versions/20261030T091500Z-a1b2c3";
const publishFacts = (fields) => ({
	draft_id: DRAFT_ID,
	generation: 5,
	removed: true,
	reason: null,
	retained_path: null,
	...fields,
});
const publishJob = (fields) => ({
	kind: "publish_draft",
	state: "succeeded",
	outcome: "published",
	draft: null,
	draft_publish: publishFacts({}),
	...fields,
});
// A publish that went live and left its draft.
const keptPublish = (facts) =>
	publishJob({ draft_publish: publishFacts({ removed: false, ...facts }) });
// A publish that put nothing live.
const failedPublish = (facts) =>
	publishJob({
		state: "failed",
		outcome: "failed",
		draft_publish: publishFacts({ removed: false, ...facts }),
	});
// The list's draft that this card's publish went from: same id, same generation.
const leftByPublish = (fields) => draft({ generation: 5, ...fields });
const onThePublishList = (fields) => ({
	hasRow: true,
	listFresh: true,
	listDraft: leftByPublish({}),
	...fields,
});
// Nothing a publish shows names a round, or says progress was saved: it is not one.
const namesNoRound = (view) => {
	const said = JSON.stringify(view);
	expect(said).not.toMatch(/第 ?\d+ ?輪/);
	expect(said).not.toContain("進度已保存");
	expect(said).not.toContain("未能保存");
	expect(said).not.toContain("未能存成草稿");
};

describe("jobDraftView: the publish of a draft, its own result", () => {
	it("says the draft was removed, and names no round", () => {
		const view = jobDraftView(publishJob({}));
		expect(view.tone).toBe("published");
		expect(view.text).toContain("已刪除");
		expect(view.note).toBeNull();
		expect(view.retained).toBeNull();
		namesNoRound(view);
	});

	it("says the draft stays when it was not removed, with the backend's reason as it came", () => {
		const reason =
			"已發布，但這次發布的內容與草稿不完全相同，因此保留草稿。需要的話先從草稿目錄取出，再到工具列放棄這份草稿。\n沒帶進發布的項目：.env";
		const view = jobDraftView(keptPublish({ reason }));
		expect(view.tone).toBe("kept");
		expect(view.note).toBe(reason);
		expect(view.text).not.toContain("已刪除");
		expect(view.text).not.toBe(jobDraftView(publishJob({})).text);
		namesNoRound(view);
	});

	it("still says the draft stays when the backend gave no reason", () => {
		const view = jobDraftView(keptPublish({ reason: null }));
		expect(view.tone).toBe("kept");
		expect(view.note).toBeNull();
		expect(view.text).not.toContain("已刪除");
	});

	it("says where the files a failed publish left are, to be copied, and claims nothing about the draft without a reason", () => {
		const view = jobDraftView(
			failedPublish({ retained_path: RETAINED_VERSION }),
		);
		expect(view.retained.path).toBe(RETAINED_VERSION);
		expect(view.retained.text).toContain(RETAINED_VERSION);
		expect(view.text).toBeNull();
		expect(view.note).toBeNull();
		namesNoRound(view);
	});

	it("says the draft was not left as it was, with the backend's reason", () => {
		const reason = "無法清除草稿上的發布標記，草稿現在只能放棄。";
		const view = jobDraftView(failedPublish({ reason }));
		expect(view.tone).toBe("failed");
		expect(view.note).toBe(reason);
		expect(view.text).not.toBeNull();
		expect(view.retained).toBeNull();
		namesNoRound(view);
	});

	it("says both when a failed publish has a reason and left files", () => {
		const reason = "無法清除草稿上的發布標記。";
		const view = jobDraftView(
			failedPublish({ reason, retained_path: RETAINED_VERSION }),
		);
		expect(view.note).toBe(reason);
		expect(view.retained.path).toBe(RETAINED_VERSION);
	});

	it("adds nothing for a failed publish that left everything as it was", () => {
		expect(jobDraftView(failedPublish({}))).toBeNull();
	});

	it("needs no round's `draft` to say what became of the draft", () => {
		for (const draftOfARound of [null, undefined]) {
			const view = jobDraftView(publishJob({ draft: draftOfARound }));
			expect(view.tone).toBe("published");
		}
	});

	it("is null when the job carries no report of what it did with the draft", () => {
		for (const state of ["succeeded", "failed"]) {
			const job = publishJob({ state, draft_publish: null });
			expect(jobDraftView(job)).toBeNull();
			expect(
				jobDraftView(job, "publish_draft", onThePublishList({})),
			).toBeNull();
		}
	});

	it("is null while the publish is still running, whatever it reports so far", () => {
		for (const state of ["running", "queued"]) {
			const reporting = publishJob({
				state,
				outcome: null,
				draft_publish: publishFacts({
					removed: false,
					retained_path: RETAINED_VERSION,
				}),
			});
			expect(jobDraftView(reporting), state).toBeNull();
			expect(
				jobDraftView(reporting, "publish_draft", onThePublishList({})),
				state,
			).toBeNull();
		}
	});

	it("takes the job's own kind over the caller's, and the caller's until the job names one", () => {
		expect(jobDraftView(publishJob({}), "revise")?.tone).toBe("published");
		const unnamed = { ...publishJob({}), kind: undefined };
		expect(jobDraftView(unnamed, "publish_draft")?.tone).toBe("published");
		expect(jobDraftView(unnamed, "revise")).toBeNull();
	});
});

describe("jobDraftView: the tool's current draft after a publish", () => {
	it("offers what a draft that can be continued offers, once the list read after the end says it is the same draft", () => {
		const view = jobDraftView(
			failedPublish({}),
			"publish_draft",
			onThePublishList({}),
		);
		expect(view.buttons).toEqual(CARD_BUTTONS);
		expect(view.current).toBeNull();
	});

	it("says the current draft is another one when only the generation differs, and offers nothing", () => {
		const view = jobDraftView(
			failedPublish({}),
			"publish_draft",
			onThePublishList({ listDraft: leftByPublish({ generation: 6 }) }),
		);
		expect(view.current).toBe(DRAFT_SUPERSEDED);
		expect(view.buttons).toEqual([]);
	});

	it("says the same when another draft took its place", () => {
		const view = jobDraftView(
			failedPublish({}),
			"publish_draft",
			onThePublishList({
				listDraft: leftByPublish({ draft_id: OTHER_DRAFT_ID }),
			}),
		);
		expect(view.current).toBe(DRAFT_SUPERSEDED);
		expect(view.buttons).toEqual([]);
	});

	it.each([
		["the list was not read after the job ended", { listFresh: false }],
		["the card has no row", { hasRow: false }],
		["the tool has no draft now", { listDraft: null }],
	])("keeps the publish's own result and path, and claims and offers nothing, when %s", (_when, fields) => {
		const view = jobDraftView(
			failedPublish({
				reason: "草稿的發布標記沒能清掉",
				retained_path: RETAINED_VERSION,
			}),
			"publish_draft",
			onThePublishList({
				...fields,
				listDraft:
					"listDraft" in fields
						? fields.listDraft
						: leftByPublish({ generation: 6 }),
			}),
		);
		expect(view.retained.path).toBe(RETAINED_VERSION);
		expect(view.note).toBe("草稿的發布標記沒能清掉");
		expect(view.current).toBeNull();
		expect(view.buttons).toEqual([]);
	});

	it("claims nothing when the list's draft cannot name itself", () => {
		const view = jobDraftView(
			failedPublish({ retained_path: RETAINED_VERSION }),
			"publish_draft",
			{
				...onThePublishList({}),
				listDraft: unreadable(),
			},
		);
		expect(view.current).toBeNull();
		expect(view.buttons).toEqual([]);
	});

	it("offers only for an open draft", () => {
		for (const state of BACKEND_DRAFT_STATES) {
			const view = jobDraftView(
				failedPublish({ retained_path: RETAINED_VERSION }),
				"publish_draft",
				onThePublishList({ listDraft: leftByPublish({ state }) }),
			);
			expect(view.buttons, state).toEqual(state === "open" ? CARD_BUTTONS : []);
			expect(view.current, state).toBeNull();
		}
	});

	it("says nothing of the current draft for a publish that removed its draft", () => {
		const view = jobDraftView(
			publishJob({}),
			"publish_draft",
			onThePublishList({ listDraft: null }),
		);
		expect(view.tone).toBe("published");
		expect(view.current).toBeNull();
		expect(view.buttons).toEqual([]);
	});
});

// The card's word on the list comes from toolList: the list's own read number
// against the mark the page took when it saw the job end.
describe("jobDraftView on the list as the page reads it", () => {
	function scriptedGet() {
		const calls = [];
		const get = () =>
			new Promise((resolve, reject) => {
				calls.push({ resolve, reject });
			});
		return { get, calls };
	}
	const listWith = (listDraft) => ({
		tools: [{ name: "kb", draft: listDraft }],
	});
	const cardOn = (list, mark) => ({
		hasRow: true,
		listDraft: list.tools[0].draft,
		listFresh: listReadAfter(list, mark),
	});

	it("offers nothing on a list whose read started before the job was seen to end", async () => {
		const { get, calls } = scriptedGet();
		const before = readToolList(get);
		const mark = markListReads();
		calls[0].resolve(listWith(leftByRound({})));
		const stale = await before;
		expect(
			jobDraftView(job({}), "revise", cardOn(stale, mark)).buttons,
		).toEqual([]);
		const after = readToolList(get);
		calls[1].resolve(listWith(leftByRound({})));
		expect(
			jobDraftView(job({}), "revise", cardOn(await after, mark)).buttons,
		).toEqual(CARD_BUTTONS);
	});

	it("offers nothing when the re-read after the end failed", async () => {
		const client = new QueryClient({
			defaultOptions: {
				queries: { retry: false, staleTime: 5_000, networkMode: "always" },
			},
		});
		const { get, calls } = scriptedGet();
		const observer = new QueryObserver(client, {
			queryKey: ["tools"],
			queryFn: () => readToolList(get),
		});
		const unsubscribe = observer.subscribe(() => {});
		calls[0].resolve(listWith(leftByRound({})));
		await vi.waitFor(() =>
			expect(client.getQueryData(["tools"])).toBeDefined(),
		);
		const mark = markListReads();
		const rereading = rereadToolList(client);
		await vi.waitFor(() => expect(calls).toHaveLength(2), { timeout: 500 });
		calls[1].reject(new Error("boom"));
		await rereading;
		const shown = client.getQueryData(["tools"]);
		const view = jobDraftView(job({}), "revise", cardOn(shown, mark));
		expect(view.text).toBe("進度已保存（第 3 輪）");
		expect(view.buttons).toEqual([]);
		unsubscribe();
	});
});
