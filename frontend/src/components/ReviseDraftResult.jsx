import { Group, Stack, Text } from "@mantine/core";
import {
	CONTINUE_BUTTON,
	jobDraftView,
	PUBLISH_BUTTON,
} from "../utils/toolDraft.js";
import { CopyPathButton } from "./CopyPathButton.jsx";
import { DraftContinueButtons } from "./DraftContinueButtons.jsx";
import { DraftPublishButton } from "./DraftPublishButton.jsx";

// Under a finished job that reports on a draft -- an AI revise, published or not, or
// the publish of a draft as it is: what became of the job's work. For a revise, that
// is the round's: kept as the tool's draft (and which round that makes it, with the
// backend's note about the save and where the work was also left when the save could
// not be confirmed), not kept (and why), left in a directory and not in the draft, or,
// for a revise that went live, why its draft is still there. For a publish, that is
// its draft's: removed, or kept (and why), or, when nothing went live, where the
// files it tried to publish were left and whether the draft was left as it was. Then,
// when the page says the list was read after the job ended and the card has a row,
// what the card may say about the tool's current draft and the buttons it may offer
// from here. Renders nothing when there is nothing to say. Kept apart from
// ReviseStopNotice, which is about how a session stopped: this is about where the
// work went. What is said, and when, is decided by jobDraftView.
//
// `followUp` comes from the page: { context, blocked, submitting, publishing,
// toolName, continuation, onContinue, onSendNoted, onPublish } -- `context` is
// jobDraftView's view of the card's row, `continuation` the row's draft a round or a
// publish from here goes from, `toolName` the row's tool, and the rest go to
// DraftContinueButtons and DraftPublishButton; null for a card that offers nothing
// about the current draft (an install, a card no row owns).
export function ReviseDraftResult({ job, kind, followUp = null }) {
	const view = jobDraftView(job, kind, followUp?.context);
	if (view === null) {
		return null;
	}
	const offersContinue =
		followUp !== null && view.buttons.includes(CONTINUE_BUTTON);
	const offersPublish =
		followUp !== null && view.buttons.includes(PUBLISH_BUTTON);
	return (
		<Stack gap={4} w="100%">
			{view.text ? (
				<Group gap="xs" align="flex-start" wrap="nowrap">
					<Text
						size="sm"
						fw={500}
						c={
							view.tone === "saved" || view.tone === "published"
								? undefined
								: "orange"
						}
						style={{ wordBreak: "break-all" }}
					>
						{view.text}
					</Text>
					{view.path ? <CopyPathButton path={view.path} /> : null}
				</Group>
			) : null}
			{view.note ? (
				<Text
					size="sm"
					style={{ whiteSpace: "pre-wrap", wordBreak: "break-all" }}
				>
					{view.note}
				</Text>
			) : null}
			{view.retained ? (
				<Group gap="xs" align="flex-start" wrap="nowrap">
					<Text size="sm" c="orange" style={{ wordBreak: "break-all" }}>
						{view.retained.text}
					</Text>
					<CopyPathButton path={view.retained.path} />
				</Group>
			) : null}
			{view.current ? (
				<Text size="sm" c="dimmed">
					{view.current}
				</Text>
			) : null}
			{offersContinue || offersPublish ? (
				<Group gap="xs">
					{offersContinue ? (
						<DraftContinueButtons
							size="compact-xs"
							continuation={followUp.continuation}
							blocked={followUp.blocked}
							submitting={followUp.submitting}
							onContinue={followUp.onContinue}
							onSendNoted={followUp.onSendNoted}
						/>
					) : null}
					{offersPublish ? (
						<DraftPublishButton
							size="compact-xs"
							toolName={followUp.toolName}
							continuation={followUp.continuation}
							blocked={followUp.blocked}
							submitting={followUp.publishing}
							onPublish={followUp.onPublish}
						/>
					) : null}
				</Group>
			) : null}
		</Stack>
	);
}
