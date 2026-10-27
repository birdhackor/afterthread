// Pure helpers for the 工具 page's run records (web-v6 §6, §9): the list row's
// one-line hint about a tool's newest run, and the wording of the 最近執行
// section. Kept out of the components for the reason every sibling here is:
// vitest runs in node without jsdom, so a pure function is the only testable
// surface.
//
// The backend already decided everything that matters: which records keep
// bodies, what is masked, and -- above all -- `fixable`, the one input that may
// offer a one-click fix (web-v6 §8.5). Nothing here re-derives it.

import { WORKFLOW_LABELS } from "../constants/labels.js";

// How many records the 最近執行 section asks for (the route allows 1-100).
export const TOOL_RUNS_LIMIT = 20;

export function toolRunsQueryKey(name) {
	return ["tool-runs", name];
}

export function toolRunQueryKey(runId) {
	return ["tool-run", runId];
}

// Outcome -> badge label + color, the backend's vocabulary (services/tools.py
// RUN_*). output_overflow is spelled out: the model was told "exit -9", which
// reads like a crash, while the record knows the tool was stopped for printing
// too much. refused is gray because the tool never ran -- nothing failed.
// error is the backend failing the call (its secret scan, its tools lock), not
// the tool: orange, and never offered as a fix.
const OUTCOME_META = {
	ok: { label: "成功", color: "green" },
	failed: { label: "失敗", color: "red" },
	output_overflow: { label: "輸出超過上限被終止", color: "red" },
	timeout: { label: "逾時", color: "red" },
	start_failed: { label: "無法啟動", color: "red" },
	refused: { label: "被拒，未執行", color: "gray" },
	error: { label: "後端錯誤", color: "orange" },
};

export function runOutcomeMeta(outcome) {
	return OUTCOME_META[outcome] ?? { label: outcome ?? "—", color: "gray" };
}

const FAILED_OUTCOMES = new Set([
	"failed",
	"output_overflow",
	"timeout",
	"start_failed",
]);

// Where the version a run executed stands now (ToolRunSummary.vid_state). A
// discarded version's records stay (web-v6 §6.4), and so can one a hand edit
// removed -- "no longer exists" covers both without guessing which.
const VID_STATE_META = {
	current: { label: "目前版本", color: "blue" },
	older: { label: "舊版本", color: "gray" },
	gone: { label: "版本已不存在", color: "gray" },
};

export function vidStateMeta(state) {
	return VID_STATE_META[state] ?? { label: state ?? "—", color: "gray" };
}

export function runWorkflowLabel(workflow) {
	return WORKFLOW_LABELS[workflow] ?? WORKFLOW_LABELS.unknown;
}

// The list row's hint about the newest run, or null when it has none.
//
// A failure of an OLDER version says so: the tool may well have been fixed
// since, which is exactly why the backend does not mark it fixable. That reading
// needs a current version to compare with -- a package whose pointer is broken
// has none, and then the failure is reported plainly.
export function lastRunHint(lastRun, currentVid) {
	if (lastRun == null) {
		return null;
	}
	if (lastRun.outcome === "ok") {
		return { text: "最近一次執行成功", color: "dimmed" };
	}
	if (lastRun.outcome === "refused") {
		return { text: "最近一次呼叫被拒，工具沒有執行", color: "dimmed" };
	}
	if (lastRun.outcome === "error") {
		return { text: "最近一次呼叫在後端出錯", color: "orange" };
	}
	const label = runOutcomeMeta(lastRun.outcome).label;
	if (
		FAILED_OUTCOMES.has(lastRun.outcome) &&
		currentVid != null &&
		lastRun.vid !== currentVid
	) {
		return {
			text: `最近一次執行${label}（發生在舊版本）`,
			color: "orange",
		};
	}
	return { text: `最近一次執行${label}`, color: "red" };
}

// Whether a record has anything behind its 詳情 toggle: bodies (a failed run
// that started) or the string the model got (every run that did not succeed).
export function runHasDetails(run) {
	return Boolean(run.has_details || run.message);
}

// The detail view's text blocks, in reading order, skipping what the record did
// not keep. stderr is its TAIL and stdout its HEAD (web-v6 §6.2), and the
// labels say so, because a cut that is not named reads as the whole output.
//
// A kept but EMPTY output is a fact ("it printed nothing"), so it stays, with
// `empty` set for the view to say so instead of drawing a blank box.
export function runDetailSections(detail) {
	const candidates = [
		{ key: "message", label: "AI 收到的結果", text: detail.message || null },
		{ key: "args", label: "輸入參數（stdin）", text: detail.args },
		{
			key: "stderr",
			label: "錯誤輸出（stderr，保留結尾）",
			text: detail.stderr,
		},
		{
			key: "stdout",
			label: "標準輸出（stdout，保留開頭）",
			text: detail.stdout,
		},
	];
	return candidates
		.filter((section) => section.text != null)
		.map((section) => ({ ...section, empty: section.text === "" }));
}
