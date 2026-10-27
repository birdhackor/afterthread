// Pure helpers for the tool failures an AI result reports (web-v6 §8.2): the
// reminder shown beside a capture / enrich / assist-update result, and whether
// its 請 AI 修正 may be sent. These pages do not know a tool's current version,
// so the decision is taken against a FRESH tool list read at the click.

import { runOutcomeMeta } from "./toolRuns.js";

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

// What 請 AI 修正 may do, judged at the click against the tool list as it is
// now: send a revise for the version that failed, or say why not.
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
	if (tool.current_vid !== group.vid) {
		return {
			kind: "older",
			message: `工具「${group.toolName}」已更新，這次失敗發生在舊版本；請到工具頁確認新版本是否還有問題。`,
		};
	}
	return { kind: "submit", expectedVid: tool.current_vid };
}

// A refused submit, in words. version_mismatch reaches here only after the one
// re-read the caller already spent on it.
export function fixRefusalMessage(error) {
	if (error?.status === 409 && error?.code === "job_busy") {
		return "目前有其他 AI 工作進行中，請稍後再試。";
	}
	if (error?.status === 409 && error?.code === "env_policy") {
		return error.message;
	}
	if (error?.status === 409 && error?.code === "version_mismatch") {
		return "工具版本剛剛又變了，請到工具頁確認後再試。";
	}
	if (error?.status === 404 && error?.code === "run_not_found") {
		return "這次失敗的執行紀錄已被清除，請到工具頁查看。";
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
