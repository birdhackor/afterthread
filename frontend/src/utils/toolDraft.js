// Pure helpers for a tool's draft: the unfinished revision a revise leaves behind
// when it does not publish, and that later rounds continue. Kept out of the
// components for the reason every sibling here is: vitest runs in node without
// jsdom, so a pure function is the only testable surface.
//
// Five things meet here, and each has one home:
// * the tools list's `draft` (backend ToolDraft): draftView says what the toolbar
//   shows for it and which buttons it may offer;
// * continuing a draft: draftContinuation says whether a draft can be continued and
//   names it for the request (its id, generation and base version, as the list
//   gave them); continuationNote checks the words sent with a round;
// * what a draft does to the rest of its row: reviseBlockedByDraft says whether a
//   revise that starts over may be sent and, when it may not, why; recordFixView
//   says what a run record's fix button becomes while the tool has a draft;
// * discarding a draft: the request it becomes (buildDraftDiscardRequest), what to
//   ask before (draftDiscardConfirmation), what to tell the operator once it is
//   done (draftDiscardNotification) and how its failures are met
//   (draftDiscardFailure);
// * a revise job's `draft` (backend ToolJobDraft): jobDraftView says what the job
//   card adds about what became of the round's work, and what it may say and offer
//   about the tool's current draft.
//
// Every decision here is taken from the backend's own facts (`state`,
// `interrupted`, `saved`, `retained_path`, ...). What the model wrote
// (`operator_action`, `summary`) is only shown, as its words: nothing else is
// decided from it. How a round stopped is worded by toolJobStop.js, the one
// vocabulary for it.
//
// Nothing here says which round a draft's files are from. A round that could not
// save them is still counted, and the operator may have changed the files by hand,
// so the files can be older than the newest round. The one round a sentence names
// is the round a job card belongs to, when it just saved.

import { SECTION_MAX_LENGTH } from "../constants/sections.js";
import { formatTimestamp } from "./format.js";
import { codePointLength } from "./text.js";
import { isTerminalToolJobState, toolJobOutcomeView } from "./toolInstall.js";
import { toolJobStopReason } from "./toolJobStop.js";

// The buttons a draft can offer, as draftView and jobDraftView name them in
// `buttons`. 接續修正 sends a round at once; 加上說明後接續… asks for the words of
// the round first; 放棄 asks before it deletes, and is offered on the toolbar only.
export const CONTINUE_BUTTON = "continue";
export const CONTINUE_WITH_NOTE_BUTTON = "continue_with_note";
export const DISCARD_BUTTON = "discard";

const CONTINUE_BUTTONS = [CONTINUE_BUTTON, CONTINUE_WITH_NOTE_BUTTON];

