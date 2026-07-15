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
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useAtom, useSetAtom } from "jotai";
import { useEffect, useRef } from "react";
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

// Whether `data` represents a page overshoot: a fetch that genuinely
// completed for the CURRENT `page` (unlike react-query's own kept-previous
// placeholder, which callers must exclude separately -- see needsClamp in
// ItemsListPage below) but came back with zero rows while `total` says rows
// exist elsewhere, i.e. `page` is past the actual last page for that total.
// Shared by the clamp effect (decides whether to snap `page` back) AND the
// lastGoodRef guard (decides whether `data` is safe to remember as the last
// DISPLAYABLE result) -- both are plain effects that run every render with no
// ordering guarantee between them, so a transitional overshoot response must
// be recognized identically by both, or lastGoodRef could store it as "good"
// moments before (or after) the clamp effect reacts to that SAME response by
// moving away from it. Without a shared predicate, a subsequent failed
// request for the corrected page would then fall back to this empty page
// instead of the last real one.
function needsPageClamp(data, page) {
	if (data === undefined) {
		return false;
	}
	const total = data.total ?? 0;
	const itemsEmpty = (data.items?.length ?? 0) === 0;
	const lastPage = Math.max(1, Math.ceil(total / DEFAULT_LIMIT));
	return itemsEmpty && total > 0 && page > 1 && lastPage !== page;
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

	// Trimmed once here, shared by the query key, the request URL and hasFilters
	// -- backend tags are stored trimmed and matched exactly, and q's whitespace
	// would otherwise become part of the LIKE pattern, so whitespace-only
	// input must mean "no filter" (buildQuery drops the resulting "", and
	// hasFilters must agree or an empty DB would show 找不到符合條件的項目
	// instead of 尚無記憶項目). The atoms/debounced values themselves stay
	// untrimmed (see changeTag/changeQ) so the input's caret/typing is never
	// fought -- only this derived pair is trimmed.
	const trimmedTag = debouncedTag.trim();
	const trimmedQ = debouncedQ.trim();

	// Normalized filter object feeding both the query key and the request URL.
	// It IS the query key's payload, so any change (a debounced tag/q, a filter,
	// a page) refetches -- and identical values reuse the cache. This replaces
	// the old monotonic requestId guard: a superseded response is dropped by the
	// query cache instead of a manual id re-check.
	const filters = {
		status,
		stage,
		tag: trimmedTag,
		q: trimmedQ,
		limit: DEFAULT_LIMIT,
		offset,
	};

	const { data, error, isError, isFetching, isPlaceholderData, refetch } =
		useQuery({
			queryKey: ["items", filters],
			queryFn: () => apiGet(`/api/items${buildQuery(filters)}`),
			// keepPreviousData keeps the prior page's rows on screen (under the
			// LoadingOverlay below) while a filter/page change refetches, instead of
			// flashing empty -- the react-query equivalent of the old "keep
			// state.items until the new response lands" behavior.
			placeholderData: keepPreviousData,
		});

	// keepPreviousData covers the pending (successful) transition, but on an
	// ERRORED reload react-query drops back to data === undefined, which would
	// blank the table. The original instead kept the previous rows visible with
	// a "重新載入失敗" banner, so remember the last delivered page here and fall
	// back to it when a reload fails. Guarded by needsPageClamp (see its doc
	// comment above) so a transitional overshoot response -- one the clamp
	// effect below is about to react to by moving `page` away from it -- is
	// never the thing this falls back to.
	const lastGoodRef = useRef(null);
	useEffect(() => {
		if (data !== undefined && !needsPageClamp(data, page)) {
			lastGoodRef.current = data;
		}
	}, [data, page]);
	const shown = data ?? lastGoodRef.current;
	const items = shown?.items ?? [];
	const total = shown?.total ?? 0;

	// The persisted page can outlive its data (e.g. the last item on it was
	// deleted elsewhere, or a filter shrank the result set). When a FRESH,
	// non-placeholder response for a page > 1 comes back empty while total > 0,
	// snap back to the last valid page; changing `page` moves `offset`, which
	// changes the query key and refetches the correct page. This is the old
	// in-response clamp re-expressed as an effect reacting to the query result.
	// !isPlaceholderData is layered on here rather than folded into
	// needsPageClamp itself: a kept-previous placeholder is still a genuinely
	// displayable result (the lastGoodRef guard above does NOT exclude
	// placeholders), it just isn't evidence about the page currently being
	// requested, so it must never drive the clamp decision. clampTarget is
	// computed separately (needsPageClamp only answers yes/no) since setPage
	// needs an actual target, not just a boolean.
	const freshTotal = data?.total ?? 0;
	const clampTarget = Math.max(1, Math.ceil(freshTotal / DEFAULT_LIMIT));
	const needsClamp = !isPlaceholderData && needsPageClamp(data, page);
	useEffect(() => {
		if (needsClamp) {
			setPage(clampTarget);
		}
	}, [needsClamp, clampTarget, setPage]);

	// The old load().catch fired a red toast on every failed fetch (on top of
	// the inline alert); mirror that off the query error. `error` keeps a stable
	// identity until the next fetch, so this fires once per distinct failure
	// rather than on every render.
	useEffect(() => {
		if (isError && error) {
			notifications.show({
				color: "red",
				title: "載入失敗",
				message: error?.message ?? "無法載入記憶清單",
			});
		}
	}, [isError, error]);

	const hasFilters = Boolean(status || stage || trimmedTag || trimmedQ);
	const totalPages = Math.max(1, Math.ceil(total / DEFAULT_LIMIT));

	const skeletonBody = (
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

	let body;
	if (shown === null && isFetching) {
		// First load: nothing delivered yet.
		body = skeletonBody;
	} else if (isError && items.length === 0) {
		// Load failed with nothing to show.
		body = (
			<Alert color="red" title="載入失敗">
				<Stack gap="sm" align="flex-start">
					<Text size="sm">{error?.message ?? "無法載入記憶清單"}</Text>
					<Button size="xs" onClick={() => refetch()}>
						重試
					</Button>
				</Stack>
			</Alert>
		);
	} else if (needsClamp) {
		// Clamping to a valid page (see the effect above); show the skeleton
		// rather than let the empty page flash before the clamp refetch lands.
		body = skeletonBody;
	} else if (items.length === 0 && !isFetching) {
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
		// Rows present (fresh, or kept-previous during a reload). A reload in
		// flight is otherwise silent, so this overlay is the only signal that the
		// visible rows are stale and a new query is running.
		body = (
			<Box pos="relative">
				<LoadingOverlay visible={isFetching} />
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
							{items.map((item) => (
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

			{isError && items.length > 0 ? (
				<Alert color="red" title="重新載入失敗" variant="light">
					<Stack gap="sm" align="flex-start">
						<Text size="sm">
							{error?.message ?? "無法重新載入記憶清單"}
							，顯示的是先前的結果
						</Text>
						<Button size="xs" onClick={() => refetch()}>
							重試
						</Button>
					</Stack>
				</Alert>
			) : null}

			{body}

			{total > 0 ? (
				<Group justify="space-between" align="center">
					<Text size="sm" c="dimmed">
						共 {total} 筆{isError ? "（顯示先前結果）" : ""}
					</Text>
					<Pagination total={totalPages} value={page} onChange={setPage} />
				</Group>
			) : null}
		</Stack>
	);
}
