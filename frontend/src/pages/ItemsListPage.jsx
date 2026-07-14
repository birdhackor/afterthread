import {
	Alert,
	Anchor,
	Badge,
	Button,
	Group,
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
import {
	STAGE_META,
	STAGE_OPTIONS,
	STATUS_META,
	STATUS_OPTIONS,
} from "../constants/labels.js";

// Local YYYY-MM-DD formatter (a shared DateText primitive replaces this later).
function formatDate(value) {
	if (!value) {
		return "";
	}
	const date = new Date(value);
	if (Number.isNaN(date.getTime())) {
		return "";
	}
	const year = date.getFullYear();
	const month = String(date.getMonth() + 1).padStart(2, "0");
	const day = String(date.getDate()).padStart(2, "0");
	return `${year}-${month}-${day}`;
}

// A single memory item rendered as a table row.
function ItemRow({ item }) {
	const statusMeta = STATUS_META[item.status] ?? {
		label: item.status,
		color: "gray",
	};
	const stageMeta = STAGE_META[item.stage] ?? { label: item.stage };
	return (
		<Table.Tr>
			<Table.Td>
				<Group gap="xs" wrap="nowrap">
					<Anchor component={Link} to={`/items/${item.id}`} fw={500}>
						{item.title}
					</Anchor>
					{item.is_stale ? (
						<Badge color="yellow" variant="light" size="sm">
							陳舊
						</Badge>
					) : null}
				</Group>
			</Table.Td>
			<Table.Td>
				<Badge color={statusMeta.color} variant="light">
					{statusMeta.label}
				</Badge>
			</Table.Td>
			<Table.Td>
				<Text size="sm">{stageMeta.label}</Text>
			</Table.Td>
			<Table.Td>
				<Group gap={4}>
					{(item.tags ?? []).map((tag) => (
						<Badge key={tag} variant="outline" color="gray" size="sm">
							{tag}
						</Badge>
					))}
				</Group>
			</Table.Td>
			<Table.Td>
				<Text size="sm" c="dimmed">
					{formatDate(item.updated)}
				</Text>
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
	const load = useCallback(() => {
		const id = ++requestId.current;
		setState((prev) => ({ ...prev, phase: "loading", error: null }));

		const query = buildQuery({
			status,
			stage,
			tag: debouncedTag,
			q: debouncedQ,
			limit: DEFAULT_LIMIT,
			offset,
		});

		apiGet(`/api/items${query}`)
			.then((data) => {
				if (id !== requestId.current) {
					return;
				}
				setState({
					phase: "success",
					items: data?.items ?? [],
					total: data?.total ?? 0,
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
	}, [status, stage, debouncedTag, debouncedQ, offset]);

	useEffect(() => {
		load();
	}, [load]);

	const hasFilters = Boolean(status || stage || debouncedTag || debouncedQ);
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
			<Stack gap="sm" align="flex-start" py="xl">
				<Text c="dimmed">找不到符合條件的項目</Text>
				<Button variant="light" size="xs" onClick={() => resetFilters()}>
					清除篩選
				</Button>
			</Stack>
		) : (
			<Stack gap="sm" align="center" py="xl">
				<Text c="dimmed">尚無記憶項目</Text>
				<Group gap="sm">
					<Anchor component={Link} to="/capture">
						快速捕捉
					</Anchor>
					<Anchor component={Link} to="/items/new">
						新增項目
					</Anchor>
				</Group>
			</Stack>
		);
	} else {
		body = (
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
					placeholder="標題或內容關鍵字"
					value={q}
					onChange={changeQ}
					w={200}
				/>
				<Button variant="default" onClick={() => resetFilters()}>
					清除
				</Button>
			</Group>

			{body}

			{state.total > DEFAULT_LIMIT ? (
				<Group justify="space-between" align="center">
					<Text size="sm" c="dimmed">
						共 {state.total} 筆
					</Text>
					<Pagination total={totalPages} value={page} onChange={setPage} />
				</Group>
			) : null}
		</Stack>
	);
}
