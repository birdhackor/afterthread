// Pure helpers for a tool's draft: the unfinished revision a revise leaves behind
// when it does not publish. Kept out of the components for the reason every sibling
// here is: vitest runs in node without jsdom, so a pure function is the only
// testable surface.
//
// Four things meet here, and each has one home:
// * the tools list's `draft` (backend ToolDraft): draftView says what the toolbar
//   shows for it and which buttons it may offer;
// * what a draft does to the rest of its row: reviseBlockedByDraft says whether a
//   revise may be sent and, when it may not, why;
// * discarding a draft: the request it becomes (buildDraftDiscardRequest), what to
//   tell the operator once it is done (draftDiscardNotification) and how its
//   failures are met (draftDiscardFailure);
// * a revise job's `draft` (backend ToolJobDraft): jobDraftView says what the job
//   card adds about what became of the round's work.
//
// Every decision here is taken from the backend's own facts (`state`,
// `interrupted`, `saved`, `retained_path`, ...). What the model wrote
// (`operator_action`, `summary`) is only shown, as its words: nothing else is
// decided from it. How a round stopped is worded by toolJobStop.js, the one
// vocabulary for it.

import { formatTimestamp } from "./format.js";
import { isTerminalToolJobState, toolJobOutcomeView } from "./toolInstall.js";
import { toolJobStopReason } from "./toolJobStop.js";

// The one button a draft can offer today: the name draftView puts in `buttons`.
export const DISCARD_BUTTON = "discard";

// How each state of a draft reads, and what the toolbar may offer for it. `buttons`
// lists every button the toolbar may show, and today that is at most 放棄: a draft
// a round works on, or a publish is going from, is not taken away from under that
// job; in every other state it can only be discarded, and its files fetched from
// the path.
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
	published: {
		title: "草稿已發布",
		color: "blue",
		buttons: [DISCARD_BUTTON],
		note: (_draft, { currentVid }) =>
			`這份草稿的內容已經發布成目前的版本${currentVid ? `（${currentVid}）` : ""}，只是草稿沒能刪掉，放棄它即可。`,
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
		buttons: [DISCARD_BUTTON],
		note: () =>
			"AI 上一輪沒有發布新版本，成果存成了草稿。需要裡面的檔案，請到下方路徑取回。",
	},
};

// The states worded above -- the backend's ToolDraft.state as the toolbar knows it.
// Any other value still reads, as itself (see draftView).
export const KNOWN_DRAFT_STATES = Object.freeze(Object.keys(DRAFT_STATE_VIEW));

// Said under every state that can be discarded: while a draft exists the backend
// refuses a new revise of the tool, so the operator should know what the draft
// costs them by staying.
const BLOCKS_REVISE = "放棄這份草稿之前，這個工具不能送出新的修正。";

function nonBlank(text) {
	return typeof text === "string" && text.trim() !== "" ? text : null;
}

function positiveInt(value) {
	return Number.isInteger(value) && value > 0 ? value : null;
}