// How each state of a draft reads, and what the toolbar may offer for it. `buttons`
// lists every button the toolbar may show: a draft that can be continued
// (`open`) offers the two continue buttons and 放棄; a draft a round works on, or a
// publish is going from, is not taken away from under that job and offers nothing;
// in every other state it can only be discarded, and its files fetched from the
// path.
//
// `note` is the one sentence that tells the operator what the state means; it gets
// the draft and `{ currentVid }`, the version the tool is on now.
const DRAFT_STATE_VIEW = {
	running: {
		title: "草稿正在處理",
		color: "blue",
		buttons: [],
		note: () => "有一輪修正正在處理這份草稿，結束前不能放棄。",
	},
	unusable: {
		title: "草稿無法使用",
		color: "orange",
		buttons: [DISCARD_BUTTON],
		note: () =>
			"這份草稿的說明檔讀不出來、格式不對，或檔案目錄不見了，所以只能放棄。需要的東西請先到下方的草稿目錄取回。",
	},
	publishing: {
		title: "草稿正在發布",
		color: "blue",
		buttons: [],
		note: () => "這份草稿的內容正在發布成新版本，結束前不能放棄。",
	},
	unknown: {
		title: "不確定草稿有沒有發布",
		color: "orange",
		buttons: [DISCARD_BUTTON],
		note: () =>
			"曾經開始從這份草稿發布新版本，但後端沒有留下發布的結果。請先確認目前的版本是不是你要的；需要的檔案請到下方路徑取回。",
	},
	// The draft a publish went from stays when it could not be removed, but also
	// when it was found to differ from the round's saved copy, or that could
	// not be told: it may hold changes no version has, so the note never says
	// discarding it is all that is left to do.
	published: {
		title: "草稿已發布",
		color: "blue",
		buttons: [DISCARD_BUTTON],
		note: (_draft, { currentVid }) =>
			`這一輪已經發布成目前的版本${currentVid ? `（${currentVid}）` : ""}，但草稿可能另有沒發布的修改。需要的內容請先到下方路徑取回，再放棄這份草稿。`,
	},
	stale: {
		title: "草稿已過期",
		color: "orange",
		buttons: [DISCARD_BUTTON],
		note: (draft) =>
			`這份草稿是照舊版本${draft.base_vid ? `（${draft.base_vid}）` : ""}改的，目前版本已經不是那一版。裡面的檔案仍可到下方路徑取回。`,
	},
	open: {
		title: "未完成的修正（草稿）",
		color: "orange",
		buttons: [...CONTINUE_BUTTONS, DISCARD_BUTTON],
		note: () =>
			"這份草稿可以接續修正：AI 會從草稿目前的檔案接著做，你可以先改好 config.env、憑證或草稿裡的檔案再按。也可以到下方路徑取回需要的檔案，再放棄它。",
	},
};

// The states worded above -- the backend's ToolDraft.state as the toolbar knows it.
// Any other value still reads, as itself (see draftView).
export const KNOWN_DRAFT_STATES = Object.freeze(Object.keys(DRAFT_STATE_VIEW));

// What the draft costs by staying, said under every state that offers anything:
// while a draft exists the backend refuses a revise that starts over (409
// draft_pending), so the operator should know that. A draft that can be continued
// is where every fix of the tool goes on from; any other is in the way until it is
// discarded.
const CONTINUES_FROM_DRAFT =
	"這份草稿還在時，這個工具的修正都從它接著做；要從目前的版本重新開始，請先放棄它。";
const BLOCKS_REVISE = "放棄這份草稿之前，這個工具不能送出新的修正。";

// Said under the paths: the toolbar lists the files directory the draft names and
// the old ones it kept, but a draft directory can hold others no manifest names
// (files a round saved before a crash took its manifest back), and discarding
// deletes the directory whole.
const UNLISTED_NOTE =
	"草稿目錄裡可能還有這裡沒列出的目錄，取回時請看整個草稿目錄；放棄會連它們一起刪掉。";

// Said beside the kept directories: a continued round replaced the draft's files
// with a new directory, and the one before could not be confirmed safe to delete.
const KEPT_NOTE =
	"接續時換下來、但沒能確認可以刪掉的舊檔案目錄（例如裡面有沒帶進那一輪的檔案，或那一輪進行中被改過）。需要的內容請先取回；放棄草稿時會一起刪掉。";

// The round a running marker named is over, and nothing recorded how it ended:
// its work was not saved into the draft. Which round the draft's files are from is
// not said (see the header).
const INTERRUPTED_ROUND = "上一輪沒有正常結束，它的進度沒有存進草稿";

// The last recorded round's outcome when it is one such round: recorded when the
// next round started, with no end time.
const INTERRUPTED_OUTCOME = "沒有正常結束（沒有留下結束紀錄）";

function nonBlank(text) {
	return typeof text === "string" && text.trim() !== "" ? text : null;
}

function positiveInt(value) {
	return Number.isInteger(value) && value > 0 ? value : null;
}

function roundsText(draft, last, formatTime) {
	const rounds = positiveInt(draft.attempts);
	const finishedAt = nonBlank(last?.finished_at);
	const parts = [];
	if (rounds !== null) {
		parts.push(`已經過 ${rounds} 輪`);
	}
	if (finishedAt !== null) {
		parts.push(`上一輪結束於 ${formatTime(finishedAt)}`);
	}
	return parts.length === 0 ? null : parts.join("，");
}

