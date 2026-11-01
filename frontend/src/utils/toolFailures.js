// Pure helpers for the tool failures an AI result reports (web-v6 §8.2): the
// reminder shown beside a capture / enrich / assist-update result, and whether
// its 請 AI 修正 may be sent. These pages do not know a tool's current version,
// so the decision is taken against a FRESH tool list read at the click.
// requestFix is the click itself, with that read and the send passed in.

import { reviseBlockedByDraft } from "./toolDraft.js";
import { runOutcomeMeta } from "./toolRuns.js";
import { buildReviseRequest } from "./toolSummary.js";

// A revise attaches at most this many records (backend ToolReviseRequest).
const RUN_IDS_MAX = 3;

// The failures grouped per tool, in the order the tools first failed: one
// reminder per tool, since one revise addresses one tool. `runIds` are the
// RECORDED failures, newest last, at most three; `unrecorded` counts the ones
// whose record could not be written (they can only be linked to).
export function toolFailureGroups(failures) {
	const groups = new Map();
	for (const failure of failures ?? []) {
		const group = groups.get(failure.tool_name) ?? {
			toolName: failure.tool_name,
			vid: failure.vid,
			count: 0,
			runIds: [],
			unrecorded: 0,
			outcomes: [],
		};
		group.count += 1;
		group.vid = failure.vid;
		if (failure.recorded && failure.run_id != null) {
			group.runIds.push(failure.run_id);
		} else {
			group.unrecorded += 1;
		}
		if (!group.outcomes.includes(failure.outcome)) {
			group.outcomes.push(failure.outcome);
		}
		groups.set(failure.tool_name, group);
	}
	return [...groups.values()].map((group) => ({
		...group,
		runIds: group.runIds.slice(-RUN_IDS_MAX),
	}));
}

// The reminder's sentence for one tool.
export function toolFailureText(group) {
	const outcomes = group.outcomes
		.map((outcome) => runOutcomeMeta(outcome).label)
		.join("、");
	const times = group.count > 1 ? ` ${group.count} 次` : "";
	return `這次 AI 處理呼叫的工具「${group.toolName}」執行失敗${times}（${outcomes}），AI 的結果可能因此不完整。`;
}

// What a tool with a draft gets here instead of a fix. A revise that starts over is
// refused while the tool has a draft in any state, and the round that continues
// the draft is the 工具 page's to start: that page shows the draft with what it
// needs from the operator, and from here a round could be paid for before that was
// seen. Said for a draft the list shows (fixDecision) and for one the backend finds
// that the list did not show (draft_pending), with the instruction the 工具 page
// gives for the same block (toolDraft.reviseBlockedByDraft).
function toolHasDraftMessage(toolName) {
	const tool = toolName ? `工具「${toolName}」` : "這個工具";
	return `${tool}有一份未完成的修正（草稿），有草稿時這裡不能請 AI 修正。請到工具頁看這份草稿：可以接續的，用「接續修正」接著修；不能接續的，取回草稿裡需要的檔案，再把草稿放棄。`;
}

// What 請 AI 修正 may do, judged at the click against the tool list as it is
// now: send a revise for the version that failed, or say why not. A tool with a
// draft in any state (toolDraft.reviseBlockedByDraft) is sent nothing, whichever
// version failed; the words are toolHasDraftMessage's.
export function fixDecision(group, tools) {
	if (group.runIds.length === 0) {
		return {
			kind: "unrecorded",
			message: "這次失敗沒有留下執行紀錄，無法直接修正；請到工具頁查看。",
		};
	}
	const tool = (tools ?? []).find(
		(candidate) => candidate.name === group.toolName,
	);
	if (!tool) {
		return { kind: "gone", message: `工具「${group.toolName}」已不存在。` };
	}
	if (reviseBlockedByDraft(tool.draft) !== null) {
		return { kind: "draft", message: toolHasDraftMessage(group.toolName) };
	}
	if (tool.current_vid !== group.vid) {
		return {
			kind: "older",
			message: `工具「${group.toolName}」已更新，這次失敗發生在舊版本；請到工具頁確認新版本是否還有問題。`,
		};
	}
	return { kind: "submit", expectedVid: tool.current_vid };
}

