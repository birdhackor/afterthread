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
// Which draft a round continues, and whether one may be sent now, are the page's:
// `onContinue` gets the words (possibly empty) and checks again before it sends;
// `blocked` is the page's gate for the writes that take the backend's one slot, and
// the dialog answers to it too, since the list can change behind an open dialog.
// `size` is the buttons' size where they sit (a card's are compact).
export function DraftContinueButtons({
	blocked,
	submitting,
	onContinue,
	size = "xs",
}) {
	const [noting, setNoting] = useState(false);
	const [note, setNote] = useState("");
	const checked = continuationNote(note);
	// Every way out clears the words, so the next round never opens with words
	// written for another one.
	const close = () => {
		setNote("");
		setNoting(false);
	};
	return (
		<>
			<Button
				size={size}
				loading={submitting}
				disabled={blocked}
				onClick={() => onContinue("")}
			>
				接續修正
			</Button>
			<Button
				size={size}
				variant="light"
				disabled={blocked}
				onClick={() => setNoting(true)}
			>
				加上說明後接續…
			</Button>
			<Modal opened={noting} onClose={close} title="加上說明後接續" centered>
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
								const feedback = checked.feedback;
								close();
								onContinue(feedback);
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
