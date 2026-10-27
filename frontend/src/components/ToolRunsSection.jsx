import {
	Alert,
	Anchor,
	Badge,
	Button,
	Center,
	Code,
	Collapse,
	Group,
	Loader,
	Menu,
	Modal,
	Stack,
	Text,
	Textarea,
} from "@mantine/core";
import { useDisclosure } from "@mantine/hooks";
import { notifications } from "@mantine/notifications";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useState } from "react";
import { apiDelete, apiGet } from "../api/client.js";
import { SECTION_MAX_LENGTH } from "../constants/sections.js";
import { formatDuration, formatTimestamp } from "../utils/format.js";
import { codePointLength } from "../utils/text.js";
import {
	runDetailSections,
	runHasDetails,
	runOutcomeMeta,
	runWorkflowLabel,
	TOOL_RUNS_LIMIT,
	toolRunQueryKey,
	toolRunsQueryKey,
	vidStateMeta,
} from "../utils/toolRuns.js";
import {
	TOOL_PURGE_PRESETS,
	toolRunStatsQueryKey,
} from "../utils/toolRunsPurge.js";
import { logLinkSearch } from "../utils/toolSummary.js";
import { CharCounter } from "./CharCounter.jsx";
import { ToolRunsPurgeDialog } from "./ToolRunsPurgeDialog.jsx";

// Bounded, whitespace-preserving box for a body (up to 16,000 chars of stderr):
// the same shape the AI 日誌 page uses for its prompt/response bodies.
const BODY_STYLE = {
	maxHeight: 280,
	overflow: "auto",
	whiteSpace: "pre-wrap",
	wordBreak: "break-word",
};

// One string, not two JSX lines: a line break inside JSX text renders as a
// space, which splits a Chinese sentence.
const RETENTION_NOTE =
	"失敗的執行會連同輸入參數與錯誤輸出（已遮蔽秘密）一起保存；成功的只記時間與結果。";
const STALE_LIST_NOTE =
	"。以下是先前讀到的內容，可能已過期；重新讀取成功前不能從這裡請 AI 修正。";

// The 最近執行 section of one tool row (web-v6 §6, §9): the newest run records,
// newest first, fetched lazily (enabled: expanded) like the 設定 section and
// offered for every package -- a package whose pointer broke still has a
// history worth reading. Read-only apart from deleting: one record (for a run
// whose contents the operator does not want kept) from its detail, or many
// from the 清除紀錄 menu, which previews before it deletes (web-v6 §7).
//
// `onFix(runIds, feedback?)` is the one-click fix (web-v6 §8): null when the tool
// has no version a revise could start from, `fixBlocked` the page's gate for
// every AI write. Only a record the backend marked `fixable` offers it -- and
// `fixable` is the backend's judgement AT FETCH TIME, so the buttons also wait
// while this list is refetching or after a refetch failed: the copy on screen
// may predate a fix that already published (a re-expanded row renders its
// cached list first), and a click would pay for a builder session on a record
// the backend no longer offers. The same rule as 送出修訂's displayedMayBeStale.
export function ToolRunsSection({
	name,
	expanded,
	onFix = null,
	fixBlocked = false,
	isSubmittingFix = false,
}) {
	const [purgeTarget, setPurgeTarget] = useState(null);
	const [noteRun, setNoteRun] = useState(null);
	const { data, error, isError, isFetching } = useQuery({
		queryKey: toolRunsQueryKey(name),
		queryFn: () => apiGet(`/api/tools/${name}/runs?limit=${TOOL_RUNS_LIMIT}`),
		enabled: expanded,
	});

	if (data === undefined && isFetching) {
		return (
			<Center py="sm">
				<Loader size="sm" />
			</Center>
		);
	}
	if (data === undefined) {
		return isError ? (
			<Alert color="red" title="無法載入執行紀錄">
				<Text size="sm">{error?.message ?? "請稍後再試"}</Text>
			</Alert>
		) : null;
	}
	if (data.runs.length === 0) {
		return (
			<Text size="sm" c="dimmed" pt="xs">
				還沒有執行紀錄。AI 呼叫這個工具時，每一次都會記在這裡。
			</Text>
		);
	}
	const blocked = fixBlocked || isFetching || isError;
	return (
		<Stack gap="xs" pt="xs">
			{/* Reached with data in hand: a BACKGROUND refetch failed and left the
			    previous list rendered. Labelled, like the AI 總結 panel's. */}
			{isError ? (
				<Alert color="orange" title="無法更新執行紀錄">
					<Text size="sm">{`${error?.message ?? "請稍後再試"}${STALE_LIST_NOTE}`}</Text>
				</Alert>
			) : null}
			<Group justify="space-between" align="flex-start" wrap="nowrap" gap="sm">
				<Text size="xs" c="dimmed">
					{`共 ${data.total} 筆，顯示最近 ${data.runs.length} 筆。`}
					{RETENTION_NOTE}
				</Text>
				<Menu position="bottom-end" withinPortal>
					<Menu.Target>
						<Button size="compact-xs" variant="subtle" color="red">
							清除紀錄
						</Button>
					</Menu.Target>
					<Menu.Dropdown>
						{TOOL_PURGE_PRESETS.map((preset) => (
							<Menu.Item
								key={preset.key}
								onClick={() =>
									setPurgeTarget({
										scope: { tool: name, ...preset.scope },
										now: new Date(),
										editable: false,
									})
								}
							>
								{preset.label}
							</Menu.Item>
						))}
					</Menu.Dropdown>
				</Menu>
			</Group>
			{data.runs.map((run) => (
				<ToolRunRow
					key={run.id}
					name={name}
					run={run}
					fix={
						run.fixable && onFix
							? {
									blocked,
									submitting: isSubmittingFix,
									now: () => onFix([run.id]),
									withNote: () => setNoteRun(run),
								}
							: null
					}
				/>
			))}
			<FixNoteModal
				run={noteRun}
				blocked={blocked}
				onClose={() => setNoteRun(null)}
				onSubmit={(feedback) => {
					onFix?.([noteRun.id], feedback);
					setNoteRun(null);
				}}
			/>
			<ToolRunsPurgeDialog
				target={purgeTarget}
				onClose={() => setPurgeTarget(null)}
			/>
		</Stack>
	);
}

