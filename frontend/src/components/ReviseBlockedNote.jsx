import { Text } from "@mantine/core";

// Beside a control that would send a revise while its tool has a draft: why the
// control is off and what to do first. Whether the tool is blocked at all, and the
// words, are decided by reviseBlockedByDraft; the caller passes its answer, and
// this draws it -- nothing for null.
export function ReviseBlockedNote({ reason }) {
	if (reason == null) {
		return null;
	}
	return (
		<Text size="xs" c="orange.9">
			{reason}
		</Text>
	);
}
