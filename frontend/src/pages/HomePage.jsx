import {
	Alert,
	Anchor,
	Badge,
	Button,
	Card,
	Center,
	Divider,
	Group,
	Loader,
	Stack,
	Text,
	Title,
} from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useAtomValue, useSetAtom } from "jotai";
import { apiGet } from "../api/client.js";
import { backendStatusAtom } from "../atoms/connectivity.js";
import { llmStatusAtom, loadLlmStatusAtom } from "../atoms/llm.js";
import { DateText } from "../components/DateText.jsx";
import { StaleBadge } from "../components/StaleBadge.jsx";
import { StatusBadge } from "../components/StatusBadge.jsx";
import { usePageTitle } from "../hooks/usePageTitle.js";

// The four review buckets in methodology order. Keys mirror the /api/review
// response; each bucket is returned oldest-first and is rendered as-is.
const REVIEW_GROUPS = [
	{ key: "needs_enrichment", title: "待補齊", emptyText: "沒有待補齊的項目" },
	{ key: "active", title: "進行中", emptyText: "沒有進行中的項目" },
	{ key: "waiting", title: "等待中", emptyText: "沒有等待中的項目" },
	{ key: "parked", title: "擱置", emptyText: "沒有擱置的項目" },
];

// A single review item: title link, status + stale badges, and the updated
// date pushed to the right.
function ReviewRow({ item }) {
	return (
		<Group justify="space-between" wrap="nowrap" gap="sm">
			<Group gap="xs" wrap="nowrap" style={{ minWidth: 0 }}>
				<Anchor
					component={Link}
					to={`/items/${item.id}`}
					fw={500}
					lineClamp={1}
				>
					{item.title}
				</Anchor>
				<StatusBadge status={item.status} />
				<StaleBadge stale={item.is_stale} />
			</Group>
			<DateText value={item.updated} size="sm" c="dimmed" />
		</Group>
	);
}

// One review bucket as a card: a header row carrying the count, then either the
// compact list or the bucket-specific empty text.
function ReviewSection({ title, emptyText, items }) {
	return (
		<Card withBorder padding="md" radius="md">
			<Group justify="space-between" align="center" mb="sm">
				<Title order={4}>{title}</Title>
				<Badge variant="light" color="gray">
					{items.length}
				</Badge>
			</Group>
			{items.length === 0 ? (
				<Text c="dimmed" size="sm">
					{emptyText}
				</Text>
			) : (
				<Stack gap="xs">
					{items.map((item) => (
						<ReviewRow key={item.id} item={item} />
					))}
				</Stack>
			)}
		</Card>
	);
}

// Stale-first summary: how many items across every bucket are stale.
function StaleSummary({ staleCount }) {
	if (staleCount === 0) {
		return (
			<Text size="sm" c="dimmed">
				陳舊優先：目前沒有陳舊項目，維持得很好。
			</Text>
		);
	}
	return (
		<Group gap="xs">
			<StaleBadge stale />
			<Text size="sm" fw={500}>
				陳舊優先：有 {staleCount} 個陳舊項目建議優先處理。
			</Text>
		</Group>
	);
}