function ToolRunRow({ name, run, fix }) {
	const [open, { toggle }] = useDisclosure(false);
	const outcome = runOutcomeMeta(run.outcome);
	const version = vidStateMeta(run.vid_state);
	return (
		<Stack gap={4}>
			<Group gap="xs" wrap="wrap">
				<Text size="sm">{formatTimestamp(run.started_at)}</Text>
				<Badge size="sm" variant="light" color={outcome.color} tt="none">
					{outcome.label}
				</Badge>
				<Text size="xs" c="dimmed">
					{runWorkflowLabel(run.workflow)} ・ 耗時{" "}
					{formatDuration(run.duration_ms)}
				</Text>
				<Badge size="xs" variant="outline" color={version.color} tt="none">
					{version.label}
				</Badge>
				{run.truncated ? (
					<Text size="xs" c="dimmed">
						輸出過長，AI 只收到開頭
					</Text>
				) : null}
				{runHasDetails(run) ? (
					<Button variant="subtle" size="compact-xs" onClick={toggle}>
						{open ? "收合" : "詳情"}
					</Button>
				) : null}
				{fix ? (
					<>
						<Button
							size="compact-xs"
							variant="light"
							color="red"
							disabled={fix.blocked}
							loading={fix.submitting}
							onClick={fix.now}
						>
							請 AI 修正
						</Button>
						<Button
							size="compact-xs"
							variant="subtle"
							disabled={fix.blocked}
							onClick={fix.withNote}
						>
							加上說明…
						</Button>
					</>
				) : null}
			</Group>
			{/* `expanded`, not `in` -- see the AI 總結 Collapse in ToolsPage. */}
			<Collapse expanded={open}>
				<ToolRunDetail name={name} run={run} expanded={open} />
			</Collapse>
		</Stack>
	);
}

