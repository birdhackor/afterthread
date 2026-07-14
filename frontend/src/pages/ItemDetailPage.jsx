import {
	Alert,
	Anchor,
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
} from "@mantine/core";
import { useDisclosure } from "@mantine/hooks";
import { notifications } from "@mantine/notifications";
import { Link, useNavigate, useParams } from "@tanstack/react-router";
import { useCallback, useEffect, useRef, useState } from "react";
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
// RHF note form that appends a new entry optimistically from the POST response,
// then refetches (`onRefresh`) so the item's `updated`/is_stale -- bumped
// server-side by the same POST, see add_progress in app/routers/items.py --
// stay honest rather than frozen at their pre-post values.
//
// `pending` is the page-wide mutation gate from ItemDetailPage (true while
// this submit, a quick Select PATCH or an AI action is in flight elsewhere);
// `onMutationStart`/`onMutationEnd` bracket this submit's own POST+refresh so
// the Selects and AI actions are disabled for its duration too -- see
// ItemDetailPage's `mutationGate` comment for why no two of these mutations
// may ever overlap.
function ProgressPanel({
	itemId,
	progress,
	onAdded,
	onRefresh,
	pending,
	onMutationStart,
	onMutationEnd,
}) {
	const {
		control,
		handleSubmit,
		reset,
		formState: { isSubmitting },
	} = useForm({ defaultValues: { note: "" } });

	const submit = handleSubmit(async ({ note }) => {
		if (pending) {
			return;
		}
		const trimmed = note.trim();
		onMutationStart();
		try {
			const entry = await apiPost(`/api/items/${itemId}/progress`, {
				note: trimmed,
			});
			onAdded(entry);
			reset({ note: "" });
			notifications.show({ color: "green", message: "已新增進度" });
		} catch (error) {
			notifications.show({
				color: "red",
				title: "新增進度失敗",
				message: error?.message ?? "無法新增進度",
			});
			onMutationEnd();
			return;
		}
		// Separate try/catch from the add above (mirrors ItemAiActions'
		// onSuccess/onRefresh split): the add already succeeded, so a refresh
		// failure here must read as "refresh failed", never as "add failed".
		try {
			await onRefresh();
		} catch (_error) {
			notifications.show({
				color: "red",
				title: "重新載入失敗",
				message: "進度已新增，但重新載入失敗，請重新整理頁面",
			});
		} finally {
			onMutationEnd();
		}
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
							validate: (value) => {
								if (value.trim() === "") {
									return "請輸入進度內容";
								}
								if (codePointLength(value) > SECTION_MAX_LENGTH) {
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

	const [state, setState] = useState({
		phase: "loading",
		item: null,
		error: null,
	});
	const [showEmpty, setShowEmpty] = useState(false);
	// Page-wide mutation gate: ONE shared flag for every control that can
	// mutate this item -- both quick-update Selects, the progress-note
	// submit and both AI action submits (via ItemAiActions, see its
	// `pending`/`onMutationStart`/`onMutationEnd` props below) -- so at most
	// one PATCH/POST is ever in flight at a time. Every initiator disables
	// itself while `mutationPending` is true, so a second mutation can never
	// start before the first settles; see patchField for why that
	// serialization matters (a slower response's full-item snapshot would
	// otherwise silently overwrite whatever a faster, later one just wrote).
	// Delete is intentionally NOT gated by this -- it navigates away on
	// success, so it can't race a snapshot-merge the way an in-place update
	// can.
	const [mutationPending, mutationGate] = useDisclosure(false);
	const [deleting, setDeleting] = useState(false);
	const [confirmOpen, confirm] = useDisclosure(false);

	// Reflect the loaded item's title in the document title, falling back while
	// loading or when the item is missing.
	let pageTitle = "項目詳情";
	if (state.phase === "success" && state.item) {
		pageTitle = state.item.title;
	} else if (state.phase === "notfound") {
		pageTitle = "找不到項目";
	}
	usePageTitle(pageTitle);

	// Monotonic id so a slow initial fetch cannot clobber a newer one when the
	// route param changes.
	const reqRef = useRef(0);

	// Replace the loaded item (accepts a value or an updater). Used by the quick
	// status/stage controls and the progress form for in-place updates that must
	// not trigger a full reload.
	const setItem = useCallback((updater) => {
		setState((prev) => ({
			...prev,
			item: typeof updater === "function" ? updater(prev.item) : updater,
		}));
	}, []);

	// Refetch the full item (with progress). Throws on failure so callers that
	// refresh after a mutation surface the error themselves.
	const refresh = useCallback(async () => {
		const data = await apiGet(`/api/items/${itemId}`);
		setState({ phase: "success", item: data, error: null });
		return data;
	}, [itemId]);

	// 重試 from the error state: show the loader while refetching, and land
	// back on the error state with the new error on failure (never a silent
	// no-op).
	const retryLoad = () => {
		setState((prev) => ({ ...prev, phase: "loading", error: null }));
		refresh().catch((error) => {
			setState({ phase: "error", item: null, error });
		});
	};

	// Initial load / reload when the route param changes.
	useEffect(() => {
		const id = ++reqRef.current;
		setState({ phase: "loading", item: null, error: null });
		apiGet(`/api/items/${itemId}`)
			.then((data) => {
				if (id === reqRef.current) {
					setState({ phase: "success", item: data, error: null });
				}
			})
			.catch((error) => {
				if (id !== reqRef.current) {
					return;
				}
				setState({
					phase: error?.status === 404 ? "notfound" : "error",
					item: null,
					error,
				});
			});
	}, [itemId]);

	const item = state.item;

	// PATCH a single scalar field (status or stage). The PATCH response is a
	// full MemoryItemRead WITHOUT progress (so we merge the existing progress
	// back in) -- and, being a full snapshot, applying it while a fresher
	// state from another in-flight mutation (the other quick Select, the
	// progress form or an AI action) is still landing would silently
	// overwrite whatever that other mutation just wrote. Every mutating
	// control shares `mutationPending` and disables itself while any one of
	// them is in flight, so a second mutation can never start before the
	// first settles -- no merge logic needed because the race is prevented,
	// not resolved.
	const patchField = async (field, value, successMessage) => {
		if (!item || value === item[field]) {
			return;
		}
		mutationGate.open();
		try {
			const updated = await apiPatch(`/api/items/${item.id}`, {
				[field]: value,
			});
			setItem((prev) => ({ ...updated, progress: prev?.progress ?? [] }));
			notifications.show({ color: "green", message: successMessage });
		} catch (error) {
			notifications.show({
				color: "red",
				title: "更新失敗",
				message: error?.message ?? "無法更新項目",
			});
		} finally {
			mutationGate.close();
		}
	};

	const handleDelete = async () => {
		setDeleting(true);
		try {
			await apiDelete(`/api/items/${item.id}`);
			notifications.show({
				color: "green",
				title: "已刪除",
				message: `已刪除「${item.title}」`,
			});
			navigate({ to: "/items" });
		} catch (error) {
			setDeleting(false);
			confirm.close();
			notifications.show({
				color: "red",
				title: "刪除失敗",
				message: error?.message ?? "無法刪除項目",
			});
		}
	};

	if (state.phase === "loading") {
		return (
			<Center py="xl">
				<Loader />
			</Center>
		);
	}

	if (state.phase === "notfound") {
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

	if (state.phase === "error") {
		return (
			<Alert color="red" title="載入失敗">
				<Stack gap="sm" align="flex-start">
					<Text size="sm">{state.error?.message ?? "無法載入項目"}</Text>
					<Button size="xs" onClick={retryLoad}>
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
				<Button
					component={Link}
					to="/items/$itemId/edit"
					params={{ itemId: String(item.id) }}
					variant="default"
				>
					編輯
				</Button>
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
				onRefresh={refresh}
				pending={mutationPending}
				onMutationStart={mutationGate.open}
				onMutationEnd={mutationGate.close}
			/>

			<Divider />

			<ProgressPanel
				itemId={item.id}
				progress={progress}
				onAdded={(entry) =>
					setItem((prev) => ({
						...prev,
						progress: [...(prev.progress ?? []), entry],
					}))
				}
				onRefresh={refresh}
				pending={mutationPending}
				onMutationStart={mutationGate.open}
				onMutationEnd={mutationGate.close}
			/>

			<Modal
				opened={confirmOpen}
				onClose={confirm.close}
				title="刪除項目"
				centered
			>
				<Stack gap="md">
					<Text>確定要刪除「{item.title}」嗎？此動作無法復原。</Text>
					<Group justify="flex-end">
						<Button variant="default" onClick={confirm.close}>
							取消
						</Button>
						<Button color="red" loading={deleting} onClick={handleDelete}>
							刪除
						</Button>
					</Group>
				</Stack>
			</Modal>
		</Stack>
	);
}
