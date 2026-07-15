import {
	Alert,
	Anchor,
	Box,
	Button,
	Card,
	Center,
	Divider,
	Group,
	Loader,
	Modal,
	Select,
	Stack,
	Switch,
	Text,
	Textarea,
	Timeline,
	Title,
	Tooltip,
} from "@mantine/core";
import { useDisclosure } from "@mantine/hooks";
import { notifications } from "@mantine/notifications";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "@tanstack/react-router";
import { useEffect, useRef, useState } from "react";
import { Controller, useForm } from "react-hook-form";
import { apiDelete, apiGet, apiPatch, apiPost } from "../api/client.js";
import { DateText } from "../components/DateText.jsx";
import { EmptyState } from "../components/EmptyState.jsx";
import { ItemAiActions } from "../components/ItemAiActions.jsx";
import { StaleBadge } from "../components/StaleBadge.jsx";
import { StatusBadge } from "../components/StatusBadge.jsx";
import { TagList } from "../components/TagList.jsx";
import {
	STAGE_META,
	STAGE_OPTIONS,
	STATUS_OPTIONS,
} from "../constants/labels.js";
import { SECTION_GROUPS, SECTION_MAX_LENGTH } from "../constants/sections.js";
import { usePageTitle } from "../hooks/usePageTitle.js";
import { codePointLength } from "../utils/text.js";

// A section field counts as filled only when it holds non-whitespace text.
function isFilled(value) {
	return typeof value === "string" && value.trim() !== "";
}

// Renders the methodology section groups as pre-wrap text cards. Empty groups
// (and, when a group is shown, its empty fields) are hidden unless `showEmpty`
// is on, in which case they render a dimmed placeholder. Single-field groups
// use the group title as their only heading; multi-field groups add per-field
// sub-labels.
function SectionGroupsView({ item, showEmpty }) {
	const groups = SECTION_GROUPS.map((group) => {
		const fields = group.fields.map((field) => ({
			...field,
			value: item[field.key] ?? "",
		}));
		return {
			group,
			fields,
			hasContent: fields.some((field) => isFilled(field.value)),
		};
	}).filter((entry) => showEmpty || entry.hasContent);

	if (groups.length === 0) {
		return <EmptyState message="此項目尚無區段內容" align="flex-start" />;
	}

	return (
		<Stack gap="md">
			{groups.map(({ group, fields }) => {
				const single = group.fields.length === 1;
				return (
					<Card key={group.id} withBorder padding="md" radius="md">
						<Title order={4} mb="sm">
							{group.title}
						</Title>
						<Stack gap="sm">
							{fields.map((field) => {
								const filled = isFilled(field.value);
								if (!filled && !showEmpty) {
									return null;
								}
								return (
									<div key={field.key}>
										{single ? null : (
											<Text fw={600} size="sm" c="dimmed">
												{field.label}
											</Text>
										)}
										{filled ? (
											<Text style={{ whiteSpace: "pre-wrap" }}>
												{field.value}
											</Text>
										) : (
											<Text c="dimmed" fs="italic" size="sm">
												（空）
											</Text>
										)}
									</div>
								);
							})}
						</Stack>
					</Card>
				);
			})}
		</Stack>
	);
}

