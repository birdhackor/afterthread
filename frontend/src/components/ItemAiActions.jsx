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
import { useAtomValue } from "jotai";
import { useState } from "react";
import { Controller, useForm } from "react-hook-form";
import { apiPost } from "../api/client.js";
import { llmStatusAtom } from "../atoms/llm.js";
import { LLM_NOT_CONFIGURED_NOTICE } from "../constants/labels.js";
import { SECTION_MAX_LENGTH } from "../constants/sections.js";
import { codePointLength } from "../utils/text.js";

// One AI action card: a textarea + submit button that POSTs to an LLM endpoint.
// Handles the long-request loading state (double-submit is blocked while the
// request is in flight), disables + explains the button when the LLM is not
// configured, and surfaces the shared error mapping. A 409 additionally offers
// an inline 重新整理 action, since the item changed under the AI mid-request.
//
// `pending` is ItemDetailPage's page-wide mutation gate -- true while this
// card's own submit, the OTHER AI card's submit, a quick Select PATCH or the
// progress-note submit is in flight -- combined below with this card's own
// `isSubmitting` so the button is disabled for either reason.
// `onMutationStart`/`onMutationEnd` bracket this card's own action()+refresh
// so every other mutating control is disabled for its duration too. The
// conflict banner's own 重新整理 button is a plain GET (no action() call) but
// still brackets itself with the same pair and checks `pending`/its own
// `refreshing` lock before firing, so it is just as exclusive with every
// other mutating control -- and ItemDetailPage's refresh() tags every
// request with a monotonic id, so a slow, superseded GET from here can never
// clobber state a faster, later request already landed.
function AiActionCard({
	title,
	description,
	fieldName,
	fieldLabel,
	placeholder,
	submitLabel,
	successMessage,
	configured,
	action,
	onSuccess,
	onRefresh,
	pending,
	onMutationStart,
	onMutationEnd,
	children,
}) {
	const {
		control,
		handleSubmit,
		reset,
		formState: { isSubmitting },
	} = useForm({ defaultValues: { [fieldName]: "" } });
	const [conflict, setConflict] = useState(false);
	const [refreshing, setRefreshing] = useState(false);

	const submit = handleSubmit(async (values) => {
		if (!configured || isSubmitting || pending) {
			return;
		}
		const value = values[fieldName].trim();
		setConflict(false);
		onMutationStart();
		let result;
		try {
			result = await action(value);
		} catch (error) {
			if (error?.status === 409) {
				setConflict(true);
			}
			notifications.show({
				color: "red",
				title: "AI 處理失敗",
				message: error?.message ?? "AI 服務暫時無法使用，請稍後再試",
			});
			onMutationEnd();
			return;
		}
		reset({ [fieldName]: "" });
		if (onSuccess) {
			try {
				await onSuccess(result);
			} catch (_error) {
				notifications.show({
					color: "red",
					title: "重新載入失敗",
					message: "AI 已完成，但重新載入失敗，請重新整理頁面",
				});
				onMutationEnd();
				return;
			}
		}
		notifications.show({ color: "green", message: successMessage });
		onMutationEnd();
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
								disabled={pending || refreshing}
								onClick={async () => {
									if (pending || refreshing) {
										return;
									}
									setRefreshing(true);
									onMutationStart();
									try {
										await onRefresh?.();
										setConflict(false);
									} catch (_error) {
										notifications.show({
											color: "red",
											title: "重新載入失敗",
											message: "請再試一次",
										});
									} finally {
										setRefreshing(false);
										onMutationEnd();
									}
								}}
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
									disabled={isSubmitting}
									error={fieldState.error?.message}
								/>
							)}
						/>
						<Group justify="flex-end">
							<Tooltip
								label={configured ? "處理中…" : LLM_NOT_CONFIGURED_NOTICE}
								disabled={configured && !pending}
								multiline
								w={260}
								withArrow
							>
								<Box display="inline-block">
									<Button
										type="submit"
										loading={isSubmitting}
										disabled={!configured || isSubmitting || pending}
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
// checklist) and an assisted progress update. Both refresh the parent item on
// success so status/stage/section/progress changes made server-side appear.
//
// `pending`/`onMutationStart`/`onMutationEnd` are ItemDetailPage's page-wide
// mutation gate, threaded through unchanged to both cards below (see
// AiActionCard's doc comment) so an AI submit is exclusive with the quick
// Selects, the progress-note submit and the OTHER AI card's submit.
export function ItemAiActions({
	item,
	onRefresh,
	pending,
	onMutationStart,
	onMutationEnd,
}) {
	const llm = useAtomValue(llmStatusAtom);
	const configured = llm.configured;
	// Only the gaps array from the last AI 補齊 call. The completion copy
	// derived from it is NOT stored here -- GapsChecklist re-derives it from
	// the live `item.stage` prop at render (see its doc comment), so this
	// never goes stale relative to a later quick-Select stage change.
	const [gaps, setGaps] = useState(null);

	return (
		<Stack gap="md">
			<AiActionCard
				title="AI 補齊"
				description="提供更多背景，AI 會補齊各區段並列出仍缺少的資訊。"
				fieldName="additional_context"
				fieldLabel="補充背景"
				placeholder="貼上更多背景資訊、對話紀錄或文件片段..."
				submitLabel="AI 補齊"
				successMessage="AI 已補齊內容"
				configured={configured}
				action={(value) =>
					apiPost(`/api/items/${item.id}/enrich`, {
						additional_context: value,
					})
				}
				onSuccess={async (result) => {
					await onRefresh();
					// Store only the gaps array -- GapsChecklist reads stage live off
					// the `item` prop below, not off a snapshot taken here, so a later
					// stage change from the quick Select can't leave this checklist's
					// copy contradicting the page.
					setGaps(result?.gaps ?? []);
				}}
				onRefresh={onRefresh}
				pending={pending}
				onMutationStart={onMutationStart}
				onMutationEnd={onMutationEnd}
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
				successMessage="AI 已更新進度"
				configured={configured}
				action={(value) =>
					apiPost(`/api/items/${item.id}/assist-update`, { note: value })
				}
				onSuccess={async () => {
					// An assist-update can resolve (or otherwise make stale) the gaps
					// the last AI 補齊 flagged, so clear that checklist rather than
					// leave a superseded one on screen -- the next AI 補齊 recomputes
					// it from scratch.
					setGaps(null);
					await onRefresh();
				}}
				onRefresh={onRefresh}
				pending={pending}
				onMutationStart={onMutationStart}
				onMutationEnd={onMutationEnd}
			/>
		</Stack>
	);
}
