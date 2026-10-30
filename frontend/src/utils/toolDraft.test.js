import { describe, expect, it } from "vitest";
import {
	buildDraftDiscardRequest,
	draftDiscardConfirmation,
	draftDiscardFailure,
	draftDiscardNotification,
	draftView,
	jobDraftView,
	KNOWN_DRAFT_STATES,
	reviseBlockedByDraft,
} from "./toolDraft.js";
import { fixRefusalMessage } from "./toolFailures.js";
import { KNOWN_FAILURES, toolJobStopReason } from "./toolJobStop.js";

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
const TOKEN = "f".repeat(64);
const BASE_VID = "20260728T010203Z-abcdef";
const FILES_PATH = "/srv/tools/kb/.afterthread.meta/draft/files-0a1b2c";
const DRAFT_DIR = "/srv/tools/kb/.afterthread.meta/draft";

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
		open: ["discard"],
		stale: ["discard"],
		unusable: ["discard"],
		published: ["discard"],
		unknown: ["discard"],
		running: [],
		publishing: [],
	};

	it.each(
		BACKEND_DRAFT_STATES,
	)("offers only what %s allows: 放棄 or nothing", (state) => {
		expect(draftView(draft({ state })).buttons).toEqual(OFFERED[state]);
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

	it("is null for a tool without a draft", () => {
		expect(draftView(null)).toBeNull();
		expect(draftView(undefined)).toBeNull();
	});
});

// The backend refuses a revise of a tool that has a draft, whatever the draft's
// state (409 draft_pending). This says so before the click, and every control that
// would send a revise draws its answer.
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

	it("says what to do now, and names no button the page does not have yet", () => {
		const reason = reviseBlockedByDraft(draft({}));
		expect(reason).toContain("路徑");
		expect(reason).toContain("放棄");
		// Continuing a draft and publishing one are not on the page; 加上說明… is, on
		// a record, and is a different button.
		for (const missing of ["接續", "發布草稿"]) {
			expect(reason, missing).not.toContain(missing);
		}
	});

	it("uses the words the draft_pending refusal uses", () => {
		const refusal = fixRefusalMessage(
			{ status: 409, code: "draft_pending" },
			"kb",
		);
		const reason = reviseBlockedByDraft(draft({}));
		for (const phrase of ["未完成的修正（草稿）", "取回", "放棄"]) {
			expect(refusal, phrase).toContain(phrase);
			expect(reason, phrase).toContain(phrase);
		}
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

	it("names the base version a stale draft was made from", () => {
		const view = draftView(draft({ state: "stale" }));
		expect(view.title).toBe("草稿已過期");
		expect(view.note).toContain(BASE_VID);
	});

	it("points an unusable draft's path at the draft directory", () => {
		const view = draftView(unreadable());
		expect(view.path).toBe(DRAFT_DIR);
		expect(view.pathLabel).toBe("草稿目錄");
		expect(draftView(draft({})).pathLabel).toBe("草稿檔案");
	});

	it("says what an interrupted round left, with the round it ended on", () => {
		expect(
			draftView(draft({ interrupted: true, attempts: 4 })).interruption,
		).toBe("上一輪被中斷，草稿是第 4 輪結束時的狀態");
		expect(draftView(draft({ interrupted: false })).interruption).toBeNull();
	});

	it("still says a round was interrupted when the round count is not known", () => {
		expect(
			draftView(draft({ interrupted: true, attempts: null })).interruption,
		).toBe("上一輪被中斷");
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
	it("names the tool and says the files are deleted for good", () => {
		const text = draftDiscardConfirmation("kb");
		expect(text).toContain("「kb」");
		expect(text).toContain("無法復原");
		expect(text).toContain("草稿路徑");
	});

	it("says a busy slot or a changed draft deletes nothing", () => {
		const text = draftDiscardConfirmation("kb");
		expect(text).toContain("其他工具任務");
		expect(text).toContain("已經變動");
	});
});

describe("jobDraftView", () => {
	const jobDraft = (fields) => ({
		saved: true,
		draft_id: DRAFT_ID,
		generation: 1,
		attempts: 1,
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

	it("says the progress was kept, with the round it is", () => {
		expect(jobDraftView(job({ draft: jobDraft({ attempts: 3 }) }))).toEqual({
			tone: "saved",
			text: "進度已保存（第 3 輪）",
			path: null,
		});
	});

	it("still says the progress was kept when the round count is not known", () => {
		expect(
			jobDraftView(job({ draft: jobDraft({ attempts: null }) })).text,
		).toBe("進度已保存");
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
		).toEqual({
			tone: "not_saved",
			text: "未能保存（無法取得工具目錄的鎖）",
			path: null,
		});
	});

	it("does not leave a parenthesis empty when no reason came with it", () => {
		const view = jobDraftView(
			job({ draft: jobDraft({ saved: false, reason: null }) }),
		);
		expect(view.text).toBe("未能保存");
	});

	it("says where the work is when neither a draft nor a version holds it", () => {
		const retained = "/srv/tools/.staging/0123456789abcdef/build";
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
					retained_path: retained,
				}),
			}),
		);
		expect(view.tone).toBe("retained");
		expect(view.path).toBe(retained);
		expect(view.text).toBe(
			`成果留在 ${retained}，未能存成草稿（無法取得工具目錄的鎖）`,
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
	});

	it("shows only what could not be taken away for a published revise", () => {
		const reason = "已發布，但未能刪除這一輪的草稿，請在工具列放棄它。";
		expect(
			jobDraftView(
				job({
					state: "succeeded",
					outcome: "published",
					draft: jobDraft({ saved: true, reason }),
				}),
			),
		).toEqual({ tone: "note", text: reason, path: null });
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