// How the last recorded round ended, in the words the job card uses for the same
// outcome. A round saved just before it published has none yet, and nothing is said
// of one that reads "published".
function lastOutcomeText(last) {
	if (last?.outcome === "interrupted") {
		return INTERRUPTED_OUTCOME;
	}
	if (last?.outcome !== "not_published" && last?.outcome !== "failed") {
		return null;
	}
	return toolJobOutcomeView(
		{ state: "failed", outcome: last.outcome },
		"revise",
	).title;
}

function omittedText(last) {
	const omitted = positiveInt(last?.omitted);
	return omitted === null
		? null
		: `上一輪有 ${omitted} 個路徑沒有存進草稿（例如指向工作區外的連結，或命中秘密掃描的檔案）`;
}

function keptPaths(draft) {
	return Array.isArray(draft.kept)
		? draft.kept.filter((path) => nonBlank(path) !== null)
		: [];
}

// What the toolbar shows for a tool's draft, or null when it has none.
//
// * title / note / color: the state, in words (DRAFT_STATE_VIEW); a state it does
//   not know is shown as itself and offers nothing, since an action on a state
//   nobody has worded is not one to offer;
// * buttons: see DRAFT_STATE_VIEW. The continue buttons are left out when the
//   draft does not name itself fully (draftContinuation), since a round could not
//   be sent for it;
// * consequence: what staying costs, said wherever the draft offers anything;
// * interruption / rounds / lastOutcome / stopReason / omitted: facts about the
//   rounds, each null when the backend gave none. The stop reason is the card's
//   (toolJobStopReason) from the two measured fields the draft keeps; the round
//   counts of the session are not kept, so its sentences simply have no counts;
// * operatorAction / summary: the model's words, blank ones dropped. The summary is
//   the backend's excerpt of the last round's.
// * path / pathLabel: where the files are. An unusable draft's path is the draft
//   directory itself, and the label says so;
// * draftDir: the whole draft directory, when it is not already `path`;
// * kept / keptNote: the old files directories a continued round kept, and what
//   they are;
// * unlisted: that the draft directory may hold directories nobody listed.
//
// Both shapes of an unusable draft read here: a manifest that cannot be read has no
// id, no rounds and no last round; one whose files directory is gone still has all
// of those.
//
// `formatTime` renders the last round's timestamp; the caller's zone decides it.
export function draftView(
	draft,
	{ currentVid = null, formatTime = formatTimestamp } = {},
) {
	if (draft == null) {
		return null;
	}
	const known = Object.hasOwn(DRAFT_STATE_VIEW, draft.state);
	const entry = known
		? DRAFT_STATE_VIEW[draft.state]
		: {
				title: `草稿（狀態：${draft.state}）`,
				color: "gray",
				buttons: [],
				note: () =>
					"這個畫面不認得這個草稿狀態，所以不提供操作；路徑仍可複製。",
			};
	const canContinue = draftContinuation(draft) !== null;
	const buttons = entry.buttons.filter(
		(button) => canContinue || !CONTINUE_BUTTONS.includes(button),
	);
	const last = draft.last ?? null;
	const draftDir = nonBlank(draft.draft_dir);
	const kept = keptPaths(draft);
	let consequence = null;
	if (buttons.includes(CONTINUE_BUTTON)) {
		consequence = CONTINUES_FROM_DRAFT;
	} else if (buttons.includes(DISCARD_BUTTON)) {
		consequence = BLOCKS_REVISE;
	}
	return {
		state: draft.state,
		title: entry.title,
		color: entry.color,
		note: entry.note(draft, { currentVid }),
		buttons,
		consequence,
		interruption: draft.interrupted === true ? INTERRUPTED_ROUND : null,
		rounds: roundsText(draft, last, formatTime),
		lastOutcome: lastOutcomeText(last),
		stopReason:
			last === null
				? null
				: toolJobStopReason({
						stopped_by: last.stopped_by,
						failure: last.failure,
					}),
		operatorAction: nonBlank(last?.operator_action),
		summary: nonBlank(last?.summary),
		omitted: omittedText(last),
		pathLabel: draft.state === "unusable" ? "草稿目錄" : "草稿檔案",
		path: draft.path,
		draftDir: draftDir !== null && draftDir !== draft.path ? draftDir : null,
		kept,
		keptNote: kept.length === 0 ? null : KEPT_NOTE,
		unlisted: UNLISTED_NOTE,
	};
}

