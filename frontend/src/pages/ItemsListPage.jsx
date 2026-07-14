import {
	Alert,
	Anchor,
	Box,
	Button,
	Group,
	LoadingOverlay,
	Pagination,
	Select,
	Skeleton,
	Stack,
	Table,
	Text,
	TextInput,
	Title,
} from "@mantine/core";
import { useDebouncedValue } from "@mantine/hooks";
import { notifications } from "@mantine/notifications";
import { Link } from "@tanstack/react-router";
import { useAtom, useSetAtom } from "jotai";
import { useCallback, useEffect, useRef, useState } from "react";
import { apiGet, buildQuery } from "../api/client.js";
import {
	DEFAULT_LIMIT,
	pageAtom,
	qFilterAtom,
	resetFiltersAtom,
	stageFilterAtom,
	statusFilterAtom,
	tagFilterAtom,
} from "../atoms/filters.js";
import { DateText } from "../components/DateText.jsx";
import { EmptyState } from "../components/EmptyState.jsx";
import { StaleBadge } from "../components/StaleBadge.jsx";
import { StatusBadge } from "../components/StatusBadge.jsx";
import { TagList } from "../components/TagList.jsx";
import {
	STAGE_META,
	STAGE_OPTIONS,
	STATUS_OPTIONS,
} from "../constants/labels.js";
import { usePageTitle } from "../hooks/usePageTitle.js";

// A single memory item rendered as a table row.
function ItemRow({ item }) {
	const stageMeta = STAGE_META[item.stage] ?? { label: item.stage };
	return (
		<Table.Tr>
			<Table.Td>
				<Group gap="xs" wrap="nowrap">
					<Anchor component={Link} to={`/items/${item.id}`} fw={500}>
						{item.title}
					</Anchor>
					<StaleBadge stale={item.is_stale} />
				</Group>
			</Table.Td>
			<Table.Td>
				<StatusBadge status={item.status} />
			</Table.Td>
			<Table.Td>
				<Text size="sm">{stageMeta.label}</Text>
			</Table.Td>
			<Table.Td>
				<TagList tags={item.tags} />
			</Table.Td>
			<Table.Td>
				<DateText value={item.updated} size="sm" c="dimmed" />
			</Table.Td>
		</Table.Tr>
	);
}

// Stable placeholder keys so skeleton rows/cells avoid array-index keys.
const SKELETON_ROW_KEYS = ["r1", "r2", "r3", "r4", "r5", "r6"];
const SKELETON_CELL_KEYS = ["title", "status", "stage", "tags", "updated"];

// Skeleton placeholder rows shown during the initial load.
function SkeletonRows() {
	return SKELETON_ROW_KEYS.map((rowKey) => (
		<Table.Tr key={rowKey}>
			{SKELETON_CELL_KEYS.map((cellKey) => (
				<Table.Td key={`${rowKey}-${cellKey}`}>
					<Skeleton height={18} />
				</Table.Td>
			))}
		</Table.Tr>
	));
}

