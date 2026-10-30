// Pure helpers for publishing a tool's draft as it is: no round of the AI, only the
// draft's files made into a new version. Kept out of the components for the reason
// every sibling here is: vitest runs in node without jsdom, so a pure function is
// the only testable surface.
//
// What the toolbar and the job cards show for it (the button, the card's words, the
// claims about the tool's current draft) is toolDraft.js's, and the request it
// becomes is toolSummary.buildDraftPublishRequest. What lies between a click and
// that request is here:
// * what the confirmation records when it opens (draftPublishTarget), and what is
//   sent for it (draftPublishVariables);
// * what the confirmation says (draftPublishConfirmation);
// * what the page tells the operator once the publish was accepted
//   (draftPublishStartedMessage).

import { toolIdentityConsumers } from "./toolSummary.js";

// What 發布草稿 records when its confirmation opens, from the draft the operator was
// looking at (`continuation`, toolDraft.draftContinuation's answer for the list's
// draft): the tool, the draft's id and generation, and the version it was made from.
// Null for a draft that cannot be published, so nothing is recorded for it.
//
// The confirmation keeps this while it is open and is sent with it. By the time it
// is confirmed the row may show another draft, or none: what is sent is still what
// it opened on. A draft that changed is the backend's to refuse -- draft_gone or
// draft_changed -- and the page reads the list again on those codes
// (versionWriteConflictReaction).
export function draftPublishTarget(name, continuation) {
	if (continuation == null) {
		return null;
	}
	return {
		name,
		draftId: continuation.draftId,
		generation: continuation.generation,
		baseVid: continuation.baseVid,
	};
}

// The publishDraftMutation's variables for the `target` a confirmation recorded,
// sent from `tool`, the row as the page has it when the publish is sent. The
// request is made of the target alone (buildDraftPublishRequest); the row only says
// which instance of the tool the job card belongs to, so the card stays with the
// version the publish was sent from even if the same name points elsewhere by then.
export function draftPublishVariables(tool, target) {
	return {
		...target,
		jobAttributionKey: toolIdentityConsumers(tool.name, tool.current_vid)
			.jobAttributionKey,
	};
}

// What the confirmation of 發布草稿 says before the publish is sent: a lead
// sentence and the points the operator should know -- what the publish does and
// does not do, when it refuses, and when the draft stays after it.
export function draftPublishConfirmation(name) {
	return {
		title: "發布草稿",
		lead: `確定要發布「${name}」的草稿嗎？`,
		points: [
			"不經 AI 修改，也不產生 AI 總結：直接把草稿目前的檔案發布成這個工具的新版本，接著照常跑範例。範例沒過也不會撤銷發布。",
			"新版本一開始沒有 AI 總結（會顯示「尚無總結」）；需要的話，之後到「AI 總結」按「重新產生」。",
			"除了根目錄的 .env 與 .afterthread.meta（它們不會進新版本）之外，草稿裡有項目含有已知的秘密值（檔名、內容或連結目標），或有項目無法查看、檢查沒能完成時，不會發布，草稿也不會被動到；原因會顯示在發布的進度卡片上。",
			"發布成功後，只有確定草稿裡的東西都已經進了新版本，草稿才會刪除。有東西沒進新版本（例如根目錄的 .env、指到工具外面的連結）、草稿目錄裡另有別的內容、草稿的檔案在發布途中被改過，或無法確認時，草稿會留著，原因會顯示在進度卡片上；需要的內容請先取回，再放棄草稿。",
		],
	};
}

// What the 工具 page says once a publish it sent was accepted. The progress is on
// the tool's row.
export function draftPublishStartedMessage(name) {
	return `已開始發布「${name}」的草稿，進度顯示在這個工具下方`;
}