// --- continuing a draft ------------------------------------------------------

// The draft a round may continue, named the way the request names it, or null
// when this draft cannot be continued: only a draft the backend reads as `open`
// can, and only one whose manifest gave its id, its generation and the version it
// was made from. The request sends exactly these -- the list's, never a job's --
// so a draft written to since the list was read is refused (409 draft_changed)
// rather than continued unseen.
export function draftContinuation(draft) {
	if (draft?.state !== "open") {
		return null;
	}
	const { draft_id: draftId, generation, base_vid: baseVid } = draft;
	if (
		nonBlank(draftId) === null ||
		!Number.isInteger(generation) ||
		generation < 1 ||
		nonBlank(baseVid) === null
	) {
		return null;
	}
	return { draftId, generation, baseVid };
}

// The words sent with a round (加上說明後接續…, or a record's 加上說明…): trimmed,
// and bounded like every other AI input -- the backend's 20,000 characters, counted
// in code points. They may be empty: a round needs no words. `error` is the field's
// message when they are too long, and then there is nothing to send.
export function continuationNote(note) {
	const feedback = (note ?? "").trim();
	if (codePointLength(feedback) > SECTION_MAX_LENGTH) {
		return { feedback: null, error: `不可超過 ${SECTION_MAX_LENGTH} 字` };
	}
	return { feedback, error: null };
}

// --- sending a revise --------------------------------------------------------

// Said beside every control that would send a revise starting over while its tool
// has a draft: why it is off, and what to do instead. It gives the instruction the
// backend's own refusal (draft_pending) and fixRefusalMessage give after a click --
// continue the draft when it can be continued; otherwise take the files from the
// draft's path, then discard it -- in the same terms (the tests pin the words they
// share), so the operator is told the same thing before the click as after it.
// Publishing a draft is not on the page, and no sentence asks for it.
const REVISE_BLOCKED_BY_DRAFT =
	"這個工具有一份未完成的修正（草稿），所以現在不能送出新的修正。要送出，請先到草稿路徑取回需要的檔案，再放棄這份草稿。";
const REVISE_BLOCKED_BY_OPEN_DRAFT =
	"這個工具有一份未完成的修正（草稿），所以不能另外送出新的修正。要接著修，請用草稿提示的「接續修正」；要從目前的版本重新開始，請先到草稿路徑取回需要的檔案，再放棄這份草稿。";

// Why a revise that starts over cannot be sent for this tool, as the sentence to
// show; null when it can. The backend refuses one (409 draft_pending) while the tool
// has a draft in ANY state, so whether it is blocked turns only on whether the list
// row carries a draft: not on its state, and not on whether it has an id -- an
// unusable draft whose manifest cannot be read has none (`draft_id` is null) and
// blocks all the same. The state only chooses the words: a draft that can be
// continued is pointed at 接續修正.
//
// Every control on the 工具 page that sends such a revise asks this and draws the
// answer, and the handlers they feed check it again when pressed; none keeps a
// judgement of its own. The other pages' 請 AI 修正 asks it at the click
// (toolFailures.fixDecision) and words the answer itself: those pages show neither
// the draft's path nor its buttons.
export function reviseBlockedByDraft(draft) {
	if (draft == null) {
		return null;
	}
	return draftContinuation(draft) === null
		? REVISE_BLOCKED_BY_DRAFT
		: REVISE_BLOCKED_BY_OPEN_DRAFT;
}

