// Pure helpers for running a tool's examples.json (web-v6 §10): the verification
// line of 最近執行 (§9), and the verification block of a job card -- a 跑一次範例
// health check, or the check that follows a published fix (§8.3). The backend
// runs the examples and judges them; these only word what it reports.

import { formatTimestamp } from "./format.js";
import { runOutcomeMeta } from "./toolRuns.js";

// A revise attaches at most this many records (backend ToolReviseRequest).
const RUN_IDS_MAX = 3;

// The 最近執行 line for ToolRunList.verification, or null when there is nothing
// to say (no current version).
export function verificationStateText(state) {
	switch (state?.state) {
		case "examples_invalid":
			return {
				text: `examples.json 無法使用：${state.message}`,
				color: "orange",
			};
		case "no_examples":
			return { text: "這個版本沒有範例（examples.json）", color: "dimmed" };
		case "never":
			return { text: `有 ${state.total} 個範例，尚未驗證`, color: "dimmed" };
		case "passed":
			return {
				text: `驗證通過（${state.passed}/${state.total}，${formatTimestamp(state.checked_at)}）`,
				color: "green",
			};
		case "failed":
			return {
				text: `驗證失敗（${state.passed}/${state.total} 通過，${formatTimestamp(state.checked_at)}）`,
				color: "red",
			};
		case "incomplete":
			return { text: incompleteText(state), color: "orange" };
		default:
			return null;
	}
}

// Why the newest check of the current version did not cover every example
// (ToolVerificationState.reason): the budget, the tool switched off or replaced
// mid-run, or an examples.json that changed since.
function incompleteText(state) {
	const counted = `跑了 ${state.ran}/${state.total} 個`;
	switch (state.reason) {
		case "budget":
			return `驗證未完成：預算用完（${counted}）`;
		case "interrupted":
			return `驗證未完成：工具在驗證途中被停用或更換（${counted}）`;
		case "examples_changed":
			return "範例在上次驗證後有變動，請再跑一次範例";
		default:
			return `驗證未完成（${counted}）`;
	}
}

// Whether 跑一次範例 has anything to run. The backend refuses otherwise anyway
// (check_unavailable); this keeps the button from offering a sure refusal.
export function canRunExamples(state) {
	return ["never", "passed", "failed", "incomplete"].includes(state?.state);
}

// One example's badge: a run outcome, or how it was never started.
export function exampleOutcomeMeta(outcome) {
	if (outcome === "skipped") {
		return { label: "略過（前一個逾時）", color: "gray" };
	}
	if (outcome === "not_run") {
		return { label: "未執行（預算用完）", color: "gray" };
	}
	if (outcome === "stopped") {
		return { label: "未執行（工具被停用或更換）", color: "gray" };
	}
	return runOutcomeMeta(outcome);
}

const FAILED = new Set([
	"failed",
	"output_overflow",
	"timeout",
	"start_failed",
]);

// A job's verification as a card shows it: `tone` (running | passed | failed |
// incomplete | not_run), a title, the per-example rows, and the failed run ids a
// fix may attach (at most three, in order).
export function verificationView(verification) {
	if (!verification) {
		return null;
	}
	const rows = (verification.runs ?? []).map((run, index) => ({
		key: `${index}-${run.name}`,
		name: run.name,
		...exampleOutcomeMeta(run.outcome),
	}));
	const failedRunIds = (verification.runs ?? [])
		.filter((run) => FAILED.has(run.outcome) && run.run_id != null)
		.map((run) => run.run_id)
		.slice(0, RUN_IDS_MAX);
	if (!verification.finished) {
		return {
			tone: "running",
			title: `正在跑範例（${verification.completed}/${verification.total}）`,
			rows,
			failedRunIds: [],
		};
	}
	if (verification.status !== "ran") {
		return {
			tone: "not_run",
			title: NOT_RUN_TITLES[verification.status] ?? "沒有跑範例",
			note: verification.message ?? null,
			rows: [],
			failedRunIds: [],
		};
	}
	const runs = verification.runs ?? [];
	const passed = runs.filter((run) => run.outcome === "ok").length;
	const failed = runs.filter((run) => FAILED.has(run.outcome)).length;
	const total = verification.total;
	// Only the tool's own failures fail a check. A refusal (the tool switched
	// off or replaced mid-run) or the backend failing a call leaves an example
	// unverified, not failed: there is nothing for a fix to attach (P5 review
	// r1, J3), the same rule as the backend's verdict.
	if (failed > 0) {
		return {
			tone: "failed",
			title: `範例驗證失敗（${passed}/${total} 通過）`,
			rows,
			failedRunIds,
		};
	}
	if (passed < total) {
		return {
			tone: "incomplete",
			title: incompleteText({
				ran: passed,
				total,
				reason: verification.budget_exhausted
					? "budget"
					: runs.some(
								(run) => run.outcome === "stopped" || run.outcome === "refused",
							)
						? "interrupted"
						: null,
			}),
			rows,
			failedRunIds,
		};
	}
	return {
		tone: "passed",
		title: `範例全部通過（${passed}/${total}）`,
		rows,
		failedRunIds: [],
	};
}

const NOT_RUN_TITLES = {
	disabled: "工具已停用，未驗證",
	no_examples: "這個版本沒有範例，未驗證",
	examples_invalid: "examples.json 無法使用，未驗證",
	version_changed: "工具版本已變更，未驗證",
	invalid: "工具目前的版本無法執行，未驗證",
	not_found: "找不到工具，未驗證",
	error: "驗證無法執行",
};
