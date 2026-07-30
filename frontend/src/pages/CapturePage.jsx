import {
	Alert,
	Anchor,
	Box,
	Button,
	Card,
	Divider,
	Group,
	Stack,
	Text,
	Textarea,
	Title,
	Tooltip,
} from "@mantine/core";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useAtomValue, useSetAtom } from "jotai";
import { useState } from "react";
import { Controller, useForm } from "react-hook-form";
import { apiPost } from "../api/client.js";
import {
	llmStatusAtom,
	loadLlmStatusAtom,
	markLlmUnconfiguredAtom,
} from "../atoms/llm.js";
import { BulletList } from "../components/BulletList.jsx";
import { CharCounter } from "../components/CharCounter.jsx";
import { StaleBadge } from "../components/StaleBadge.jsx";
import { StatusBadge } from "../components/StatusBadge.jsx";
import { LLM_NOT_CONFIGURED_NOTICE } from "../constants/labels.js";
import { SECTION_MAX_LENGTH } from "../constants/sections.js";
import { usePageTitle } from "../hooks/usePageTitle.js";
import { codePointLength } from "../utils/text.js";

// The capture textarea shares the backend's per-field bound (20000 chars).
const CAPTURE_MAX = SECTION_MAX_LENGTH;

// Summary of a freshly captured item plus the AI's follow-up questions. The
// questions render as a non-interactive bullet list the user should answer
// soon; the primary button carries them into the detail page's AI 補齊 card.
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
						<BulletList items={questions} />
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
	// llm.loading is intentionally not also checked in the submit button's
	// `disabled` below: on a 503 the catch block flips `configured` to false
	// synchronously (see markLlmUnconfigured below) before any re-probe's
	// `loading` flag even turns on, so a repeat guaranteed failure already
	// finds the button disabled without needing to additionally couple to
	// `loading`.
	const loadLlmStatus = useSetAtom(loadLlmStatusAtom);
	const markLlmUnconfigured = useSetAtom(markLlmUnconfiguredAtom);
	const queryClient = useQueryClient();
	const [result, setResult] = useState(null);
	const [error, setError] = useState(null);

	const { control, handleSubmit, reset } = useForm({
		defaultValues: { raw_text: "" },
	});

	// Capture POST as a mutation. The button/textarea now read
	// captureMutation.isPending (RHF's own isSubmitting would flip false the
	// instant mutate() fires, since the submit handler no longer awaits).
	const captureMutation = useMutation({
		mutationFn: (value) =>
			apiPost("/api/capture", { body: { raw_text: value } }),
		onSuccess: (data) => {
			setResult(data);
			// Clear the box only on success; a failure keeps the text for retry.
			reset({ raw_text: "" });
			// The capture created an item -- refresh the list and review buckets so
			// it shows up when the user navigates there.
			queryClient.invalidateQueries({ queryKey: ["items"] });
			queryClient.invalidateQueries({ queryKey: ["review"] });
		},
		onError: (submitError) => {
			setError(submitError);
			if (submitError?.code === "llm_not_configured") {
				// Backend just told us (on THIS request) AI is unavailable (503) --
				// downgrade the shared atom synchronously FIRST so the shell banner
				// and this page's own submit button reflect it immediately instead
				// of staying on a stale `configured: true` until the next full page
				// load, THEN fire the async re-probe so a since-fixed backend can
				// restore `configured: true` -- it can only confirm or correct this
				// downgrade, so it's fire-and-forget here (see
				// markLlmUnconfiguredAtom's doc comment).
				markLlmUnconfigured();
				loadLlmStatus({ force: true });
			}
		},
	});

	const submit = handleSubmit((values) => {
		if (!configured || captureMutation.isPending) {
			return;
		}
		setError(null);
		setResult(null);
		captureMutation.mutate(values.raw_text.trim());
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
							// Trim first, validate the trimmed value -- submit sends
							// values.raw_text.trim(), so the length cap must apply to
							// what's actually posted, not the raw textarea value.
							validate: (value) => {
								const trimmed = value.trim();
								if (trimmed === "") {
									return "請貼上要捕捉的內容";
								}
								if (codePointLength(trimmed) > CAPTURE_MAX) {
									return `內容不可超過 ${CAPTURE_MAX} 字`;
								}
								return true;
							},
						}}
						render={({ field, fieldState }) => (
							<div>
								<Textarea
									{...field}
									autoFocus
									placeholder="貼上剛結束的討論、想法或決策……"
									autosize
									minRows={6}
									disabled={captureMutation.isPending}
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
									loading={captureMutation.isPending}
									disabled={!configured || captureMutation.isPending}
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
