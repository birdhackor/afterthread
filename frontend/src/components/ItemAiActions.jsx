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
import { SECTION_MAX_LENGTH } from "../constants/sections.js";

const DISABLED_TOOLTIP =
	"AI 功能尚未設定：請在 backend/.env 填入 OPENAI_BASE_URL 後重啟";

// One AI action card: a textarea + submit button that POSTs to an LLM endpoint.
// Handles the long-request loading state (double-submit is blocked while the
// request is in flight), disables + explains the button when the LLM is not
// configured, and surfaces the shared error mapping. A 409 additionally offers
// an inline 重新整理 action, since the item changed under the AI mid-request.
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
	children,
}) {
	const {
		control,
		handleSubmit,
		reset,
		formState: { isSubmitting },
	} = useForm({ defaultValues: { [fieldName]: "" } });
	const [conflict, setConflict] = useState(false);

	const submit = handleSubmit(async (values) => {
		if (!configured || isSubmitting) {
			return;
		}
		const value = values[fieldName].trim();
		if (!value) {
			return;
		}
		setConflict(false);
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
				return;
			}
		}
		notifications.show({ color: "green", message: successMessage });
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
								onClick={() => {
									setConflict(false);
									onRefresh?.().catch(() => {});
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
								maxLength: {
									value: SECTION_MAX_LENGTH,
									message: `內容不可超過 ${SECTION_MAX_LENGTH} 字`,
								},
							}}
							render={({ field, fieldState }) => (
								<Textarea
									{...field}
									label={fieldLabel}
									placeholder={placeholder}
									autosize
									minRows={3}
									maxLength={SECTION_MAX_LENGTH}
									disabled={isSubmitting}
									error={fieldState.error?.message}
								/>
							)}
						/>
						<Group justify="flex-end">
							<Tooltip
								label={DISABLED_TOOLTIP}
								disabled={configured}
								multiline
								w={260}
								withArrow
							>
								<Box display="inline-block">
									<Button
										type="submit"
										loading={isSubmitting}
										disabled={!configured || isSubmitting}
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
function GapsChecklist({ gaps }) {
	if (gaps.length === 0) {
		return (
			<Text size="sm" c="green">
				目前沒有仍待補齊的缺口
			</Text>
		);
	}
	return (
		<Stack gap="xs">
			<Text fw={600} size="sm">
				仍待補齊
			</Text>
			<Stack gap={4}>
				{gaps.map((gap) => (
					<Checkbox key={gap} checked={false} readOnly label={gap} />
				))}
			</Stack>
		</Stack>
	);
}

// The two AI cards shown on the detail page: full enrichment (returns a gaps
// checklist) and an assisted progress update. Both refresh the parent item on
// success so status/stage/section/progress changes made server-side appear.
export function ItemAiActions({ item, onRefresh }) {
	const llm = useAtomValue(llmStatusAtom);
	const configured = llm.configured;
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
					setGaps(result?.gaps ?? []);
					await onRefresh();
				}}
				onRefresh={onRefresh}
			>
				{gaps ? <GapsChecklist gaps={gaps} /> : null}
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
					await onRefresh();
				}}
				onRefresh={onRefresh}
			/>
		</Stack>
	);
}
