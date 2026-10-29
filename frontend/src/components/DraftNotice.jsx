import { Alert, Button, Code, Group, Modal, Stack, Text } from "@mantine/core";
import { useState } from "react";
import {
	DISCARD_BUTTON,
	draftDiscardConfirmation,
	draftView,
} from "../utils/toolDraft.js";
import { CopyPathButton } from "./CopyPathButton.jsx";

// The tool row's note about its draft -- the unfinished revision a revise left
// when it did not publish: what state it is in, how many rounds it has been
// through and how the last one ended, what the AI says the operator has to do by
// hand, where the files are (to be opened or copied as they are), and 放棄. What it
// says and which buttons a state gets is decided by draftView.
//
// 放棄 asks first, and the confirmation acts on the draft the operator was looking
// at when they pressed it (`confirming`), not on whatever the list shows by the
// time they confirm: the request names that draft (by id and generation, or by
// token), so one that changed meanwhile is refused by the backend instead of
// discarded unseen.
//
// `blocked` is the page's gate for every write that takes the backend's one slot;
// the mutation lives in the page (`onDiscard`), with the other writes of a row.
export function DraftNotice({
	toolName,
	draft,
	currentVid,
	blocked,
	discarding,
	onDiscard,
}) {
	const [confirming, setConfirming] = useState(null);
	const view = draftView(draft, { currentVid });
	if (view === null) {
		return null;
	}
	const canDiscard = view.buttons.includes(DISCARD_BUTTON);
	const confirm = () => {
		const target = confirming;
		setConfirming(null);
		onDiscard(target);
	};
	return (
		<>
			<Alert color={view.color} title={view.title}>
				<Stack gap="xs">
					<Text size="sm">{view.note}</Text>
					{view.consequence ? (
						<Text size="sm" c="dimmed">
							{view.consequence}
						</Text>
					) : null}

					<Stack gap={2}>
						{view.rounds ? <Text size="sm">{view.rounds}</Text> : null}
						{view.interruption ? (
							<Text size="sm">{view.interruption}</Text>
						) : null}
						{view.lastOutcome ? (
							<Text size="sm">上一輪：{view.lastOutcome}</Text>
						) : null}
						{view.stopReason ? (
							<Text size="sm">停止原因：{view.stopReason}</Text>
						) : null}
						{view.omitted ? <Text size="sm">{view.omitted}</Text> : null}
					</Stack>

					{view.operatorAction ? (
						<Alert color="yellow" variant="outline" title="需要你動手">
							<Text size="sm" style={{ whiteSpace: "pre-wrap" }}>
								{view.operatorAction}
							</Text>
						</Alert>
					) : null}

					{view.summary ? (
						<Stack gap={2}>
							<Text size="xs" c="dimmed">
								AI 上一輪的總結（摘錄）
							</Text>
							<Text size="sm" style={{ whiteSpace: "pre-wrap" }}>
								{view.summary}
							</Text>
						</Stack>
					) : null}

					<Stack gap={2}>
						<Text size="xs" c="dimmed">
							{view.pathLabel}
						</Text>
						<Group gap="xs" align="flex-start" wrap="nowrap">
							{/* One click selects the whole path, for the browser that will
							    not copy it (see CopyPathButton). */}
							<Code style={{ wordBreak: "break-all", userSelect: "all" }}>
								{view.path}
							</Code>
							<CopyPathButton path={view.path} />
						</Group>
					</Stack>

					{canDiscard ? (
						<Group justify="flex-end">
							<Button
								size="xs"
								color="red"
								variant="light"
								loading={discarding}
								disabled={blocked}
								onClick={() => setConfirming(draft)}
							>
								放棄
							</Button>
						</Group>
					) : null}
				</Stack>
			</Alert>

			<Modal
				opened={confirming !== null}
				onClose={() => setConfirming(null)}
				title="放棄草稿"
				centered
			>
				<Stack gap="md">
					<Text>{draftDiscardConfirmation(toolName)}</Text>
					<Group justify="flex-end">
						<Button variant="default" onClick={() => setConfirming(null)}>
							取消
						</Button>
						<Button color="red" disabled={blocked} onClick={confirm}>
							放棄
						</Button>
					</Group>
				</Stack>
			</Modal>
		</>
	);
}
