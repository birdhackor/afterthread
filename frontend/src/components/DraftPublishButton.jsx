import { Button, Group, Modal, Stack, Text } from "@mantine/core";
import { useState } from "react";
import {
	draftPublishConfirmation,
	draftPublishTarget,
} from "../utils/toolDraftPublish.js";
import { BulletList } from "./BulletList.jsx";

// 發布草稿 for a draft that can be continued: on the tool row's draft note, and on a
// job card whose job left the tool's current draft. It asks first, and publishes the
// draft's files as they are, with no round of the AI.
//
// `continuation` is the draft on screen, as the page names it
// (toolDraft.draftContinuation). The button keeps the publish it opens the
// confirmation for -- the tool, the draft's id and generation, and the version it
// was made from (toolDraftPublish.draftPublishTarget) -- and confirming sends that
// through `onPublish(target)`, whatever the list shows by then. A draft that changed
// meanwhile is the backend's to refuse (toolDraftPublish.draftPublishVariables). The
// confirmation lives with the button: when the button leaves the screen -- the draft
// gone, or no longer one that can be continued, or a card no longer offering it -- it
// closes with it and sends nothing. `blocked` is the page's gate for the writes that
// take the backend's one slot; the confirmation answers to it too. `size` is the
// button's size where it sits (a card's is compact).
export function DraftPublishButton({
	toolName,
	continuation,
	blocked,
	submitting,
	onPublish,
	size = "xs",
}) {
	// The publish the open confirmation is for; null while it is closed.
	const [confirming, setConfirming] = useState(null);
	const words = draftPublishConfirmation(toolName);
	const close = () => setConfirming(null);
	return (
		<>
			<Button
				size={size}
				variant="light"
				loading={submitting}
				disabled={blocked || continuation == null}
				onClick={() =>
					setConfirming(draftPublishTarget(toolName, continuation))
				}
			>
				發布草稿
			</Button>
			<Modal
				opened={confirming !== null}
				onClose={close}
				title={words.title}
				centered
			>
				<Stack gap="md">
					<Text size="sm">{words.lead}</Text>
					<BulletList items={words.points} />
					<Group justify="flex-end" gap="sm">
						<Button variant="default" onClick={close}>
							取消
						</Button>
						<Button
							disabled={blocked}
							onClick={() => {
								const target = confirming;
								close();
								if (target !== null) {
									onPublish(target);
								}
							}}
						>
							發布草稿
						</Button>
					</Group>
				</Stack>
			</Modal>
		</>
	);
}