function interruptionText(draft) {
	if (draft.interrupted !== true) {
		return null;
	}
	const rounds = positiveInt(draft.attempts);
	return rounds === null
		? "上一輪被中斷"
		: `上一輪被中斷，草稿是第 ${rounds} 輪結束時的狀態`;
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

// How the last round ended, in the words the job card uses for the same outcome.
// A round saved just before it published has none yet, and nothing is said of one
// that reads "published".
function lastOutcomeText(last) {
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

// What the toolbar shows for a tool's draft, or null when it has none.
//
// * title / note / color: the state, in words (DRAFT_STATE_VIEW); a state it does
//   not know is shown as itself and offers nothing, since an action on a state
//   nobody has worded is not one to offer;
// * buttons: ["discard"] or []; see DRAFT_STATE_VIEW;
// * consequence: what staying costs, said wherever the draft can be discarded;
// * interruption / rounds / lastOutcome / stopReason / omitted: facts about the
//   rounds, each null when the backend gave none. The stop reason is the card's
//   (toolJobStopReason) from the two measured fields the draft keeps; the round
//   counts of the session are not kept, so its sentences simply have no counts;
// * operatorAction / summary: the model's words, blank ones dropped. The summary is
//   the backend's excerpt of the last round's.
// * path / pathLabel: where the files are. An unusable draft's path is the draft
//   directory itself, and the label says so.
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
	const last = draft.last ?? null;
	return {
		state: draft.state,
		title: entry.title,
		color: entry.color,
		note: entry.note(draft, { currentVid }),
		buttons: [...entry.buttons],
		consequence: entry.buttons.includes(DISCARD_BUTTON) ? BLOCKS_REVISE : null,
		interruption: interruptionText(draft),
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
	};
}

// --- sending a revise --------------------------------------------------------

// Said beside every control that would send a revise while its tool has a draft:
// why it is off, and what to do first. It gives the instruction the backend's own
// refusal (draft_pending) and fixRefusalMessage give after a click -- take the
// files from the draft's path, then discard the draft -- in the same terms (the
// tests pin the words they share), so the operator is told the same thing before
// the click as after it. It names only what the page has today.
const REVISE_BLOCKED_BY_DRAFT =
	"這個工具有一份未完成的修正（草稿），所以現在不能送出新的修正。要送出，請先到草稿路徑取回需要的檔案，再放棄這份草稿。";

// Why a revise of this tool cannot be sent, as the sentence to show; null when it
// can. The backend refuses a revise (409 draft_pending) while the tool has a draft
// in ANY state, so the whole question is whether the list row carries one: not
// which state it is in, and not whether it has an id -- an unusable draft whose
// manifest cannot be read has none (`draft_id` is null) and blocks all the same.
//
// Every control that sends a revise asks this and draws the answer, and the
// handlers they feed check it again when pressed; none keeps a judgement of its
// own.
export function reviseBlockedByDraft(draft) {
	return draft == null ? null : REVISE_BLOCKED_BY_DRAFT;
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

export function draftDiscardConfirmation(name) {
	return `確定要放棄「${name}」的草稿嗎？草稿裡的所有檔案都會被刪除，無法復原，需要的檔案請先到草稿路徑取回。若有其他工具任務正在進行，或草稿在你確認之前已經變動，系統都不會刪除，你可以稍後重新確認。`;
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

// What a finished revise's card adds about its round's work, or null when there is
// nothing to add: not a revise, not finished, or a round that had nothing to keep
// (the job's `draft` is then null).
//
// { tone, text, path }:
// * "retained": the save before a publish failed and the publish did too, so the
//   work is where `retained_path` says and nowhere else. Said before anything
//   about `saved`, because the operator has to know where it is;
// * "saved": the draft holds this round's work, and which round that makes it;
// * "not_saved": it does not, with the reason when the backend gave one;
// * "note": a published revise, whose only thing to say is that its draft could not
//   be taken away. "Saved" is not said of a revise that went live.
// `kind` is the caller's job kind, used until the job's own `kind` is known.
export function jobDraftView(job, kind) {
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
	const reason = nonBlank(draft.reason);
	if (job.state === "succeeded") {
		return reason === null ? null : { tone: "note", text: reason, path: null };
	}
	const retained = nonBlank(draft.retained_path);
	if (retained !== null) {
		const why = reason === null ? "" : `（${reason}）`;
		return {
			tone: "retained",
			text: `成果留在 ${retained}，未能存成草稿${why}`,
			path: retained,
		};
	}
	if (draft.saved === true) {
		const round = positiveInt(draft.attempts);
		return {
			tone: "saved",
			text: round === null ? "進度已保存" : `進度已保存（第 ${round} 輪）`,
			path: null,
		};
	}
	return {
		tone: "not_saved",
		text: reason === null ? "未能保存" : `未能保存（${reason}）`,
		path: null,
	};
}