// The click on 請 AI 修正, start to end: read the list (`readTools`, resolving to
// its `tools`), decide (fixDecision), and send the revise (`post(path, body)`)
// only when the decision is to; nothing else is ever sent, and never a draft's
// identity. One re-read on version_mismatch: the list the decision came from may
// predate a revise that just published, and the re-read list is decided on again.
// A second mismatch is reported. Resolves to the reminder's next state: started,
// with the job's id, or refused, with the words to show.
export async function requestFix(group, { readTools, post }) {
	for (let attempt = 0; attempt < 2; attempt += 1) {
		let tools;
		try {
			tools = await readTools();
		} catch (error) {
			return {
				kind: "refused",
				message: fixRefusalMessage(error, group.toolName),
			};
		}
		const decision = fixDecision(group, tools);
		if (decision.kind !== "submit") {
			return { kind: "refused", message: decision.message };
		}
		const request = buildReviseRequest({
			name: group.toolName,
			currentVid: decision.expectedVid,
			runIds: group.runIds,
		});
		try {
			const accepted = await post(request.path, request.body);
			return { kind: "started", jobId: accepted.job_id };
		} catch (error) {
			const again =
				attempt === 0 &&
				error?.status === 409 &&
				error?.code === "version_mismatch";
			if (!again) {
				return {
					kind: "refused",
					message: fixRefusalMessage(error, group.toolName),
				};
			}
		}
	}
}

// What the reminder shows under its sentence, for where its fix stands (the
// state requestFix resolves to, or idle / checking before it does): 請 AI 修正
// while any of the failures was recorded and no fix has started, what was said,
// and always the way to the 工具 page -- to the started job's progress, or else to
// the page itself, which shows a draft or a newer version that kept a fix from
// being sent here, with what can be done about it.
export function failureNoticeView(group, state) {
	if (state?.kind === "started") {
		return {
			started: `已請 AI 修正「${group.toolName}」。`,
			fix: null,
			link: { label: "到工具頁看進度", search: { job: state.jobId } },
			refusal: null,
		};
	}
	return {
		started: null,
		fix:
			group.runIds.length > 0 ? { checking: state?.kind === "checking" } : null,
		link: { label: "到工具頁查看", search: {} },
		refusal: state?.kind === "refused" ? state.message : null,
	};
}

// A refused submit, in words. version_mismatch reaches here only after the one
// re-read the caller already spent on it. A 404 without a code is the revise
// route's own "Tool not found" -- the tool was deleted after the fresh list
// read -- which the API client would otherwise word as an item's 「找不到項目」.
export function fixRefusalMessage(error, toolName) {
	if (error?.status === 409 && error?.code === "job_busy") {
		return "目前有其他 AI 工作進行中，請稍後再試。";
	}
	if (error?.status === 409 && error?.code === "env_policy") {
		return error.message;
	}
	if (error?.status === 409 && error?.code === "version_mismatch") {
		return "工具版本剛剛又變了，請到工具頁確認後再試。";
	}
	// A draft the list did not show -- one made after it was read, say. The
	// backend's own sentence points at "the tool's row", which only the 工具 page
	// has; this page says it as it says a draft the list shows.
	if (error?.status === 409 && error?.code === "draft_pending") {
		return toolHasDraftMessage(toolName);
	}
	if (error?.status === 404 && error?.code === "run_not_found") {
		return "這次失敗的執行紀錄已被清除，請到工具頁查看。";
	}
	if (error?.status === 404 && error?.code == null && toolName) {
		return `工具「${toolName}」已不存在。`;
	}
	return error?.message ?? "無法送出修正，請稍後再試。";
}

// Whether a /tools?job= value names a job: the backend's ids are uuid4 hex.
export function toolsJobSearch(search) {
	const raw = search?.job;
	return typeof raw === "string" && /^[0-9a-f]{32}$/.test(raw)
		? { job: raw }
		: {};
}