export function ItemsListPage() {
	usePageTitle("記憶清單");
	const [status, setStatus] = useAtom(statusFilterAtom);
	const [stage, setStage] = useAtom(stageFilterAtom);
	const [tag, setTag] = useAtom(tagFilterAtom);
	const [q, setQ] = useAtom(qFilterAtom);
	const [page, setPage] = useAtom(pageAtom);
	const resetFilters = useSetAtom(resetFiltersAtom);

	// Only the free-text filters are debounced before hitting the API.
	const [debouncedTag] = useDebouncedValue(tag, 300);
	const [debouncedQ] = useDebouncedValue(q, 300);

	const [state, setState] = useState({
		phase: "loading",
		items: [],
		total: 0,
		error: null,
	});

	const offset = (page - 1) * DEFAULT_LIMIT;

	// Changing any filter jumps back to page 1; routing every control through
	// these keeps the reset explicit (no run-on-change effect) while leaving the
	// page position untouched on plain navigation.
	const changeStatus = (value) => {
		setStatus(value ?? "");
		setPage(1);
	};
	const changeStage = (value) => {
		setStage(value ?? "");
		setPage(1);
	};
	const changeTag = (event) => {
		setTag(event.currentTarget.value);
		setPage(1);
	};
	const changeQ = (event) => {
		setQ(event.currentTarget.value);
		setPage(1);
	};

	// Fetch the current page. A monotonic request id drops stale responses when
	// filters change faster than the network resolves.
	const requestId = useRef(0);

	// Trimmed once here, shared by both the query below and hasFilters --
	// backend tags are stored trimmed and matched exactly, and q's whitespace
	// would otherwise become part of the LIKE pattern, so whitespace-only
	// input must mean "no filter" (buildQuery drops the resulting "", and
	// hasFilters must agree or an empty DB would show 找不到符合條件的項目
	// instead of 尚無記憶項目). The atoms/debounced values themselves stay
	// untrimmed (see changeTag/changeQ) so the input's caret/typing is never
	// fought -- only this derived pair is trimmed.
	const trimmedTag = debouncedTag.trim();
	const trimmedQ = debouncedQ.trim();

	const load = useCallback(() => {
		const id = ++requestId.current;
		setState((prev) => ({ ...prev, phase: "loading", error: null }));

		const query = buildQuery({
			status,
			stage,
			tag: trimmedTag,
			q: trimmedQ,
			limit: DEFAULT_LIMIT,
			offset,
		});

		apiGet(`/api/items${query}`)
			.then((data) => {
				if (id !== requestId.current) {
					return;
				}
				const items = data?.items ?? [];
				const total = data?.total ?? 0;
				// The persisted page can outlive its data (e.g. the last item on
				// it was deleted elsewhere). Clamp back to the last valid page and
				// let that refetch supply real state, instead of rendering the
				// "no data" empty state for this transient, page-that-no-longer-
				// exists response. Only do that when the clamp target actually
				// differs from the current page: setPage() with the same value is
				// a no-op that would never retrigger `load` (its deps, including
				// `offset`, would all stay unchanged), sticking the UI in
				// "loading" forever. When the target is already the current page,
				// this response IS the coherent state for it, so fall through and
				// render it honestly instead.
				if (items.length === 0 && total > 0 && page > 1) {
					const lastPage = Math.max(1, Math.ceil(total / DEFAULT_LIMIT));
					if (lastPage !== page) {
						setPage(lastPage);
						return;
					}
				}
				setState({
					phase: "success",
					items,
					total,
					error: null,
				});
			})
			.catch((error) => {
				if (id !== requestId.current) {
					return;
				}
				setState((prev) => ({ ...prev, phase: "error", error }));
				notifications.show({
					color: "red",
					title: "載入失敗",
					message: error?.message ?? "無法載入記憶清單",
				});
			});
	}, [status, stage, trimmedTag, trimmedQ, offset, page, setPage]);

	useEffect(() => {
		load();
	}, [load]);

	const hasFilters = Boolean(status || stage || trimmedTag || trimmedQ);
	const totalPages = Math.max(1, Math.ceil(state.total / DEFAULT_LIMIT));

	let body;
	if (state.phase === "loading" && state.items.length === 0) {
		body = (
			<Table striped highlightOnHover verticalSpacing="sm">
				<Table.Thead>
					<Table.Tr>
						<Table.Th>標題</Table.Th>
						<Table.Th>狀態</Table.Th>
						<Table.Th>階段</Table.Th>
						<Table.Th>標籤</Table.Th>
						<Table.Th>更新</Table.Th>
					</Table.Tr>
				</Table.Thead>
				<Table.Tbody>
					<SkeletonRows />
				</Table.Tbody>
			</Table>
		);
	} else if (state.phase === "error" && state.items.length === 0) {
		body = (
			<Alert color="red" title="載入失敗">
				<Stack gap="sm" align="flex-start">
					<Text size="sm">{state.error?.message ?? "無法載入記憶清單"}</Text>
					<Button size="xs" onClick={load}>
						重試
					</Button>
				</Stack>
			</Alert>
		);
	} else if (state.items.length === 0) {
		body = hasFilters ? (
			<EmptyState message="找不到符合條件的項目" align="flex-start">
				<Button variant="light" size="xs" onClick={() => resetFilters()}>
					清除篩選
				</Button>
			</EmptyState>
		) : (
			<EmptyState message="尚無記憶項目">
				<Group gap="sm">
					<Anchor component={Link} to="/capture">
						快速捕捉
					</Anchor>
					<Anchor component={Link} to="/items/new">
						新增項目
					</Anchor>
				</Group>
			</EmptyState>
		);
	} else {
		// A page/filter reload keeps the previous rows on screen (only the
		// first load shows the skeleton above) so the table doesn't flash
		// empty, but that means a reload in flight is otherwise silent --
		// this overlay is the only signal that the visible rows are stale
		// and a new query is running.
		body = (
			<Box pos="relative">
				<LoadingOverlay
					visible={state.phase === "loading" && state.items.length > 0}
				/>
				<Table.ScrollContainer minWidth={640}>
					<Table striped highlightOnHover verticalSpacing="sm">
						<Table.Thead>
							<Table.Tr>
								<Table.Th>標題</Table.Th>
								<Table.Th>狀態</Table.Th>
								<Table.Th>階段</Table.Th>
								<Table.Th>標籤</Table.Th>
								<Table.Th>更新</Table.Th>
							</Table.Tr>
						</Table.Thead>
						<Table.Tbody>
							{state.items.map((item) => (
								<ItemRow key={item.id} item={item} />
							))}
						</Table.Tbody>
					</Table>
				</Table.ScrollContainer>
			</Box>
		);
	}

	return (
		<Stack gap="md">
			<Group justify="space-between" align="center">
				<Title order={2}>記憶清單</Title>
				<Button component={Link} to="/items/new" variant="light">
					新增項目
				</Button>
			</Group>

			<Group align="flex-end" gap="sm">
				<Select
					label="狀態"
					placeholder="全部"
					data={STATUS_OPTIONS}
					value={status || null}
					onChange={changeStatus}
					clearable
					w={140}
				/>
				<Select
					label="階段"
					placeholder="全部"
					data={STAGE_OPTIONS}
					value={stage || null}
					onChange={changeStage}
					clearable
					w={120}
				/>
				<TextInput
					label="標籤"
					placeholder="以標籤篩選"
					value={tag}
					onChange={changeTag}
					w={160}
				/>
				<TextInput
					label="搜尋"
					placeholder="搜尋標題、快照或恢復關鍵字"
					value={q}
					onChange={changeQ}
					w={200}
				/>
				<Button variant="default" onClick={() => resetFilters()}>
					清除
				</Button>
			</Group>

			{state.phase === "error" && state.items.length > 0 ? (
				<Alert color="red" title="重新載入失敗" variant="light">
					<Stack gap="sm" align="flex-start">
						<Text size="sm">
							{state.error?.message ?? "無法重新載入記憶清單"}
							，顯示的是先前的結果
						</Text>
						<Button size="xs" onClick={load}>
							重試
						</Button>
					</Stack>
				</Alert>
			) : null}

			{body}

			{state.total > 0 ? (
				<Group justify="space-between" align="center">
					<Text size="sm" c="dimmed">
						共 {state.total} 筆
						{state.phase === "error" ? "（顯示先前結果）" : ""}
					</Text>
					<Pagination total={totalPages} value={page} onChange={setPage} />
				</Group>
			) : null}
		</Stack>
	);
}