// Small footer showing backend reachability and the LLM status. The backend
// badge reads the shared connectivity atom -- fed by api/client.js's
// passive reports of unambiguous request outcomes and by the authoritative
// /api/health probe RootLayout's useConnectivityMonitor fires (30s poll +
// focus/online/visibility triggers) -- instead of the one-shot local health
// ping it used to keep, which froze in whatever state mount time happened
// to see (dead-at-start never turned green after a recovery, alive-at-start
// never turned red after an outage).
function StatusFooter() {
	const llm = useAtomValue(llmStatusAtom);
	const loadLlmStatus = useSetAtom(loadLlmStatusAtom);
	const { reachable } = useAtomValue(backendStatusAtom);

	// AI badge precedence, highest first:
	// 1. Backend unreachable beats every LLM reading: while no HTTP response
	//    is coming back at all, any claim about the LLM would be a guess --
	//    llm.* still holds whatever the last successful probe saw, and
	//    atoms/llm.js deliberately preserves that memory across transport
	//    failures (an outage must not wash a known "not configured" back to
	//    the optimistic default). So the "can't tell" reading lives purely in
	//    this display layer; the atom itself is left untouched.
	// 2. `loading` must win over the remaining states: it covers both the
	//    first request and a forced retry, and in the retry case
	//    `loaded`/`configured` still hold their previous (possibly stale)
	//    values while `error` was just cleared back to null for the new
	//    attempt -- so without checking `loading` before them, a retry of a
	//    failed check would render as a confident green "已設定" while the
	//    probe is still hanging.
	// 3. A failed status request must not assert either configured state
	//    (both would be a guess) once it does settle, so it gets its own
	//    honest "can't tell" badge with a way to retry the check.
	let llmColor = "gray";
	let llmLabel = "檢查中";
	if (reachable === false) {
		llmColor = "gray";
		llmLabel = "無法確認";
	} else if (llm.loading) {
		llmColor = "gray";
		llmLabel = "確認中…";
	} else if (llm.error) {
		llmColor = "gray";
		llmLabel = "無法確認";
	} else if (llm.loaded) {
		llmColor = llm.configured ? "green" : "orange";
		if (llm.configured) {
			llmLabel = llm.model ? `已設定（${llm.model}）` : "已設定";
		} else {
			llmLabel = "未設定";
		}
	}

	return (
		<Group gap="lg">
			<Group gap="xs">
				<Text size="xs" c="dimmed">
					後端
				</Text>
				<Badge
					size="sm"
					variant="light"
					color={reachable === null ? "gray" : reachable ? "green" : "red"}
				>
					{reachable === null ? "檢查中" : reachable ? "連線正常" : "無法連線"}
				</Badge>
			</Group>
			<Group gap="xs">
				<Text size="xs" c="dimmed">
					AI
				</Text>
				<Badge size="sm" variant="light" color={llmColor}>
					{llmLabel}
				</Badge>
				{/* Same action, three triggers: backend unreachable, a transport
				error (llm.error), and an honest "not configured" reading
				(llm.loaded && !llm.configured) -- e.g. after the user fixes
				backend/.env and restarts the backend, this is the only way back
				to "已設定" short of a full page reload. The forced probe is a
				real /api/llm/status request, so its outcome also feeds the
				passive connectivity report: if the backend is actually back,
				the sub-5xx response flips the backend badge green immediately
				instead of waiting out the 30s poll (an ambiguous 5xx -- e.g.
				the dev proxy answering for a still-dead backend -- reports
				nothing, so the badge honestly stays red; see api/client.js).
				The trigger states can coexist -- a failed re-probe keeps the
				prior configured:false instead of resetting it (see
				loadLlmStatusAtom) -- so the label picks 重試 whenever something
				is wrong (unreachable or error) and only falls back to 重新檢查
				for the plain not-configured-and-no-error case. */}
				{reachable === false || llm.error || (llm.loaded && !llm.configured) ? (
					<Button
						size="xs"
						variant="subtle"
						loading={llm.loading}
						onClick={() => loadLlmStatus({ force: true })}
					>
						{reachable === false || llm.error ? "重試" : "重新檢查"}
					</Button>
				) : null}
			</Group>
		</Group>
	);
}

// Home page = the review dashboard. Fetches /api/review and renders the four
// buckets in methodology order (待補齊 / 進行中 / 等待中 / 擱置), a stale-first
// summary, quick actions, and a health/LLM footer.
export function HomePage() {
	usePageTitle("總覽");

	// Query key ['review'] owns staleness now: this single useQuery replaces the
	// old hand-rolled monotonic requestId guard (a superseded response, e.g.
	// from StrictMode's double-mount, is dropped by the query cache instead of a
	// manual id re-check), and 重試 below is just refetch().
	const { data, error, isError, isFetching, refetch } = useQuery({
		queryKey: ["review"],
		queryFn: () => apiGet("/api/review"),
	});

	// Loader condition covers both the first load and a 重試 after a failure:
	// react-query keeps status 'error' (not 'pending') while re-fetching after
	// an error, so `data === undefined && isFetching` -- rather than isPending --
	// is what re-shows the Loader on retry, matching the old phase machine.
	// A failed background refetch that still has prior data (data !== undefined)
	// falls through to the buckets instead of blanking to the error Alert. As
	// on ToolsPage, that retained-data state gets its own orange warning so the
	// previous snapshot cannot look freshly verified.
	const loading = data === undefined && isFetching;
	const showError = isError && data === undefined && !isFetching;
	const staleReview = isError && data !== undefined;

	const staleCount = data
		? REVIEW_GROUPS.reduce(
				(total, group) =>
					total +
					(data[group.key] ?? []).filter((item) => item.is_stale).length,
				0,
			)
		: 0;

	return (
		<Stack gap="lg">
			<Group justify="space-between" align="center">
				<Title order={2}>總覽</Title>
				<Group gap="sm">
					<Button component={Link} to="/capture">
						快速捕捉
					</Button>
					<Button component={Link} to="/items" variant="light">
						記憶清單
					</Button>
				</Group>
			</Group>

			{data ? <StaleSummary staleCount={staleCount} /> : null}

			{loading ? (
				<Center py="xl">
					<Loader />
				</Center>
			) : null}

			{showError ? (
				<Alert color="red" title="載入失敗">
					<Stack gap="sm" align="flex-start">
						<Text size="sm">{error?.message ?? "無法載入待辦總覽"}</Text>
						<Button size="xs" onClick={() => refetch()}>
							重試
						</Button>
					</Stack>
				</Alert>
			) : null}

			{staleReview ? (
				<Alert color="orange" title="無法更新回顧">
					<Text size="sm">
						{error?.message ?? "請稍後再試"}
						。以下內容是先前讀到的結果，可能已過期。
					</Text>
				</Alert>
			) : null}

			{data ? (
				<Stack gap="md">
					{REVIEW_GROUPS.map((group) => (
						<ReviewSection
							key={group.key}
							title={group.title}
							emptyText={group.emptyText}
							items={data[group.key] ?? []}
						/>
					))}
				</Stack>
			) : null}

			<Divider />
			<StatusFooter />
		</Stack>
	);
}