// What the 工具 page says once a revise it sent was accepted: a round that continued
// the tool's draft (with records attached or not), or a revise that starts over.
// The progress is on the tool's row either way.
export function reviseStartedMessage(
	name,
	{ continuation = null, runIds } = {},
) {
	if (continuation === null) {
		return `已請 AI 修正「${name}」，進度顯示在這個工具下方`;
	}
	return runIds?.length
		? `已接續「${name}」的草稿並附上紀錄，進度顯示在這個工具下方`
		: `已接續「${name}」的草稿，進度顯示在這個工具下方`;
}

// What a run record's fix (the row's newest failure, and each record under 最近執行)
// becomes for this tool's draft: with a draft that can be continued, 接續修正（附上這筆）
// -- the round continues the draft and attaches the record; with none, the fix that
// starts a revise; with a draft in any other state, that same fix, off, with the
// reason (reviseBlockedByDraft). `continuation` names the draft to continue, or null.
// The note dialog (加上說明…) words itself the same way.
export function recordFixView(draft) {
	const continuation = draftContinuation(draft);
	if (continuation !== null) {
		return {
			label: "接續修正（附上這筆）",
			noteTitle: "接續修正，附上這次失敗",
			noteIntro:
				"會接續這個工具的草稿，並把這次失敗的參數與錯誤輸出一起交給 AI。想補充背景（例如這個 API 最近改了什麼）就寫在下面，可以留空。",
			noteSubmit: "接續修正",
			continuation,
			blockedReason: null,
		};
	}
	return {
		label: "請 AI 修正",
		noteTitle: "請 AI 修正這次失敗",
		noteIntro:
			"這次失敗的參數與錯誤輸出會一起交給 AI。想補充背景（例如這個 API 最近改了什麼）就寫在下面，可以留空。",
		noteSubmit: "送出修正",
		continuation: null,
		blockedReason: reviseBlockedByDraft(draft),
	};
}

// --- discarding a draft ------------------------------------------------------

// The DELETE that discards `draft`, named one of two ways and never both (the
// backend answers 422 to both or neither): by its id and generation, so a draft
// made again or written to since the operator looked is refused, or, for an
// unusable draft, by the token the list gave for it. An unusable draft is named by
// token even when its manifest still reads: it is the way that does not depend on
// the manifest.
export function buildDraftDiscardRequest({ name, draft }) {
	const byIdentity =
		draft.state !== "unusable" &&
		draft.draft_id != null &&
		draft.generation != null;
	const query = byIdentity
		? { draft_id: draft.draft_id, generation: draft.generation }
		: { discard_token: draft.discard_token };
	return { path: `/api/tools/${name}/draft?${new URLSearchParams(query)}` };
}

// What 放棄 asks before it deletes: the whole draft directory goes -- the files the
// draft names, the old directories it kept and anything else in it -- so what is
// needed is fetched first. `draft` is the draft the operator pressed 放棄 on; its
// directory is named when the list gave one.
export function draftDiscardConfirmation(name, draft = null) {
	const dir = nonBlank(draft?.draft_dir);
	const where = dir === null ? "" : `（${dir}）`;
	return `確定要放棄「${name}」的草稿嗎？整個草稿目錄${where}都會刪掉，包括保留下來的舊目錄與其他內容，無法復原；需要的請先取回。若有其他工具任務正在進行，或草稿在你確認之前已經變動，系統都不會刪除，你可以稍後重新確認。`;
}

// After a discard that went through. The draft is renamed aside before it is
// removed; whatever could not be removed stays where `residual_path` says, and a
// later install or revise retries it.
export function draftDiscardNotification(name, result) {
	const residual = nonBlank(result?.residual_path);
	if (residual === null) {
		return { color: "green", message: `已放棄「${name}」的草稿` };
	}
	return {
		color: "orange",
		title: "草稿已放棄，但有殘留",
		message: `已放棄「${name}」的草稿，但有一部分沒能刪除，仍留在：${residual}。之後的安裝或修訂結束時，系統會再試著清除；也可以檢查權限與檔案系統狀態後自行刪除。`,
		autoClose: false,
	};
}

