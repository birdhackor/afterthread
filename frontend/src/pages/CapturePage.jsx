import {
	Alert,
	Anchor,
	Box,
	Button,
	Card,
	Checkbox,
	Divider,
	Group,
	Stack,
	Text,
	Textarea,
	Title,
	Tooltip,
} from "@mantine/core";
import { Link } from "@tanstack/react-router";
import { useAtomValue } from "jotai";
import { useState } from "react";
import { Controller, useForm } from "react-hook-form";
import { apiPost } from "../api/client.js";
import { llmStatusAtom } from "../atoms/llm.js";
import { CharCounter } from "../components/CharCounter.jsx";
import { StaleBadge } from "../components/StaleBadge.jsx";
import { StatusBadge } from "../components/StatusBadge.jsx";
import { LLM_NOT_CONFIGURED_NOTICE } from "../constants/labels.js";
import { SECTION_MAX_LENGTH } from "../constants/sections.js";
import { usePageTitle } from "../hooks/usePageTitle.js";

// The capture textarea shares the backend's per-field bound (20000 chars).
const CAPTURE_MAX = SECTION_MAX_LENGTH;

// Summary of a freshly captured item plus the AI's follow-up questions. The
// questions render as a read-only checklist the user should answer soon; the
// primary button carries them into the detail page's AI 補齊 card.
function CaptureResult({ result }) {
	const { item, questions } = result;
	const detailTo = `/items/${item.id}`;
	return (
		<Card withBorder padding="md" radius="md">
			<Stack gap="sm">
				<Group gap="xs" align="center">
					<Anchor component={Link} to={detailTo} fw={600} fz="lg">
						{item.title}
					</Anchor>
					<StatusBadge status={item.status} />
					<StaleBadge stale={item.is_stale} />
				</Group>
				<Divider />
				{questions.length > 0 ? (
					<Stack gap="xs" align="flex-start">
						<Text fw={600} size="sm">
							追問（建議盡快回答）
						</Text>
						<Stack gap={4}>
							{questions.map((question) => (
								<Checkbox
									key={question}
									checked={false}
									readOnly
									label={question}
								/>
							))}
						</Stack>
						<Button component={Link} to={detailTo} variant="light" mt="xs">
							帶著這些問題去補齊
						</Button>
					</Stack>
				) : (
					<Stack gap="xs" align="flex-start">
						<Text size="sm" c="dimmed">
							AI 沒有其他追問。
						</Text>
						<Button component={Link} to={detailTo} variant="light">
							查看項目詳情
						</Button>
					</Stack>
				)}
			</Stack>
		</Card>
	);
}

// Quick capture: paste raw discussion text, let the AI structure it into a
// memory item and surface follow-up questions. When the LLM is unconfigured the
// submit is disabled (with an explanatory tooltip) and an inline fallback points
// at manual creation; a 502/503 keeps the typed text so the user can retry.
export function CapturePage() {
	usePageTitle("快速捕捉");
	const llm = useAtomValue(llmStatusAtom);
	const configured = llm.configured;
	const [result, setResult] = useState(null);
	const [error, setError] = useState(null);

	const {
		control,
		handleSubmit,
		reset,
		formState: { isSubmitting },
	} = useForm({ defaultValues: { raw_text: "" } });

	const submit = handleSubmit(async (values) => {
		if (!configured || isSubmitting) {
			return;
		}
		const value = values.raw_text.trim();
		setError(null);
		setResult(null);
		try {
			const data = await apiPost("/api/capture", { raw_text: value });
			setResult(data);
			// Clear the box only on success; a failure keeps the text for retry.
			reset({ raw_text: "" });
		} catch (submitError) {
			setError(submitError);
		}
	});

	return (
		<Stack gap="lg">
			<div>
				<Title order={2}>快速捕捉</Title>
				<Text c="dimmed" size="sm">
					貼上剛結束的討論、想法或決策，AI 會整理成一則記憶項目並提出追問。
				</Text>
			</div>

			{!configured ? (
				<Alert color="orange" title="AI 功能尚未設定">
					<Stack gap="xs" align="flex-start">
						<Text size="sm">
							{LLM_NOT_CONFIGURED_NOTICE}，或改用手動建立項目。
						</Text>
						<Button component={Link} to="/items/new" size="xs" variant="light">
							改用手動建立
						</Button>
					</Stack>
				</Alert>
			) : null}

			{error ? (
				<Alert
					color="red"
					title="捕捉失敗"
					withCloseButton
					onClose={() => setError(null)}
				>
					{error.message ?? "AI 服務暫時無法使用，請稍後再試"}
				</Alert>
			) : null}

			<form onSubmit={submit}>
				<Stack gap="xs">
					<Controller
						name="raw_text"
						control={control}
						rules={{
							required: "請貼上要捕捉的內容",
							maxLength: {
								value: CAPTURE_MAX,
								message: `內容不可超過 ${CAPTURE_MAX} 字`,
							},
							validate: (value) => value.trim() !== "" || "請貼上要捕捉的內容",
						}}
						render={({ field, fieldState }) => (
							<div>
								<Textarea
									{...field}
									autoFocus
									placeholder="貼上剛結束的討論、想法或決策……"
									autosize
									minRows={6}
									maxLength={CAPTURE_MAX}
									disabled={isSubmitting}
									error={fieldState.error?.message}
								/>
								<CharCounter value={field.value} max={CAPTURE_MAX} />
							</div>
						)}
					/>
					<Group justify="space-between" align="center">
						<Anchor component={Link} to="/items/new" size="sm">
							改用手動建立
						</Anchor>
						<Tooltip
							label={LLM_NOT_CONFIGURED_NOTICE}
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
									AI 快速捕捉
								</Button>
							</Box>
						</Tooltip>
					</Group>
				</Stack>
			</form>

			{result ? <CaptureResult result={result} /> : null}
		</Stack>
	);
}
