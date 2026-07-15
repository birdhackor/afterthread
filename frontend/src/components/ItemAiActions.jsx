import {
	Alert,
	Box,
	Button,
	Card,
	Checkbox,
	Group,
	Stack,
	Text,
	Textarea,
	Title,
	Tooltip,
} from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useAtomValue, useSetAtom } from "jotai";
import { useEffect, useState } from "react";
import { Controller, useForm } from "react-hook-form";
import { apiPost } from "../api/client.js";
import {
	llmStatusAtom,
	loadLlmStatusAtom,
	markLlmUnconfiguredAtom,
} from "../atoms/llm.js";
import { LLM_NOT_CONFIGURED_NOTICE } from "../constants/labels.js";
import { SECTION_MAX_LENGTH } from "../constants/sections.js";
import { codePointLength } from "../utils/text.js";

// One AI action card: a textarea + submit button whose submit fires the
// `mutation` passed in from ItemAiActions. Handles the long-request loading
// state (double-submit is blocked while the mutation is pending), disables +
// explains the button when the LLM is not configured, and lets ItemAiActions'
// shared onError surface the mapped error. A 409 additionally offers an inline
// 重新整理 action -- the conflict banner is derived from the mutation's own
// error (mutation.error?.status === 409), which a fresh submit auto-clears
// (react-query resets the error when the next mutate starts) and the conflict
// refresh clears explicitly via mutation.reset().
//
// `pending` is ItemDetailPage's page-own mutation gate (a quick Select PATCH or
// the progress submit in flight); `aiBusy` is ItemAiActions' combined AI-busy
// (either AI card's mutation OR the conflict-refresh) -- ORing both here
// disables the button whenever anything else that mutates this item is running,
// with no round-trip lag, matching the old shared-gate behavior. The textarea
// stays enabled unless THIS card's own action is in flight (`busy`), so the
// user can keep typing while an unrelated control settles.
function AiActionCard({
	title,
	description,
	fieldName,
	fieldLabel,
	placeholder,
	submitLabel,
	configured,
	pending,
	aiBusy,
	mutation,
	refreshing,
	onRefreshConflict,
	children,
}) {
	const { control, handleSubmit, reset } = useForm({
		defaultValues: { [fieldName]: "" },
	});
	const busy = mutation.isPending;
	const conflict = mutation.isError && mutation.error?.status === 409;

	const submit = handleSubmit((values) => {
		if (!configured || busy || pending || aiBusy) {
			return;
		}
		const value = values[fieldName].trim();
		// Clear the box only once the mutation actually succeeds (per-call
		// onSuccess, which runs after the card's shared onSuccess in
		// ItemAiActions), so a failed action keeps the typed text for a retry.
		mutation.mutate(value, { onSuccess: () => reset({ [fieldName]: "" }) });
	});

	return (
		<Card withBorder padding="md" radius="md">
			<Stack gap="sm">
				<div>
					<Title order={4}>{title}</Title>
					<Text size="sm" c="dimmed">
						{description}
					</Text>
				</div>

				{conflict ? (
					<Alert color="orange" title="項目已被修改">
						<Stack gap="xs" align="flex-start">
							<Text size="sm">項目在 AI 處理期間被修改，請重新整理後再試</Text>
							<Button
								size="xs"
								variant="light"
								loading={refreshing}
								disabled={pending || aiBusy}
								onClick={onRefreshConflict}
							>
								重新整理
							</Button>
						</Stack>
					</Alert>
				) : null}

				<form onSubmit={submit}>
					<Stack gap="xs">
						<Controller
							name={fieldName}
							control={control}
							rules={{
								required: "請輸入內容",
								// Trim first, validate the trimmed value -- submit sends
								// values[fieldName].trim() (see submit above), so the length
								// cap must apply to what's actually posted.
								validate: (value) => {
									const trimmed = value.trim();
									if (trimmed === "") {
										return "請輸入內容";
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
									label={fieldLabel}
									placeholder={placeholder}
									autosize
									minRows={3}
									disabled={busy}
									error={fieldState.error?.message}
								/>
							)}
						/>
						<Group justify="flex-end">
							<Tooltip
								label={configured ? "處理中…" : LLM_NOT_CONFIGURED_NOTICE}
								disabled={configured && !pending && !aiBusy}
								multiline
								w={260}
								withArrow
							>
								<Box display="inline-block">
									<Button
										type="submit"
										loading={busy}
										disabled={!configured || busy || pending || aiBusy}
									>
										{submitLabel}
									</Button>
								</Box>
							</Tooltip>
						</Group>
					</Stack>
				</form>

				{children}
			</Stack>
		</Card>
	);
}

// Read-only checklist of the enrichment gaps the backend still wants filled.
// An empty list alone is not "done": the backend can legitimately return
// checklist_complete:false with an empty gaps array (item stays at stage
// "quick"), so the caller threads in the CURRENT item's stage -- read live
// off the item prop at render, never stored alongside gaps -- and only stage
// "full" earns the green completion copy -- otherwise this pass simply
// didn't list anything, worded as a neutral status rather than a false claim
// of completion. Reading stage live (instead of a snapshot taken when gaps
// was set) means a stage change from the quick Select after this pass still
// shows copy that matches the page, not what stage was at enrich time.
function GapsChecklist({ gaps, stage }) {
	if (gaps.length > 0) {
		// Keyed by `${gap}-${occurrence}`, not a raw array index -- the backend
		// can legitimately repeat a gap verbatim, and a bare-value key would
		// collide silently on duplicates.
		const gapOccurrence = new Map();
		return (
			<Stack gap="xs">
				<Text fw={600} size="sm">
					仍待補齊
				</Text>
				<Stack gap={4}>
					{gaps.map((gap) => {
						const occurrence = gapOccurrence.get(gap) ?? 0;
						gapOccurrence.set(gap, occurrence + 1);
						return (
							<Checkbox
								key={`${gap}-${occurrence}`}
								checked={false}
								readOnly
								label={gap}
							/>
						);
					})}
				</Stack>
			</Stack>
		);
	}
	if (stage === "full") {
		return (
			<Text size="sm" c="green">
				補齊完成，無待補缺口
			</Text>
		);
	}
	return (
		<Text size="sm" c="dimmed">
			本次未列出待補項目（項目仍為快速捕捉階段）
		</Text>
	);
}

// The two AI cards shown on the detail page: full enrichment (returns a gaps
// checklist) and an assisted progress update. Each is a useMutation whose
// onSuccess invalidates ['item', itemId] (+ ['items']/['review']) -- the item
// refetch replaces the old manual onRefresh, so status/stage/section/progress
// changes made server-side reappear on their own.
//
// `pending` is ItemDetailPage's page-own mutation gate (patch/progress). This
// component owns the AI half of the gate and reports it up via `onPendingChange`
// so the page can disable ITS controls while an AI action (or the conflict
// refresh) runs -- the "plus AI actions' pending, threaded to ItemAiActions"
// part of the shared gate.
export function ItemAiActions({ item, pending, onPendingChange }) {
	const queryClient = useQueryClient();
	const llm = useAtomValue(llmStatusAtom);
	const configured = llm.configured;
	// llm.loading is intentionally not also checked in the button `disabled`
	// conditions below: handleLlmNotConfigured (see below) flips `configured`
	// to false synchronously the moment a 503 comes back, before any
	// re-probe's `loading` flag even turns on -- so repeat guaranteed
	// failures already find the buttons disabled without needing to
	// additionally couple to `loading`.
	const loadLlmStatus = useSetAtom(loadLlmStatusAtom);
	const markLlmUnconfigured = useSetAtom(markLlmUnconfiguredAtom);
	// Only the gaps array from the last AI 補齊 call. The completion copy
	// derived from it is NOT stored here -- GapsChecklist re-derives it from
	// the live `item.stage` prop at render (see its doc comment), so this
	// never goes stale relative to a later quick-Select stage change.
	const [gaps, setGaps] = useState(null);
	// Conflict-banner refresh in flight (shared by both cards; only the
	// conflicted one renders a banner). Feeds aiBusy below so the page's own
	// controls stay disabled for its duration too.
	const [refreshing, setRefreshing] = useState(false);

	const itemId = String(item.id);

	// Invalidate the item AND the lists/review it can affect; awaited by each
	// action's onSuccess so the mutation's isPending (and thus the gate) stays
	// closed until the item refetch lands. Errors are swallowed (no throwOnError)
	// so a failed BACKGROUND refetch leaves the prior item on screen rather than
	// turning the success into a failure -- see the report note on the old
	// "AI 已完成，但重新載入失敗" secondary path.
	const invalidateAll = () =>
		Promise.all([
			queryClient.invalidateQueries({ queryKey: ["item", itemId] }),
			queryClient.invalidateQueries({ queryKey: ["items"] }),
			queryClient.invalidateQueries({ queryKey: ["review"] }),
		]);

	// Backend just told us (on THIS request) that AI is unavailable -- flip the
	// shared atom synchronously first so the shell banner and every AI button
	// reflect it immediately, THEN fire the async re-probe so a since-fixed
	// backend can restore `configured: true`. Fire-and-forget: it can only
	// confirm this downgrade or correct it (see markLlmUnconfiguredAtom).
	const handleLlmNotConfigured = () => {
		markLlmUnconfigured();
		loadLlmStatus({ force: true });
	};

	// Shared error path for both cards: a 503 llm_not_configured downgrades the
	// shared atom; a 409 surfaces via the derived conflict banner (mutation.error);
	// every error also toasts the mapped zh-TW message (matching the old catch).
	const onActionError = (error) => {
		if (error?.code === "llm_not_configured") {
			handleLlmNotConfigured();
		}
		notifications.show({
			color: "red",
			title: "AI 處理失敗",
			message: error?.message ?? "AI 服務暫時無法使用，請稍後再試",
		});
	};

	const enrichMutation = useMutation({
		mutationFn: (value) =>
			apiPost(`/api/items/${itemId}/enrich`, { additional_context: value }),
		onSuccess: async (result) => {
			// Store only the gaps array -- GapsChecklist reads stage live off the
			// `item` prop below, not a snapshot taken here, so a later stage change
			// from the quick Select can't leave this checklist's copy contradicting
			// the page.
			setGaps(result?.gaps ?? []);
			await invalidateAll();
			notifications.show({ color: "green", message: "AI 已補齊內容" });
		},
		onError: onActionError,
	});

	const assistMutation = useMutation({
		mutationFn: (value) =>
			apiPost(`/api/items/${itemId}/assist-update`, { note: value }),
		onSuccess: async () => {
			// An assist-update can resolve (or otherwise make stale) the gaps the
			// last AI 補齊 flagged, so clear that checklist rather than leave a
			// superseded one on screen -- the next AI 補齊 recomputes it.
			setGaps(null);
			await invalidateAll();
			notifications.show({ color: "green", message: "AI 已更新進度" });
		},
		onError: onActionError,
	});

	// Conflict banner's 重新整理: reload the item so the user can retry against
	// fresh data. throwOnError makes a failed refetch reject (unlike the default
	// swallow) so the failure path keeps the banner and toasts, exactly like the
	// old manual GET; on success mutation.reset() clears the 409 error and hides
	// the banner.
	const refreshConflict = async (mutation) => {
		if (refreshing) {
			return;
		}
		setRefreshing(true);
		try {
			await queryClient.invalidateQueries(
				{ queryKey: ["item", itemId] },
				{ throwOnError: true },
			);
			mutation.reset();
		} catch (_error) {
			notifications.show({
				color: "red",
				title: "重新載入失敗",
				message: "請再試一次",
			});
		} finally {
			setRefreshing(false);
		}
	};

	// The AI half of the page-wide gate: either card's action or the conflict
	// refresh. Reported up so ItemDetailPage can disable its own controls too.
	const aiBusy =
		enrichMutation.isPending || assistMutation.isPending || refreshing;
	useEffect(() => {
		onPendingChange(aiBusy);
	}, [aiBusy, onPendingChange]);

	return (
		<Stack gap="md">
			<AiActionCard
				title="AI 補齊"
				description="提供更多背景，AI 會補齊各區段並列出仍缺少的資訊。"
				fieldName="additional_context"
				fieldLabel="補充背景"
				placeholder="貼上更多背景資訊、對話紀錄或文件片段..."
				submitLabel="AI 補齊"
				configured={configured}
				pending={pending}
				aiBusy={aiBusy}
				mutation={enrichMutation}
				refreshing={refreshing}
				onRefreshConflict={() => refreshConflict(enrichMutation)}
			>
				{gaps ? <GapsChecklist gaps={gaps} stage={item.stage} /> : null}
			</AiActionCard>

			<AiActionCard
				title="AI 進度更新"
				description="描述最新進展，AI 會整理並記錄為一筆進度。"
				fieldName="note"
				fieldLabel="進度說明"
				placeholder="描述剛完成或改變的事情..."
				submitLabel="AI 進度更新"
				configured={configured}
				pending={pending}
				aiBusy={aiBusy}
				mutation={assistMutation}
				refreshing={refreshing}
				onRefreshConflict={() => refreshConflict(assistMutation)}
			/>
		</Stack>
	);
}
