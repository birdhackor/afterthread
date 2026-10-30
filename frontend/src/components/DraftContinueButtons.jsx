import { Button, Group, Modal, Stack, Text, Textarea } from "@mantine/core";
import { useState } from "react";
import { SECTION_MAX_LENGTH } from "../constants/sections.js";
import { continuationNote } from "../utils/toolDraft.js";
import { CharCounter } from "./CharCounter.jsx";

// One string, not JSX text: a line break inside JSX text renders as a space, which
// splits a Chinese sentence wherever a formatter puts the break.
const NOTE_INTRO =
	"這段說明會連同草稿的紀錄（原本的意見、上一輪的交接筆記等）一起交給這一輪的 AI；這一輪記進草稿時，說明也一起記下。可以留空。";

// 接續修正 and 加上說明後接續… for a draft that can be continued: on the tool row's
// draft note, and on a revise card whose round left the tool's current draft. The
// first sends a round at once, with no words; the second asks for the round's words
// first -- optional, and bounded like every AI input (continuationNote).
//
// `continuation` is the draft on screen, as the page names it
// (toolDraft.draftContinuation). 接續修正 calls `onContinue()`, and the page sends a
// round for the draft its row shows at the click. 加上說明後接續… keeps
// `continuation` as it is when the dialog opens, and sends that, with the words,
// through `onSendNoted(target, feedback)`: the words are written for the draft the
// operator was looking at, so a list read again behind the open dialog changes
// neither what the dialog says nor what it sends, and a draft that changed is the
// backend's to refuse (toolDraft.reviseVariables). The dialog lives with these
// buttons: when they leave the screen -- the draft gone, or no longer one that can
// be continued, or a card no longer offering a round -- it closes with them and
// sends nothing. `blocked` is the page's gate for the writes that take the backend's
// one slot; the dialog's submit answers to it too. `size` is the buttons' size where
// they sit (a card's are compact).
export function DraftContinueButtons({
	continuation,
	blocked,
	submitting,
	onContinue,
	onSendNoted,
	size = "xs",
}) {
	// The draft the open dialog is for; null while it is closed.
	const [noting, setNoting] = useState(null);
	const [note, setNote] = useState("");
	const checked = continuationNote(note);
	// Every way out clears the words, so the next round never opens with words
	// written for another one.
	const close = () => {
		setNote("");
		setNoting(null);
	};
	return (
		<>
			<Button
				size={size}
				loading={submitting}
				disabled={blocked}
				onClick={() => onContinue()}
			>
				接續修正
			</Button>
			<Button
				size={size}
				variant="light"
				disabled={blocked || continuation == null}
				onClick={() => setNoting(continuation)}
			>
				加上說明後接續…
			</Button>
			<Modal
				opened={noting !== null}
				onClose={close}
				title="加上說明後接續"
				centered
			>
				<Stack gap="sm">
					<Text size="sm">{NOTE_INTRO}</Text>
					<div>
						<Textarea
							label="這一輪的說明（選填）"
							autosize
							minRows={3}
							value={note}
							onChange={(event) => setNote(event.currentTarget.value)}
							error={checked.error}
						/>
						<CharCounter value={note} max={SECTION_MAX_LENGTH} />
					</div>
					<Group justify="flex-end" gap="sm">
						<Button variant="default" onClick={close}>
							取消
						</Button>
						<Button
							disabled={blocked || checked.error !== null}
							onClick={() => {
								const target = { continuation: noting, runIds: [] };
								const feedback = checked.feedback;
								close();
								onSendNoted(target, feedback);
							}}
						>
							接續修正
						</Button>
					</Group>
				</Stack>
			</Modal>
		</>
	);
}