// Progress timeline (dates ascending, as returned by the backend) plus a small
// RHF note form. On submit it calls `onSubmitNote`, which fires the page's
// progress mutation; that mutation's onSuccess invalidates ['item', itemId],
// refetching the item so the new entry AND the server-bumped `updated`/is_stale
// (see add_progress in context_memory/routers/items.py) all land coherently --
// no optimistic append or manual refresh needed anymore.
//
// `isSubmitting` is the progress mutation's own isPending (button loading);
// `pending` is ItemDetailPage's page-wide mutation gate (true while this
// submit, a quick Select PATCH or an AI action is in flight) -- the gate is
// derived from the mutations' isPending, so no two of them can overlap.
function ProgressPanel({ progress, onSubmitNote, isSubmitting, pending }) {
	const { control, handleSubmit, reset } = useForm({
		defaultValues: { note: "" },
	});

	const submit = handleSubmit(({ note }) => {
		if (pending) {
			return;
		}
		// Clear the box only once the mutation actually succeeds (per-call
		// onSuccess), so a failed submit keeps the typed note.
		onSubmitNote(note.trim(), { onSuccess: () => reset({ note: "" }) });
	});

	return (
		<Stack gap="md">
			<Title order={3}>進度紀錄</Title>
			{progress.length === 0 ? (
				<Text c="dimmed" size="sm">
					尚無進度紀錄
				</Text>
			) : (
				<Timeline active={progress.length} bulletSize={16} lineWidth={2}>
					{progress.map((entry) => (
						<Timeline.Item
							key={entry.id}
							title={<DateText value={entry.date} size="sm" fw={600} />}
						>
							<Text size="sm" style={{ whiteSpace: "pre-wrap" }}>
								{entry.note}
							</Text>
						</Timeline.Item>
					))}
				</Timeline>
			)}

			<form onSubmit={submit}>
				<Stack gap="xs">
					<Controller
						name="note"
						control={control}
						rules={{
							required: "請輸入進度內容",
							// Trim first, validate the trimmed value -- submit sends
							// note.trim() (see submit above), so the length cap must
							// apply to what's actually posted.
							validate: (value) => {
								const trimmed = value.trim();
								if (trimmed === "") {
									return "請輸入進度內容";
								}
								if (codePointLength(trimmed) > SECTION_MAX_LENGTH) {
									return `內容不可超過 ${SECTION_MAX_LENGTH} 字`;
								}
								return true;
							},
						}}
						render={({ field, fieldState }) => (
							<Textarea
								{...field}
								label="新增進度"
								placeholder="記錄一筆進度..."
								autosize
								minRows={2}
								disabled={isSubmitting || pending}
								error={fieldState.error?.message}
							/>
						)}
					/>
					<Group justify="flex-end">
						<Button
							type="submit"
							size="sm"
							loading={isSubmitting}
							disabled={pending}
						>
							新增進度
						</Button>
					</Group>
				</Stack>
			</form>
		</Stack>
	);
}

