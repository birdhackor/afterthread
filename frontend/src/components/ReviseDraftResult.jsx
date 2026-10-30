import { Group, Stack, Text } from "@mantine/core";
import { jobDraftView } from "../utils/toolDraft.js";
import { CopyPathButton } from "./CopyPathButton.jsx";
import { DraftContinueButtons } from "./DraftContinueButtons.jsx";

// Under a finished AI revise, published or not: what became of the round's work --
// kept as the tool's draft (and which round that makes it, with the backend's note
// about the save and where the work was also left when the save could not be
// confirmed), not kept (and why), left in a directory and not in the draft, or, for
// a revise that went live, why its draft is still there. Then, when the page says
// the list was read after the job ended and the card has a row, what the card may
// say about the tool's current draft and the round it may send from here. Renders
// nothing when there is nothing to say. Kept apart from ReviseStopNotice, which is
// about how the session stopped: this is about where the work went. What is said,
// and when, is decided by jobDraftView.
//
// `followUp` comes from the page: { context, blocked, submitting, continuation,
// onContinue, onSendNoted } -- `context` is jobDraftView's view of the card's row,
// `continuation` the row's draft a round from here continues, and the rest go to
// DraftContinueButtons; null for a card that offers nothing about the current draft
// (an install, a card no row owns).
export function ReviseDraftResult({ job, kind, followUp = null }) {
	const view = jobDraftView(job, kind, followUp?.context);
	if (view === null) {
		return null;
	}
	const offersRound = followUp !== null && view.buttons.length > 0;
	return (
		<Stack gap={4} w="100%">
			{view.text ? (
				<Group gap="xs" align="flex-start" wrap="nowrap">
					<Text
						size="sm"
						fw={500}
						c={view.tone === "saved" ? undefined : "orange"}
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
			{offersRound ? (
				<Group gap="xs">
					<DraftContinueButtons
						size="compact-xs"
						continuation={followUp.continuation}
						blocked={followUp.blocked}
						submitting={followUp.submitting}
						onContinue={followUp.onContinue}
						onSendNoted={followUp.onSendNoted}
					/>
				</Group>
			) : null}
		</Stack>
	);
}
