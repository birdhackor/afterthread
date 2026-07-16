import {
	Accordion,
	Alert,
	Badge,
	Box,
	Button,
	Card,
	Center,
	Code,
	Group,
	Loader,
	Stack,
	Text,
	Title,
} from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { apiGet } from "../api/client.js";
import { EmptyState } from "../components/EmptyState.jsx";
import { usePageTitle } from "../hooks/usePageTitle.js";

// The list page reads at most this many recent interactions; the backend caps
// the ring itself (llm_log_max_entries) and validates the limit to [1, 500].
const LIST_LIMIT = 50;

// Workflow -> zh-TW label. Mirrors the workflow names generate_structured is
// called with (context_memory/services/memory_ai.py); an unrecognized value
// falls back to 其他 rather than showing a raw English token.
const WORKFLOW_LABELS = {
	capture: "快速捕捉",
	enrich: "AI 補齊",
	assist_update: "AI 進度更新",
	unknown: "其他",
};

// Outcome -> badge label + color, matching the service's outcome vocabulary
// (context_memory/services/llm.py). ok is the only success; timeout and
// upstream_error are both red (an upstream/deadline failure), invalid_output is
// orange (the model replied but unusably), not_configured is a neutral gray.
const OUTCOME_META = {
	ok: { label: "成功", color: "green" },
	timeout: { label: "逾時", color: "red" },
	upstream_error: { label: "上游失敗", color: "red" },
	invalid_output: { label: "輸出無效", color: "orange" },
	not_configured: { label: "未設定", color: "gray" },
};

// Fixed column widths shared by the header row and every Accordion.Control so
// the summary cells line up as columns despite living inside accordion buttons.
const COLUMNS = [
	{ key: "time", label: "時間", width: 172 },
	{ key: "workflow", label: "工作流", width: 104 },
	{ key: "outcome", label: "結果", width: 96 },
	{ key: "duration", label: "耗時", width: 88 },
	{ key: "tokens", label: "tokens", width: 88 },
	{ key: "attempts", label: "嘗試次數", width: 72 },
];

// Scrollable, whitespace-preserving box for the (possibly tens-of-KB) prompt
// and response bodies: pre-wrap keeps the model's own line breaks and spacing
// while wrapping long lines, and the max-height + auto overflow bounds the
// panel so one huge body cannot push the rest of the page away.
const CODE_SCROLL_STYLE = {
	maxHeight: 320,
	overflow: "auto",
	whiteSpace: "pre-wrap",
	wordBreak: "break-word",
};

// Local date+time formatter: DateText renders only YYYY-MM-DD, but log rows
// within one day need the time to be distinguishable. started_at is an ISO
// UTC string; this renders it in the viewer's local zone. Returns "—" for a
// missing/invalid value so a malformed record never blanks the cell.
function formatTimestamp(value) {
	if (!value) {
		return "—";
	}
	const date = new Date(value);
	if (Number.isNaN(date.getTime())) {
		return "—";
	}
	const pad = (part) => String(part).padStart(2, "0");
	const ymd = `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
	const hms = `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
	return `${ymd} ${hms}`;
}