// What to do about a discard that was refused, from the structured code alone:
// { refresh, notification }, `refresh` meaning the list is read again (what is on
// screen is not what the server has). Null for every other error, which keeps the
// ordinary handling of a failed tool write.
//
// * draft_changed: the draft was written to, or discarded and made again, since the
//   operator looked; the list is read again and they confirm against what is there;
// * draft_not_found: the tool has no draft any more (discarded elsewhere);
// * job_busy: another task holds the backend's one slot. Nothing is wrong with the
//   draft, so nothing is re-read;
// * draft_not_removed (500): the draft may still be there -- the backend could not
//   see it gone, or could not look at it (or at what names it) and left it alone.
//   Its message names `draft/` and the next step, so that is what is shown, and it
//   stays up until closed. The list is read again: `draft/` may have been renamed
//   aside before the look that failed, and then the row would show a draft that is
//   no longer there.
export function draftDiscardFailure(error) {
	const { status, code } = error ?? {};
	if (status === 409 && code === "draft_changed") {
		return {
			refresh: true,
			notification: {
				color: "orange",
				title: "草稿已經變更",
				message:
					"草稿在你確認之前已經變更（可能在另一個分頁處理過），所以沒有刪除。已重新讀取最新的狀態，請看過再決定要不要放棄。",
			},
		};
	}
	if (status === 404 && code === "draft_not_found") {
		return {
			refresh: true,
			notification: {
				color: "blue",
				title: "沒有可放棄的草稿",
				message:
					"這個工具已經沒有草稿了（可能在另一個分頁放棄過），已重新讀取清單。",
			},
		};
	}
	if (status === 409 && code === "job_busy") {
		return {
			refresh: false,
			notification: {
				color: "orange",
				title: "暫時無法放棄草稿",
				message: "已有工具任務正在進行中，請稍後再試",
			},
		};
	}
	if (status === 500 && code === "draft_not_removed") {
		return {
			refresh: true,
			notification: {
				color: "red",
				title: "沒能確認草稿已刪除",
				message:
					nonBlank(error.message) ??
					"沒能確認草稿已刪除，它可能仍在。請檢查權限與檔案系統狀態後再放棄一次。",
				autoClose: false,
			},
		};
	}
	return null;
}

// --- the job card ------------------------------------------------------------

// Said on a card whose round left a draft that is no longer the tool's draft: the
// list, read after the round ended, holds another one. What happened in between is
// not said -- the list does not tell.
export const DRAFT_SUPERSEDED = "目前的草稿已不是這張卡片對應的那一份";

// The round's own result, from the job alone. Null when there is nothing to say: a
// published revise whose draft was taken away has no `draft` at all, and one with a
// draft and no words has nothing to add.
function roundResult(job, draft) {
	const reason = nonBlank(draft.reason);
	const retainedPath = nonBlank(draft.retained_path);
	if (job.state === "succeeded") {
		// A revise that went live: "saved" is not said of it, and its news about the
		// draft is the backend's reason the draft is still there.
		if (reason === null && retainedPath === null) {
			return null;
		}
		return {
			tone: "note",
			text: reason,
			path: null,
			note: null,
			retained:
				retainedPath === null
					? null
					: {
							text: `這一輪的成果另外留了一份在 ${retainedPath}`,
							path: retainedPath,
						},
		};
	}
	if (draft.saved === true) {
		// The draft holds this round's files. `reason` is a note about the save (an
		// old files directory it kept, and why); `retained_path` is set when the
		// draft's manifest could not be confirmed on disk, so the round's work was
		// also left where the path says.
		const round = positiveInt(draft.attempts);
		return {
			tone: "saved",
			text: round === null ? "進度已保存" : `進度已保存（第 ${round} 輪）`,
			path: null,
			note: reason,
			retained:
				retainedPath === null
					? null
					: {
							text: `沒能確認草稿已經安全寫入磁碟，所以這一輪的成果另外留了一份在 ${retainedPath}`,
							path: retainedPath,
						},
		};
	}
	if (retainedPath !== null) {
		const why = reason === null ? "" : `（${reason}）`;
		return {
			tone: "retained",
			text: `成果留在 ${retainedPath}，未能存成草稿${why}`,
			path: retainedPath,
			note: null,
			retained: null,
		};
	}
	return {
		tone: "not_saved",
		text: reason === null ? "未能保存" : `未能保存（${reason}）`,
		path: null,
		note: null,
		retained: null,
	};
}

