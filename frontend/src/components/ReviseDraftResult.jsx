import { Group, Text } from "@mantine/core";
import { jobDraftView } from "../utils/toolDraft.js";
import { CopyPathButton } from "./CopyPathButton.jsx";

// Under a finished AI revise, published or not: what became of the round's work --
// kept as the tool's draft (and which round that makes it), not kept (and why),
// left in a directory when a failed publish found no draft to hold it, or, for a
// revise that went live, that its draft could not be taken away. Renders nothing
// when the round had nothing to keep. Kept apart from ReviseStopNotice, which is
// about how the session stopped: this is about where the work went. What is said,
// and when, is decided by jobDraftView.
export function ReviseDraftResult({ job, kind }) {
	const view = jobDraftView(job, kind);
	if (view === null) {
		return null;
	}
	return (
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
	);
}
