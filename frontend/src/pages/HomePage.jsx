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
import { Link } from "@tanstack/react-router";
import { useAtomValue, useSetAtom } from "jotai";
import { useCallback, useEffect, useRef, useState } from "react";
import { apiGet } from "../api/client.js";
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

// Small footer reusing the health ping and the shared LLM status atom, so the
// home page always shows whether the backend and AI are reachable.
function StatusFooter() {
	const llm = useAtomValue(llmStatusAtom);
	const loadLlmStatus = useSetAtom(loadLlmStatusAtom);
	const [connected, setConnected] = useState(null);

	useEffect(() => {
		let cancelled = false;
		apiGet("/api/health")
			.then((data) => {
				if (!cancelled) {
					setConnected(data?.status === "ok");
				}
			})
			.catch(() => {
				if (!cancelled) {
					setConnected(false);
				}
			});
		return () => {
			cancelled = true;
		};
	}, []);

	// `loading` must win over everything else: it covers both the first
	// request and a forced retry, and in the retry case `loaded`/`configured`
	// still hold their previous (possibly stale) values while `error` was
	// just cleared back to null for the new attempt -- so without checking
	// `loading` first, a retry of a failed check would render as a confident
	// green "已設定" while the probe is still hanging. A failed status
	// request must not assert either configured state (both would be a
	// guess) once it does settle, so it gets its own honest "can't tell"
	// badge with a way to retry the check.
	let llmColor = "gray";
	let llmLabel = "檢查中";
	if (llm.loading) {
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
					color={connected === null ? "gray" : connected ? "green" : "red"}
				>
					{connected === null ? "檢查中" : connected ? "連線正常" : "無法連線"}
				</Badge>
			</Group>
			<Group gap="xs">
				<Text size="xs" c="dimmed">
					AI
				</Text>
				<Badge size="sm" variant="light" color={llmColor}>
					{llmLabel}
				</Badge>
				{/* Same action, two triggers: a transport error (llm.error) and an
				honest "not configured" reading (llm.loaded && !llm.configured) --
				e.g. after the user fixes backend/.env and restarts the backend,
				this is the only way back to "已設定" short of a full page reload.
				The two states are mutually exclusive (the atom forces configured
				back to true on error), so one button covers both. */}
				{llm.error || (llm.loaded && !llm.configured) ? (
					<Button
						size="xs"
						variant="subtle"
						loading={llm.loading}
						onClick={() => loadLlmStatus({ force: true })}
					>
						{llm.error ? "重試" : "重新檢查"}
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
	const [state, setState] = useState({
		phase: "loading",
		data: null,
		error: null,
	});

	// Monotonic request id (same pattern as ItemsListPage/ItemDetailPage) so a
	// superseded response can't win -- e.g. StrictMode's double-mount fires
	// two /api/review requests, and without this guard the older one landing
	// after the newer one would silently overwrite its state.
	const requestId = useRef(0);
	const load = useCallback(() => {
		const id = ++requestId.current;
		setState((prev) => ({ ...prev, phase: "loading", error: null }));
		apiGet("/api/review")
			.then((data) => {
				if (id !== requestId.current) {
					return;
				}
				setState({ phase: "success", data, error: null });
			})
			.catch((error) => {
				if (id !== requestId.current) {
					return;
				}
				setState({ phase: "error", data: null, error });
			});
	}, []);

	useEffect(() => {
		load();
	}, [load]);

	const data = state.data;
	const staleCount =
		state.phase === "success"
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

			{state.phase === "success" ? (
				<StaleSummary staleCount={staleCount} />
			) : null}

			{state.phase === "loading" ? (
				<Center py="xl">
					<Loader />
				</Center>
			) : null}

			{state.phase === "error" ? (
				<Alert color="red" title="載入失敗">
					<Stack gap="sm" align="flex-start">
						<Text size="sm">{state.error?.message ?? "無法載入待辦總覽"}</Text>
						<Button size="xs" onClick={load}>
							重試
						</Button>
					</Stack>
				</Alert>
			) : null}

			{state.phase === "success" ? (
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
