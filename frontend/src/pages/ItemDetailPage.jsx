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
import { StaleBadge } from "../components/StaleBadge.jsx";
import { StatusBadge } from "../components/StatusBadge.jsx";
import { TagList } from "../components/TagList.jsx";
import {
	STAGE_META,
	STAGE_OPTIONS,
	STATUS_OPTIONS,
} from "../constants/labels.js";
import { SECTION_GROUPS, SECTION_MAX_LENGTH } from "../constants/sections.js";

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
// RHF note form that appends a new entry optimistically from the POST response.
function ProgressPanel({ itemId, progress, onAdded }) {
	const {
		control,
		handleSubmit,
		reset,
		formState: { isSubmitting },
	} = useForm({ defaultValues: { note: "" } });

	const submit = handleSubmit(async ({ note }) => {
		const trimmed = note.trim();
		if (!trimmed) {
			return;
		}
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
							maxLength: {
								value: SECTION_MAX_LENGTH,
								message: `內容不可超過 ${SECTION_MAX_LENGTH} 字`,
							},
						}}
						render={({ field, fieldState }) => (
							<Textarea
								{...field}
								label="新增進度"
								placeholder="記錄一筆進度..."
								autosize
								minRows={2}
								maxLength={SECTION_MAX_LENGTH}
								error={fieldState.error?.message}
							/>
						)}
					/>
					<Group justify="flex-end">
						<Button type="submit" size="sm" loading={isSubmitting}>
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
	const [statusSaving, setStatusSaving] = useState(false);
	const [stageSaving, setStageSaving] = useState(false);
	const [deleting, setDeleting] = useState(false);
	const [confirmOpen, confirm] = useDisclosure(false);

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
	// MemoryItemRead WITHOUT progress, so we merge the existing progress back in.
	const patchField = async (field, value, setSaving, successMessage) => {
		if (!item || value === item[field]) {
			return;
		}
		setSaving(true);
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
			setSaving(false);
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
					<Button size="xs" onClick={() => refresh().catch(() => {})}>
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
						value && patchField("status", value, setStatusSaving, "已更新狀態")
					}
					allowDeselect={false}
					disabled={statusSaving}
					w={150}
				/>
				<Select
					label="階段"
					data={STAGE_OPTIONS}
					value={item.stage}
					onChange={(value) =>
						value && patchField("stage", value, setStageSaving, "已更新階段")
					}
					allowDeselect={false}
					disabled={stageSaving}
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

			<ProgressPanel
				itemId={item.id}
				progress={progress}
				onAdded={(entry) =>
					setItem((prev) => ({
						...prev,
						progress: [...(prev.progress ?? []), entry],
					}))
				}
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
