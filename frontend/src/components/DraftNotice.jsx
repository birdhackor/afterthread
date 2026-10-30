import { Alert, Button, Code, Group, Modal, Stack, Text } from "@mantine/core";
import { useState } from "react";
import {
	CONTINUE_BUTTON,
	DISCARD_BUTTON,
	draftContinuation,
	draftDiscardConfirmation,
	draftView,
	PUBLISH_BUTTON,
} from "../utils/toolDraft.js";
import { CopyPathButton } from "./CopyPathButton.jsx";
import { DraftContinueButtons } from "./DraftContinueButtons.jsx";
import { DraftPublishButton } from "./DraftPublishButton.jsx";

// One path the operator may need to open by hand, as a block that selects in one
// click (for the browser that will not copy it -- see CopyPathButton) and 複製路徑.
function PathRow({ path }) {
	return (
		<Group gap="xs" align="flex-start" wrap="nowrap">
			<Code style={{ wordBreak: "break-all", userSelect: "all" }}>{path}</Code>
			<CopyPathButton path={path} />
		</Group>
	);
}

// The tool row's note about its draft -- the unfinished revision a revise left
// when it did not publish: what state it is in, how many rounds it has been
// through and how the last one ended, what the AI says the operator has to do by
// hand, where the files are (the files the draft names, the whole draft directory
// and the old files directories a continued round kept, each to be opened or
// copied as they are), and the buttons: 接續修正, 加上說明後接續… and 發布草稿 for a
// draft that can be continued, and 放棄. What it says and which buttons a state
// gets is decided by draftView.
//
// 放棄 asks first, and the confirmation acts on the draft the operator was looking
// at when they pressed it (`confirming`), not on whatever the list shows by the
// time they confirm: the request names that draft (by id and generation, or by
// token), so one that changed meanwhile is refused by the backend instead of
// discarded unseen. The confirmation names the whole draft directory, since that
// is what goes.
//
// The mutations live in the page, with the other writes of a row: `onDiscard`
// (with the draft confirmed on), `onContinue` (接續修正: the page names the draft
// its row shows at the click and checks again), `onSendNoted` (加上說明後接續…:
// the draft the dialog opened on, with the words; see DraftContinueButtons) and
// `onPublish` (發布草稿: the draft its confirmation opened on; see
// DraftPublishButton). `blocked` is the page's gate for every write that takes the
// backend's one slot; `writesBlocked` is its gate for the version writes, which a
// round that continues a draft and a publish of it are.
export function DraftNotice({
	toolName,
	draft,
	currentVid,
	blocked,
	discarding,
	onDiscard,
	writesBlocked,
	continuing,
	onContinue,
	onSendNoted,
	publishing,
	onPublish,
}) {
	const [confirming, setConfirming] = useState(null);
	const view = draftView(draft, { currentVid });
	if (view === null) {
		return null;
	}
	const canDiscard = view.buttons.includes(DISCARD_BUTTON);
	const canContinue = view.buttons.includes(CONTINUE_BUTTON);
	const canPublish = view.buttons.includes(PUBLISH_BUTTON);
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
						<PathRow path={view.path} />
					</Stack>

					{view.draftDir ? (
						<Stack gap={2}>
							<Text size="xs" c="dimmed">
								整個草稿目錄
							</Text>
							<PathRow path={view.draftDir} />
						</Stack>
					) : null}

					{view.kept.length > 0 ? (
						<Stack gap={2}>
							<Text size="xs" c="dimmed">
								保留下來的舊檔案目錄
							</Text>
							{view.kept.map((path) => (
								<PathRow key={path} path={path} />
							))}
							<Text size="xs" c="dimmed">
								{view.keptNote}
							</Text>
						</Stack>
					) : null}

					<Text size="xs" c="dimmed">
						{view.unlisted}
					</Text>

					{canContinue || canPublish || canDiscard ? (
						<Group justify="space-between" gap="xs">
							<Group gap="xs">
								{canContinue ? (
									<DraftContinueButtons
										continuation={draftContinuation(draft)}
										blocked={writesBlocked}
										submitting={continuing}
										onContinue={onContinue}
										onSendNoted={onSendNoted}
									/>
								) : null}
								{canPublish ? (
									<DraftPublishButton
										toolName={toolName}
										continuation={draftContinuation(draft)}
										blocked={writesBlocked}
										submitting={publishing}
										onPublish={onPublish}
									/>
								) : null}
							</Group>
							{canDiscard ? (
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
							) : null}
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
					<Text style={{ wordBreak: "break-all" }}>
						{draftDiscardConfirmation(toolName, confirming)}
					</Text>
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
