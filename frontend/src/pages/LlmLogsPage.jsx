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

// The columns above are wrap="nowrap" (see HeaderRow/SummaryRow), so their
// combined natural width (~700px: the column widths, the "md"-token 16px gaps
// between them, and the Accordion.Control's own padding + chevron) overflows a
// narrow/mobile viewport. Used below as the scroll container's inner min-width
// so that overflow is contained to this component -- a horizontal scrollbar on
// the list itself -- rather than pushing the whole page body wider.
const ROW_MIN_WIDTH =
	COLUMNS.reduce((total, column) => total + column.width, 0) +
	(COLUMNS.length - 1) * 16 +
	48;

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

// Compact, dimmed per-attempt stats line shown under the "嘗試 N" header:
// chars are always the STORED (post-truncation) length the backend actually
// kept (see llm_log._stored_body), and usage is THIS attempt's own reading --
// distinct from the aggregate the list row's "tokens" column shows -- so a
// corrective retry's two attempts can be told apart at a glance. "—" stands
// in for any missing number: no response yet, or a gateway that omitted/
// malformed that one usage field (see LlmLogUsage's per-field nullability).
function formatAttemptStats(attempt) {
	const responseChars = attempt.response_chars ?? "—";
	const usage = attempt.usage;
	const tokens = usage
		? `${usage.prompt_tokens ?? "—"}/${usage.completion_tokens ?? "—"}/${usage.total_tokens ?? "—"}`
		: "—";
	return `字數：要求 ${attempt.request_chars} ・ 回應 ${responseChars} ・ tokens（提示/完成/總計）：${tokens}`;
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
					{attempt.truncated ? (
						<Badge size="sm" variant="light" color="orange">
							已截斷
						</Badge>
					) : null}
				</Group>
				<Text size="xs" c="dimmed">
					{formatAttemptStats(attempt)}
				</Text>

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
// collapsed row never pulls its (large) bodies. The query key folds in
// log.started_at (the instance discriminator carried on the summary row)
// alongside log.id: the backend's id counter resets on restart, so id N today
// and id N from a previous process lifetime are two unrelated interactions
// that merely share a number. Keyed on id alone, react-query would treat them
// as the SAME query and could hand back a STALE cached detail -- a new
// record's row showing an old call's prompts/response. The fetch URL still
// only ever has the id (the backend has no other way to address a record);
// started_at is purely a client-side cache/staleness discriminator, doubly
// enforced below by comparing the fetched detail's own started_at against the
// row's before rendering any body.
function LogDetailPanel({ log, expanded }) {
	const { data, error, isError, isFetching, refetch } = useQuery({
		queryKey: ["llm-log", log.id, log.started_at],
		queryFn: () => apiGet(`/api/llm/logs/${log.id}`),
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
		// A 404 here is a WRONG-RESOURCE case for the shared client's generic
		// 找不到項目 mapping (that copy means "memory item", not this AI 互動
		// record): most likely this id was evicted past llm_log_max_entries,
		// or -- see the started_at race guarded below -- the ring was reset by
		// a backend restart before any later interaction reallocated this id.
		const message =
			error?.status === 404
				? "找不到這筆 AI 日誌：可能已被較新的紀錄擠出保留區，請重新整理清單"
				: (error?.message ?? "無法載入互動詳情");
		return (
			<Alert color="red" title="載入失敗">
				<Stack gap="sm" align="flex-start">
					<Text size="sm">{message}</Text>
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

	// Belt-and-suspenders for the same restart-id-reuse race the query key
	// above already guards: if a NEWER interaction has since reclaimed this
	// id (started_at no longer matches the row that was open when the fetch
	// started), the fetched bodies belong to a different call entirely --
	// show a neutral notice rather than render them as if they were this
	// row's.
	if (data.started_at !== log.started_at) {
		return (
			<Alert color="gray">
				<Text size="sm">這筆紀錄已被較新的紀錄取代，請重新整理清單</Text>
			</Alert>
		);
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
				互動的提示與回應，方便除錯。預設僅存於後端記憶體，重啟後清空；若後端設定了
				LLM_LOG_FILE，互動內容也會寫入該檔案。
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
				// Horizontal-scroll container: the header + rows below are fixed-
				// width (ROW_MIN_WIDTH, nowrap columns) and can exceed a narrow
				// viewport. overflowX:"auto" here -- with the min-width pinned on
				// the inner Stack -- keeps that overflow scoped to this box, so a
				// mobile viewport gets a local scrollbar rather than the page body
				// itself growing wider.
				<Box style={{ overflowX: "auto" }}>
					<Stack gap={0} miw={ROW_MIN_WIDTH}>
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
											log={log}
											expanded={openValue === String(log.id)}
										/>
									</Accordion.Panel>
								</Accordion.Item>
							))}
						</Accordion>
					</Stack>
				</Box>
			) : null}
		</Stack>
	);
}