const NO_FOLLOW_UP = { current: null, buttons: [] };

// What the card may say about the tool's CURRENT draft, and offer for it. Only when
// all of these hold, because otherwise the list the card would read is not about
// the draft its round left:
// * the card belongs to a row of the list (`hasRow`): a card no row owns offers
//   nothing and claims nothing;
// * the list on screen comes from a read that started after the job was seen to
//   end, and succeeded (`listFresh`, from toolList.listReadAfter);
// * the round left a draft it can name (`draft_id` and `generation`), and the list's
//   draft names itself too.
// Then the two drafts are the same one when both id and generation match, and the
// card offers the continue buttons for it if it can be continued
// (draftContinuation). When they differ, the card says so and offers nothing.
function currentDraftFollowUp(jobDraft, { listDraft, listFresh, hasRow }) {
	if (hasRow !== true || listFresh !== true || listDraft == null) {
		return NO_FOLLOW_UP;
	}
	const draftId = jobDraft.draft_id;
	const generation = jobDraft.generation;
	if (draftId == null || generation == null) {
		return NO_FOLLOW_UP;
	}
	if (listDraft.draft_id == null || listDraft.generation == null) {
		return NO_FOLLOW_UP;
	}
	if (listDraft.draft_id !== draftId || listDraft.generation !== generation) {
		return { current: DRAFT_SUPERSEDED, buttons: [] };
	}
	return {
		current: null,
		buttons: draftContinuation(listDraft) === null ? [] : [...CONTINUE_BUTTONS],
	};
}

// What a finished revise's card adds about its round's work, or null when there is
// nothing to add: not a revise, not finished, or a round that had nothing to keep
// (the job's `draft` is then null).
//
// Two layers, and only the second depends on the list:
// * the round's own result, always: it may be the only place the operator learns
//   where the work is. { tone, text, path, note, retained }:
//   - "saved": the draft holds this round's work, and which round that makes it;
//     `note` is the backend's note about the save, `retained` where the work was
//     also left when the save could not be confirmed on disk;
//   - "retained": the round's work is where the path says and not in the draft;
//   - "not_saved": it is in neither, with the reason when the backend gave one;
//   - "note": a published revise, whose only thing to say is why its draft is still
//     there. "Saved" is not said of a revise that went live.
//   `path` is the path of the sentence itself (copied beside it); `text` is null when
//   only `retained` has something to say.
// * the tool's current draft: { current, buttons } -- see currentDraftFollowUp.
//   `context` is { listDraft, listFresh, hasRow } for the card's row; without it the
//   card says nothing about the current draft and offers nothing.
// `kind` is the caller's job kind, used until the job's own `kind` is known.
export function jobDraftView(job, kind, context = {}) {
	if ((job?.kind ?? kind) !== "revise") {
		return null;
	}
	if (!isTerminalToolJobState(job?.state)) {
		return null;
	}
	const draft = job.draft;
	if (draft == null) {
		return null;
	}
	const own = roundResult(job, draft);
	const followUp = currentDraftFollowUp(draft, {
		listDraft: context.listDraft ?? null,
		listFresh: context.listFresh ?? false,
		hasRow: context.hasRow ?? false,
	});
	if (
		own === null &&
		followUp.current === null &&
		followUp.buttons.length === 0
	) {
		return null;
	}
	return {
		...(own ?? {
			tone: null,
			text: null,
			path: null,
			note: null,
			retained: null,
		}),
		...followUp,
	};
}
