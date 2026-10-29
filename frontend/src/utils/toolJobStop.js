// Pure helpers for what a finished AI revise's card says about how the session
// ended and what is left for the operator. Kept out of the component so every
// branch is unit-testable without jsdom.
//
// Two kinds of fact meet on the card, and they are kept apart on purpose:
// * what the BACKEND measured about the builder session (`stopped_by`, `failure`,
//   `rounds_used`, `max_rounds` on the job, from backend llm.StructuredRunStats):
//   which budget ended it, if any, and how it failed. toolJobStopReason turns
//   these into one zh-TW sentence, naming the setting behind a budget or a
//   timeout;
// * what the MODEL wrote (`operator_action`, `handoff`): shown as its words, never
//   read to decide anything. `operator_action` can come with a published revise
//   too: a fix verified with a new value can still need that value pasted into
//   the package's config.env by hand.
//
// Settings are named, never their bounds: the bounds belong to the backend's
// settings validation, and a copy here would go stale without a sound.

import { isTerminalToolJobState } from "./toolInstall.js";

// How each measured failure reads. The two timeouts name different settings: a
// `request_timeout` is one request waiting too long for its response
// (OPENAI_TIMEOUT_SECONDS, the LLM client's per-request timeout), while
// `deadline` is the whole session's clock (TOOL_INSTALL_TIMEOUT_SECONDS).
// Naming either one for the other would send the operator to the wrong knob.
const FAILURE_TEXT = {
	empty_response: "AI 回了空內容",
	invalid_output: "AI 回覆的格式不正確，更正一次後仍然不對",
	upstream: "LLM 服務回應錯誤",
	request_timeout:
		"單一 LLM 請求等候回應逾時（OPENAI_TIMEOUT_SECONDS），整場的時間還沒用完",
	deadline: "整場的時間用完（TOOL_INSTALL_TIMEOUT_SECONDS）",
	not_configured: "LLM 尚未設定",
};

// The failure values worded above -- the backend's RunFailure as this card knows
// it. Any other value still reads, as itself (see toolJobStopReason).
export const KNOWN_FAILURES = Object.freeze(Object.keys(FAILURE_TEXT));

function countOrNull(value) {
	return Number.isInteger(value) ? value : null;
}

function roundBudgetText(used, max) {
	const spent = max ?? used;
	return spent === null
		? "用完工具回合（TOOL_INSTALL_MAX_ROUNDS）"
		: `用完 ${spent} 次工具回合（TOOL_INSTALL_MAX_ROUNDS）`;
}

// One sentence for why a builder session stopped, from the four measured fields,
// or null when the job carries none of them (an install, a job from an older
// backend, a revise refused before its session started).
//
// * a budget that ended the tool phase (`stopped_by`) is named with its setting;
// * a failure after that budget is told as a second fact ("收尾時…"): the round
//   budget forced the final answer AND that answer failed are two things, and the
//   operator acts on each differently;
// * a failure with no budget involved says how far the session had got;
// * neither: the session ended on the AI's own answer, cut off by no limit.
export function toolJobStopReason(job) {
	const stoppedBy = job?.stopped_by ?? null;
	const failure = job?.failure ?? null;
	const used = countOrNull(job?.rounds_used);
	const max = countOrNull(job?.max_rounds);

	let limit = null;
	if (stoppedBy === "rounds") {
		limit = roundBudgetText(used, max);
	} else if (stoppedBy === "conversation") {
		limit = "對話大小超過上限（LLM_TOOL_CONVERSATION_BUDGET_TOKENS）";
	}
	const failed =
		failure === null ? null : (FAILURE_TEXT[failure] ?? `失敗（${failure}）`);
	const progress =
		used !== null && max !== null ? `${used}／${max} 次工具回合` : null;

	if (limit !== null && failed !== null) {
		// A space before a Latin word ("收尾時 AI …"), none before Chinese.
		const lead = /^[A-Za-z]/.test(failed) ? "收尾時 " : "收尾時";
		return `${limit}，${lead}${failed}`;
	}
	if (limit !== null) {
		return limit;
	}
	if (failed !== null) {
		if (failure === "not_configured" || progress === null) {
			return failed;
		}
		return `${failed}，當時已用 ${progress}`;
	}
	if (progress !== null) {
		return `AI 自行結束，沒有被上限截斷（用了 ${progress}）`;
	}
	return null;
}

function nonBlank(text) {
	return typeof text === "string" && text.trim() !== "" ? text : null;
}

// What the job card adds under a finished revise: the stop reason, the operator's
// to-do, and the model's handoff note. Null while the job is not finished, when it
// is not a revise, or when there is none of the three.
// * Not published (the builder declined, or the job failed): all three, the stop
//   reason included whenever the job carries one.
// * Published: the two notes when present; a stop reason only when a budget ended
//   the tool phase (`stopped_by`), because then the published answer is the one
//   that budget forced. Never "AI 自行結束…": a published fix did not stop short.
// `kind` is the caller's job kind, used until the job's own `kind` is known.
export function reviseStopView(job, kind) {
	if ((job?.kind ?? kind) !== "revise") {
		return null;
	}
	if (!isTerminalToolJobState(job?.state)) {
		return null;
	}
	const published = job.state === "succeeded";
	const forcedByBudget = (job.stopped_by ?? null) !== null;
	const reason = published && !forcedByBudget ? null : toolJobStopReason(job);
	const operatorAction = nonBlank(job.operator_action);
	const handoff = nonBlank(job.handoff);
	if (reason === null && operatorAction === null && handoff === null) {
		return null;
	}
	return { reason, operatorAction, handoff };
}