export function ItemDetailPage() {
	const { itemId } = useParams({ strict: false });
	const navigate = useNavigate();

	const queryClient = useQueryClient();
	const [showEmpty, setShowEmpty] = useState(false);
	const [confirmOpen, confirm] = useDisclosure(false);
	// AI actions' combined busy flag. ItemAiActions calls this setter
	// SYNCHRONOUSLY -- onPendingChange(true) in the same click handler that
	// starts an AI mutation or the conflict-refresh, BEFORE the request
	// fires, and onPendingChange(false) in that mutation's onSettled -- so
	// this state and the mutation's own isPending land in the SAME React
	// batch/render as each other. An effect reacting to ItemAiActions' own
	// isPending (the earlier design) would close this gate one render late,
	// leaving a window where the Selects/progress/edit below are still
	// enabled after an AI action has already started. This is the "plus AI
	// actions' pending" half of the page-wide mutation gate derived below.
	const [aiPending, setAiPending] = useState(false);

	// If the user navigates away (e.g. browser back) while the delete mutation
	// below is still in flight, this page unmounts but the mutation still
	// resolves -- skip the success-path navigate in that case so it can't yank
	// the user back to the (now-deleted) item's list view from wherever they
	// already navigated to instead. The toast still shows (it's still true, and
	// no longer where anyone's looking makes it harmless). Set true in effect
	// setup, false in cleanup, so it resets correctly under StrictMode's
	// double-mount.
	const isMountedRef = useRef(true);
	useEffect(() => {
		isMountedRef.current = true;
		return () => {
			isMountedRef.current = false;
		};
	}, []);

	// Item fetch. Query key ['item', itemId] owns staleness now -- the old shared
	// monotonic reqRef guard (a manual re-check that dropped superseded GETs from
	// the mount load, retryLoad, the AI cards' refresh and the conflict banner's
	// refresh) is gone: the query cache drops superseded responses, and every
	// mutation below invalidates this key to refetch a fresh FULL item (with
	// progress) instead of merging partial responses by hand.
	const {
		data: item,
		error,
		isError,
		isFetching,
		refetch,
	} = useQuery({
		queryKey: ["item", itemId],
		queryFn: () => apiGet(`/api/items/${itemId}`),
	});

	// Reflect the loaded item's title in the document title, falling back while
	// loading or when the item is missing. A 404 wins even over a stale `item`
	// still sitting in cache (see the 404 render branch below for why), so
	// it's checked BEFORE `item` here too -- otherwise the tab would say
	// "找不到項目" in the body but still show the old title in the browser
	// tab/history.
	let pageTitle = "項目詳情";
	if (error?.status === 404) {
		pageTitle = "找不到項目";
	} else if (item) {
		pageTitle = item.title;
	}
	usePageTitle(pageTitle);

	// Invalidate the item AND the lists/review it can affect. Awaited by the
	// mutations' onSuccess so isPending (and thus the gate) stays closed until the
	// item refetch lands -- the react-query equivalent of the old
	// onMutationStart/onMutationEnd bracketing the POST *and* its refresh. Only
	// ['item', itemId] is active here (mounted), so only its refetch is awaited;
	// ['items']/['review'] are just marked stale and refetch when next visited.
	const invalidateItemAndLists = () =>
		Promise.all([
			queryClient.invalidateQueries({ queryKey: ["item", itemId] }),
			queryClient.invalidateQueries({ queryKey: ["items"] }),
			queryClient.invalidateQueries({ queryKey: ["review"] }),
		]);

	// PATCH a single scalar field (status or stage). The PATCH response is a full
	// MemoryItemRead WITHOUT progress; rather than merge progress back in by hand
	// (as the old code did), we invalidate and let the item refetch supply the
	// coherent full snapshot. Serialization still matters -- a slower response
	// applied after a faster, later one would be wrong -- so every mutating
	// control shares the gate below and disables itself while any mutation is
	// pending; a second mutation can never start before the first (and its
	// refetch) settle.
	const patchMutation = useMutation({
		mutationFn: ({ field, value }) =>
			apiPatch(`/api/items/${itemId}`, { [field]: value }),
		onSuccess: async (_data, variables) => {
			notifications.show({ color: "green", message: variables.successMessage });
			await invalidateItemAndLists();
		},
		onError: (mutationError) => {
			notifications.show({
				color: "red",
				title: "更新失敗",
				message: mutationError?.message ?? "無法更新項目",
			});
		},
	});

	const progressMutation = useMutation({
		mutationFn: (note) => apiPost(`/api/items/${itemId}/progress`, { note }),
		onSuccess: async () => {
			notifications.show({ color: "green", message: "已新增進度" });
			await invalidateItemAndLists();
		},
		onError: (mutationError) => {
			notifications.show({
				color: "red",
				title: "新增進度失敗",
				message: mutationError?.message ?? "無法新增進度",
			});
		},
	});

	// Delete is intentionally NOT part of the mutation gate: it navigates away on
	// success, so it can't race an in-place update the way a PATCH/POST can.
	const deleteMutation = useMutation({
		mutationFn: () => apiDelete(`/api/items/${itemId}`),
		onSuccess: () => {
			notifications.show({
				color: "green",
				title: "已刪除",
				message: `已刪除「${item.title}」`,
			});
			// Evict the exact entry rather than just invalidate it: invalidating
			// only marks it stale for the NEXT mount, but a background refetch
			// does not clear existing `data` in react-query -- so browser-back to
			// this now-deleted item would still hand back its last snapshot from
			// cache (while a 404 refetch runs silently underneath) instead of
			// starting from nothing. removeQueries drops the entry outright, so
			// back-navigation starts from `data: undefined` and genuinely
			// refetches -- landing on the 404 branch below, not a ghost of this
			// item.
			queryClient.removeQueries({ queryKey: ["item", itemId], exact: true });
			queryClient.invalidateQueries({ queryKey: ["items"] });
			queryClient.invalidateQueries({ queryKey: ["review"] });
			if (isMountedRef.current) {
				navigate({ to: "/items" });
			}
		},
		onError: (mutationError) => {
			confirm.close();
			notifications.show({
				color: "red",
				title: "刪除失敗",
				message: mutationError?.message ?? "無法刪除項目",
			});
		},
	});

	// Page-wide mutation gate derived from the mutations' isPending flags plus the
	// AI actions' reported pending -- ONE guard for every control that can mutate
	// this item, so at most one PATCH/POST is ever in flight at a time.
	// `pagePending` is just this page's own two mutations; it is threaded down to
	// ItemAiActions so its buttons disable while a quick Select or progress submit
	// runs, and ItemAiActions ORs in its own local AI-busy (no round-trip lag).
	// `mutationPending` adds aiPending back for this page's own controls.
	const pagePending = patchMutation.isPending || progressMutation.isPending;
	const mutationPending = pagePending || aiPending;

	// Quick status/stage change. The disabled Select already blocks a second
	// call, but re-check the gate defensively so the one-mutation-at-a-time
	// invariant holds regardless of how the change was triggered.
	const patchField = (field, value, successMessage) => {
		if (!item || mutationPending || value === item[field]) {
			return;
		}
		patchMutation.mutate({ field, value, successMessage });
	};

	// Loader covers the first load AND a 重試 after a failure: react-query keeps
	// status 'error' (not 'pending') while re-fetching after an error, so
	// `item === undefined && isFetching` is what re-shows the Loader on retry
	// (matching the old retryLoad), and a failed BACKGROUND refetch that still
	// has data falls through to render the item rather than blanking.
	if (item === undefined && isFetching) {
		return (
			<Center py="xl">
				<Loader />
			</Center>
		);
	}

	// Authoritative regardless of a stale `item` already in cache: a 404 is
	// the server saying this row is gone, and showing a stale snapshot would
	// invite doomed writes (a quick-Select PATCH, progress POST or AI action
	// against an id that no longer exists). This also covers a delete that
	// happened in another tab/process -- that tab's cache was never touched,
	// so `item` can still be defined here from an earlier successful fetch,
	// but the next refetch (focus, revisit, an unrelated invalidation)
	// landing a 404 must still win over it. The generic error branch below
	// keeps its `item === undefined` guard -- only a definitive "this id
	// doesn't exist" overrides stale data; a transient failure does not.
	if (error?.status === 404) {
		return (
			<Stack gap="md" align="flex-start">
				<Title order={2}>找不到項目</Title>
				<Text c="dimmed">此項目可能已被刪除，或連結有誤。</Text>
				<Button component={Link} to="/items" variant="light">
					返回記憶清單
				</Button>
			</Stack>
		);
	}

	if (isError && item === undefined) {
		return (
			<Alert color="red" title="載入失敗">
				<Stack gap="sm" align="flex-start">
					<Text size="sm">{error?.message ?? "無法載入項目"}</Text>
					<Button size="xs" onClick={() => refetch()}>
						重試
					</Button>
				</Stack>
			</Alert>
		);
	}

	const stageMeta = STAGE_META[item.stage] ?? { label: item.stage };
	const progress = item.progress ?? [];

	return (
		<Stack gap="lg">
			<Anchor component={Link} to="/items" size="sm">
				← 返回記憶清單
			</Anchor>

			<Stack gap="xs">
				<Group gap="sm" align="center">
					<Title order={2}>{item.title}</Title>
					<StatusBadge status={item.status} />
					<StaleBadge stale={item.is_stale} />
				</Group>
				<Group gap="sm">
					<Text size="sm" c="dimmed">
						階段：{stageMeta.label}
					</Text>
					<TagList tags={item.tags} />
				</Group>
				<Group gap="lg">
					<Group gap={4}>
						<Text size="sm" c="dimmed">
							建立
						</Text>
						<DateText value={item.created} size="sm" />
					</Group>
					<Group gap={4}>
						<Text size="sm" c="dimmed">
							更新
						</Text>
						<DateText value={item.updated} size="sm" />
					</Group>
				</Group>
			</Stack>

			<Group align="flex-end" gap="sm">
				<Select
					label="狀態"
					data={STATUS_OPTIONS}
					value={item.status}
					onChange={(value) =>
						value && patchField("status", value, "已更新狀態")
					}
					allowDeselect={false}
					disabled={mutationPending}
					w={150}
				/>
				<Select
					label="階段"
					data={STAGE_OPTIONS}
					value={item.stage}
					onChange={(value) =>
						value && patchField("stage", value, "已更新階段")
					}
					allowDeselect={false}
					disabled={mutationPending}
					w={130}
				/>
				{/* Gated by mutationPending: the edit page loads a snapshot of the
				item on mount, and its save is a plain PATCH with no version check
				(by design -- only the AI paths own the 409 machinery). Reaching
				it while an AI action, quick Select or progress submit is still in
				flight would let that stale snapshot's later save silently
				overwrite whatever this in-flight mutation just wrote, so the
				entry itself must be unreachable -- not just dimmed -- for the
				duration. */}
				<Tooltip label="處理中…" disabled={!mutationPending} withArrow>
					<Box display="inline-block">
						<Button
							component={Link}
							to="/items/$itemId/edit"
							params={{ itemId: String(item.id) }}
							variant="default"
							disabled={mutationPending}
						>
							編輯
						</Button>
					</Box>
				</Tooltip>
				<Button color="red" variant="light" onClick={confirm.open}>
					刪除
				</Button>
			</Group>

			<Divider />

			<Group justify="space-between" align="center">
				<Title order={3}>內容區段</Title>
				<Switch
					checked={showEmpty}
					onChange={(event) => setShowEmpty(event.currentTarget.checked)}
					label="顯示空白區段"
				/>
			</Group>
			<SectionGroupsView item={item} showEmpty={showEmpty} />

			<Divider />

			<Title order={3}>AI 協助</Title>
			<ItemAiActions
				item={item}
				pending={pagePending}
				onPendingChange={setAiPending}
				// The exact useParams value this page's own ['item', itemId] query
				// is keyed with -- NOT necessarily String(item.id). A non-canonical
				// URL like /items/001 keys this page's query as ['item','001']
				// while item.id is the canonical 1; ItemAiActions must invalidate/
				// remove the SAME key this page actually queries, or a refresh
				// silently no-ops against a cache entry that was never there.
				queryItemId={itemId}
			/>

			<Divider />

			<ProgressPanel
				progress={progress}
				onSubmitNote={(note, options) => progressMutation.mutate(note, options)}
				isSubmitting={progressMutation.isPending}
				pending={mutationPending}
			/>

			<Modal
				opened={confirmOpen}
				onClose={confirm.close}
				title="刪除項目"
				centered
				closeOnEscape={!deleteMutation.isPending}
				closeOnClickOutside={!deleteMutation.isPending}
				withCloseButton={!deleteMutation.isPending}
			>
				<Stack gap="md">
					<Text>確定要刪除「{item.title}」嗎？此動作無法復原。</Text>
					<Group justify="flex-end">
						<Button
							variant="default"
							onClick={confirm.close}
							disabled={deleteMutation.isPending}
						>
							取消
						</Button>
						<Button
							color="red"
							loading={deleteMutation.isPending}
							onClick={() => deleteMutation.mutate()}
						>
							刪除
						</Button>
					</Group>
				</Stack>
			</Modal>
		</Stack>
	);
}