function ToolRunDetail({ name, run, expanded }) {
	const queryClient = useQueryClient();
	const [confirmingDelete, setConfirmingDelete] = useState(false);
	const { data, error, isError, isFetching } = useQuery({
		queryKey: toolRunQueryKey(run.id),
		queryFn: () => apiGet(`/api/tool-runs/${run.id}`),
		enabled: expanded,
	});
	const deleteMutation = useMutation({
		mutationFn: () => apiDelete(`/api/tool-runs/${run.id}`),
		onSuccess: async () => {
			notifications.show({ color: "green", message: "已刪除這筆執行紀錄" });
			// The record's own cached detail is left to expire, NOT removed: this
			// component is still mounted and observing it, and removing an
			// observed query makes it fetch again -- a 404 for the id just
			// deleted. The list refetch below unmounts the row; ids are never
			// reused (AUTOINCREMENT), so the leftover entry is unreachable.
			// The list row's last_run may have been this record.
			await Promise.all([
				queryClient.invalidateQueries({ queryKey: toolRunsQueryKey(name) }),
				queryClient.invalidateQueries({ queryKey: ["tools"] }),
				queryClient.invalidateQueries({ queryKey: toolRunStatsQueryKey }),
			]);
		},
		onError: (mutationError) => {
			notifications.show({
				color: "red",
				title: "刪除失敗",
				message: mutationError?.message ?? "無法刪除這筆執行紀錄",
			});
		},
	});

	if (data === undefined && isFetching) {
		return (
			<Center py="xs">
				<Loader size="xs" />
			</Center>
		);
	}
	if (data === undefined) {
		return isError ? (
			<Alert color="red" title="無法載入這筆紀錄">
				<Text size="sm">{error?.message ?? "請稍後再試"}</Text>
			</Alert>
		) : null;
	}
	return (
		<Stack gap="xs" pl="sm" pb="xs">
			{data.exit_code != null ? (
				<Text size="xs" c="dimmed">
					結束代碼 {data.exit_code}
				</Text>
			) : null}
			{runDetailSections(data).map((section) => (
				<Stack key={section.key} gap={2}>
					<Text size="xs" fw={600}>
						{section.label}
					</Text>
					{section.empty ? (
						<Text size="xs" c="dimmed">
							（沒有輸出）
						</Text>
					) : (
						<Code block style={BODY_STYLE}>
							{section.text}
						</Code>
					)}
				</Stack>
			))}
			<Group gap="sm">
				{data.llm_log_id != null ? (
					<Anchor
						component={Link}
						to="/llm-logs"
						search={logLinkSearch(data.llm_log_id, data.llm_log_process)}
						size="sm"
					>
						查看這次對話的 AI 日誌（紀錄 #{data.llm_log_id}）
					</Anchor>
				) : null}
				{confirmingDelete ? (
					<>
						<Text size="xs">刪除後無法復原，確定？</Text>
						<Button
							size="compact-xs"
							color="red"
							loading={deleteMutation.isPending}
							onClick={() => deleteMutation.mutate()}
						>
							確定刪除
						</Button>
						<Button
							size="compact-xs"
							variant="default"
							disabled={deleteMutation.isPending}
							onClick={() => setConfirmingDelete(false)}
						>
							取消
						</Button>
					</>
				) : (
					<Button
						size="compact-xs"
						variant="subtle"
						color="red"
						onClick={() => setConfirmingDelete(true)}
					>
						刪除這筆紀錄
					</Button>
				)}
			</Group>
		</Stack>
	);
}

// 加上說明… (web-v6 §8.1): the same fix, with words. The record rides along; the
// note is optional, and bounded like every other AI input.
function FixNoteModal({ run, blocked, onClose, onSubmit }) {
	const [note, setNote] = useState("");
	const tooLong = codePointLength(note.trim()) > SECTION_MAX_LENGTH;
	// Every way out clears the draft, so the next record never opens with a note
	// written about another one.
	const close = () => {
		setNote("");
		onClose();
	};
	return (
		<Modal
			opened={run !== null}
			onClose={close}
			title="請 AI 修正這次失敗"
			centered
		>
			<Stack gap="sm">
				<Text size="sm">
					這次失敗的參數與錯誤輸出會一起交給 AI。想補充背景（例如這個 API
					最近改了什麼）就寫在下面，可以留空。
				</Text>
				<div>
					<Textarea
						label="補充說明（選填）"
						autosize
						minRows={3}
						value={note}
						onChange={(event) => setNote(event.currentTarget.value)}
						error={tooLong ? `不可超過 ${SECTION_MAX_LENGTH} 字` : null}
					/>
					<CharCounter value={note} max={SECTION_MAX_LENGTH} />
				</div>
				<Group justify="flex-end" gap="sm">
					<Button variant="default" onClick={close}>
						取消
					</Button>
					<Button
						color="red"
						disabled={blocked || tooLong}
						onClick={() => {
							const feedback = note.trim();
							setNote("");
							onSubmit(feedback);
						}}
					>
						送出修正
					</Button>
				</Group>
			</Stack>
		</Modal>
	);
}