// Milliseconds -> a compact human string; seconds once past 1s so a long
// generation reads as "12.3 s" rather than "12345 ms".
function formatDuration(ms) {
	if (typeof ms !== "number") {
		return "—";
	}
	return ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${ms} ms`;
}

function OutcomeBadge({ outcome }) {
	const meta = OUTCOME_META[outcome] ?? {
		label: outcome ?? "—",
		color: "gray",
	};
	return (
		<Badge size="sm" variant="light" color={meta.color}>
			{meta.label}
		</Badge>
	);
}

// Column header row, padded to roughly match the Accordion.Control's own left
// inset so the labels sit above their cells.
function HeaderRow() {
	return (
		<Group gap="md" wrap="nowrap" px="md" py={4}>
			{COLUMNS.map((column) => (
				<Box key={column.key} w={column.width}>
					<Text size="xs" fw={600} c="dimmed">
						{column.label}
					</Text>
				</Box>
			))}
		</Group>
	);
}

// One summary row rendered inside an Accordion.Control (the columns, minus the
// chevron the Accordion adds on the right).
function SummaryRow({ log }) {
	return (
		<Group gap="md" wrap="nowrap">
			<Box w={COLUMNS[0].width}>
				<Text size="sm">{formatTimestamp(log.started_at)}</Text>
			</Box>
			<Box w={COLUMNS[1].width}>
				<Text size="sm">
					{WORKFLOW_LABELS[log.workflow] ?? WORKFLOW_LABELS.unknown}
				</Text>
			</Box>
			<Box w={COLUMNS[2].width}>
				<OutcomeBadge outcome={log.outcome} />
			</Box>
			<Box w={COLUMNS[3].width}>
				<Text size="sm">{formatDuration(log.duration_ms)}</Text>
			</Box>
			<Box w={COLUMNS[4].width}>
				<Text size="sm">{log.usage?.total_tokens ?? "—"}</Text>
			</Box>
			<Box w={COLUMNS[5].width}>
				<Text size="sm">{log.attempts}</Text>
			</Box>
		</Group>
	);
}

// One attempt's request messages + response, each in a scrollable code block.
function AttemptCard({ attempt, index }) {
	return (
		<Card withBorder padding="sm" radius="md">
			<Stack gap="xs">
				<Group gap="xs">
					<Text fw={600} size="sm">
						嘗試 {index + 1}
					</Text>
					{attempt.error ? (
						<Badge size="sm" variant="light" color="red">
							{attempt.error}
						</Badge>
					) : null}
				</Group>

				<Text size="xs" fw={600} c="dimmed">
					要求訊息
				</Text>
				{attempt.request_messages.map((message, messageIndex) => (
					// biome-ignore lint/suspicious/noArrayIndexKey: immutable fetched record; message position is its identity
					<div key={`${message.role}-${messageIndex}`}>
						<Text size="xs" c="dimmed">
							{message.role}
						</Text>
						<Code block style={CODE_SCROLL_STYLE}>
							{message.content}
						</Code>
					</div>
				))}

				<Text size="xs" fw={600} c="dimmed">
					回應
				</Text>
				<Code block style={CODE_SCROLL_STYLE}>
					{attempt.response_content ?? "（無回應）"}
				</Code>
			</Stack>
		</Card>
	);
}

// The expanded detail for one log row. Fetched lazily (enabled: expanded) so a
// collapsed row never pulls its (large) bodies, and keyed by log id so each
// row's detail is cached independently once opened.
function LogDetailPanel({ logId, expanded }) {
	const { data, error, isError, isFetching, refetch } = useQuery({
		queryKey: ["llm-log", logId],
		queryFn: () => apiGet(`/api/llm/logs/${logId}`),
		enabled: expanded,
	});

	if (data === undefined && isFetching) {
		return (
			<Center py="md">
				<Loader size="sm" />
			</Center>
		);
	}

	if (isError && data === undefined) {
		return (
			<Alert color="red" title="載入失敗">
				<Stack gap="sm" align="flex-start">
					<Text size="sm">{error?.message ?? "無法載入互動詳情"}</Text>
					<Button size="xs" onClick={() => refetch()}>
						重試
					</Button>
				</Stack>
			</Alert>
		);
	}

	if (!data) {
		return null;
	}

	return (
		<Stack gap="md">
			<Group gap="lg">
				<Text size="sm" c="dimmed">
					模型：{data.model || "—"}
				</Text>
				{data.error ? (
					<Text size="sm" c="red">
						錯誤：{data.error}
					</Text>
				) : null}
			</Group>
			{data.attempts.map((attempt, index) => (
				// biome-ignore lint/suspicious/noArrayIndexKey: immutable fetched record; attempt position is its identity
				<AttemptCard key={index} attempt={attempt} index={index} />
			))}
		</Stack>
	);
}

// The "AI 日誌" page: a list of recent LLM interactions (newest first) with a
// manual 重新整理, each row expandable to its full request/response bodies.
export function LlmLogsPage() {
	usePageTitle("AI 日誌");
	const [openValue, setOpenValue] = useState(null);

	const { data, error, isError, isFetching, refetch } = useQuery({
		queryKey: ["llm-logs"],
		queryFn: () => apiGet(`/api/llm/logs?limit=${LIST_LIMIT}`),
	});

	// Same first-load / retry discipline as HomePage: react-query keeps status
	// 'error' (not 'pending') while refetching after a failure, so
	// `data === undefined && isFetching` re-shows the Loader on 重新整理, and a
	// failed background refetch that still has prior rows falls through to them.
	const loading = data === undefined && isFetching;
	const showError = isError && data === undefined && !isFetching;
	const logs = data?.logs ?? [];

	return (
		<Stack gap="md">
			<Group justify="space-between" align="center">
				<Title order={2}>AI 日誌</Title>
				<Button variant="light" loading={isFetching} onClick={() => refetch()}>
					重新整理
				</Button>
			</Group>

			<Text size="sm" c="dimmed">
				記錄每次 AI
				互動的提示與回應，方便除錯。紀錄僅存於後端記憶體，重啟後清空。
			</Text>

			{loading ? (
				<Center py="xl">
					<Loader />
				</Center>
			) : null}

			{showError ? (
				<Alert color="red" title="載入失敗">
					<Stack gap="sm" align="flex-start">
						<Text size="sm">{error?.message ?? "無法載入 AI 日誌"}</Text>
						<Button size="xs" onClick={() => refetch()}>
							重試
						</Button>
					</Stack>
				</Alert>
			) : null}

			{data && logs.length === 0 ? (
				<EmptyState message="尚無 AI 互動紀錄" />
			) : null}

			{logs.length > 0 ? (
				<Stack gap={0}>
					<HeaderRow />
					<Accordion
						variant="separated"
						value={openValue}
						onChange={setOpenValue}
						chevronPosition="right"
					>
						{logs.map((log) => (
							<Accordion.Item key={log.id} value={String(log.id)}>
								<Accordion.Control>
									<SummaryRow log={log} />
								</Accordion.Control>
								<Accordion.Panel>
									<LogDetailPanel
										logId={log.id}
										expanded={openValue === String(log.id)}
									/>
								</Accordion.Panel>
							</Accordion.Item>
						))}
					</Accordion>
				</Stack>
			) : null}
		</Stack>
	);
}
