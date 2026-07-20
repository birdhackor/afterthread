import {
	Alert,
	Anchor,
	Badge,
	Button,
	Card,
	Center,
	Group,
	Loader,
	Modal,
	PasswordInput,
	Stack,
	Switch,
	Tabs,
	Text,
	Textarea,
	TextInput,
	Title,
} from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useEffect, useState } from "react";
import { Controller, useForm } from "react-hook-form";
import { apiDelete, apiGet, apiPatch, apiPost } from "../api/client.js";
import { CharCounter } from "../components/CharCounter.jsx";
import { EmptyState } from "../components/EmptyState.jsx";
import { SECTION_MAX_LENGTH } from "../constants/sections.js";
import { usePageTitle } from "../hooks/usePageTitle.js";
import { codePointLength } from "../utils/text.js";
import {
	installJobRefetchInterval,
	isHttpUrl,
	isInstallJobActive,
	isTerminalInstallState,
	secretNameError,
	secretValueError,
} from "../utils/toolInstall.js";

// The instructions textarea shares the backend's AI-input bound (20000 chars,
// the same _MAX_AI_INPUT_CHARS every AI free-text field carries).
const INSTRUCTIONS_MAX = SECTION_MAX_LENGTH;

// The shared client maps ANY 404 to the item-flavored 找不到項目 copy; a tool
// mutation's 404 means the tool row itself is gone (deleted elsewhere, or the
// backend restarted with a different TOOLS_DIR), so it gets its own wording.
function toolErrorMessage(error, fallback) {
	if (error?.status === 404) {
		return "找不到這個工具，清單可能已過期，請重新整理";
	}
	return error?.message ?? fallback;
}

// "查看 AI 日誌" link shown on both install outcomes: the builder session's
// full prompt/response trace is the debugging surface for a failed (or
// suspicious) install, and llm_log_id names the exact record to expand. The
// link deep-links to that record via `?log=<id>` (LlmLogsPage reads it and
// auto-expands the matching row on load); with no id it is a plain jump.
function LogLink({ llmLogId }) {
	return (
		<Anchor
			component={Link}
			to="/llm-logs"
			search={llmLogId != null ? { log: llmLogId } : {}}
			size="sm"
		>
			查看 AI 日誌{llmLogId != null ? `（紀錄 #${llmLogId}）` : ""}
		</Anchor>
	);
}

// One installed tool row: name + validity badge, description, the enable
// switch, and delete. The switch is disabled for an invalid package on
// purpose -- a broken package is never advertised/executable regardless of
// its flag (backend contract), so offering the toggle would suggest a state
// change that cannot have any effect; delete is the meaningful action.
function ToolRow({ tool, onToggle, onDelete, mutating }) {
	return (
		<Card withBorder padding="md" radius="md">
			<Group justify="space-between" align="flex-start" wrap="nowrap" gap="md">
				<Stack gap={4} style={{ minWidth: 0 }}>
					<Group gap="xs">
						<Text fw={600}>{tool.name}</Text>
						{!tool.valid ? (
							<Badge size="sm" variant="light" color="red">
								無效
							</Badge>
						) : null}
					</Group>
					{!tool.valid && tool.error ? (
						<Text size="xs" c="red">
							{tool.error}
						</Text>
					) : null}
					{tool.description ? (
						<Text size="sm" c="dimmed">
							{tool.description}
						</Text>
					) : null}
				</Stack>
				<Group gap="sm" wrap="nowrap">
					<Switch
						size="sm"
						label="啟用"
						labelPosition="left"
						checked={tool.enabled}
						disabled={!tool.valid || mutating}
						onChange={(event) =>
							onToggle(tool.name, event.currentTarget.checked)
						}
					/>
					<Button
						size="xs"
						color="red"
						variant="light"
						disabled={mutating}
						onClick={() => onDelete(tool.name)}
					>
						刪除
					</Button>
				</Group>
			</Group>
		</Card>
	);
}

// The 已安裝工具 tab: list + enable toggle + delete (confirm modal).
function InstalledToolsPanel() {
	const queryClient = useQueryClient();
	const [deleteTarget, setDeleteTarget] = useState(null);

	const { data, error, isError, isFetching, refetch } = useQuery({
		queryKey: ["tools"],
		queryFn: () => apiGet("/api/tools"),
	});

	const toggleMutation = useMutation({
		mutationFn: ({ name, enabled }) =>
			apiPatch(`/api/tools/${name}`, { enabled }),
		onSuccess: async (updated) => {
			notifications.show({
				color: "green",
				message: updated.enabled
					? `已啟用「${updated.name}」`
					: `已停用「${updated.name}」`,
			});
			await queryClient.invalidateQueries({ queryKey: ["tools"] });
		},
		onError: (mutationError) => {
			notifications.show({
				color: "red",
				title: "更新失敗",
				message: toolErrorMessage(mutationError, "無法更新工具"),
			});
		},
	});

	const deleteMutation = useMutation({
		mutationFn: (name) => apiDelete(`/api/tools/${name}`),
		onSuccess: async (_data, name) => {
			setDeleteTarget(null);
			notifications.show({ color: "green", message: `已刪除「${name}」` });
			await queryClient.invalidateQueries({ queryKey: ["tools"] });
		},
		onError: (mutationError) => {
			setDeleteTarget(null);
			notifications.show({
				color: "red",
				title: "刪除失敗",
				message: toolErrorMessage(mutationError, "無法刪除工具"),
			});
		},
	});

	// Same first-load / retry discipline as the AI 日誌 page: `data === undefined
	// && isFetching` re-shows the Loader on 重新整理 after a failure, while a
	// failed background refetch that still has rows falls through to them.
	const loading = data === undefined && isFetching;
	const showError = isError && data === undefined && !isFetching;
	const tools = data?.tools ?? [];
	const mutating = toggleMutation.isPending || deleteMutation.isPending;

	return (
		<Stack gap="md">
			<Group justify="flex-end">
				<Button variant="light" loading={isFetching} onClick={() => refetch()}>
					重新整理
				</Button>
			</Group>

			{loading ? (
				<Center py="xl">
					<Loader />
				</Center>
			) : null}

			{showError ? (
				<Alert color="red" title="載入失敗">
					<Stack gap="sm" align="flex-start">
						<Text size="sm">{error?.message ?? "無法載入工具清單"}</Text>
						<Button size="xs" onClick={() => refetch()}>
							重試
						</Button>
					</Stack>
				</Alert>
			) : null}

			{data && tools.length === 0 ? (
				<EmptyState message="尚未安裝任何工具" />
			) : null}

			{tools.map((tool) => (
				<ToolRow
					key={tool.name}
					tool={tool}
					mutating={mutating}
					onToggle={(name, enabled) => toggleMutation.mutate({ name, enabled })}
					onDelete={(name) => setDeleteTarget(name)}
				/>
			))}

			<Modal
				opened={deleteTarget !== null}
				onClose={() => setDeleteTarget(null)}
				title="刪除工具"
				centered
				closeOnEscape={!deleteMutation.isPending}
				closeOnClickOutside={!deleteMutation.isPending}
				withCloseButton={!deleteMutation.isPending}
			>
				<Stack gap="md">
					<Text>
						確定要刪除「{deleteTarget}
						」嗎？整個工具目錄（含其設定與金鑰檔）將被移除，此動作無法復原。
					</Text>
					<Group justify="flex-end">
						<Button
							variant="default"
							onClick={() => setDeleteTarget(null)}
							disabled={deleteMutation.isPending}
						>
							取消
						</Button>
						<Button
							color="red"
							loading={deleteMutation.isPending}
							onClick={() => deleteMutation.mutate(deleteTarget)}
						>
							刪除
						</Button>
					</Group>
				</Stack>
			</Modal>
		</Stack>
	);
}

// The install-progress card: spinner while the job runs (2s poll, stopping on
// a terminal state -- see installJobRefetchInterval), then the green/red
// outcome with a link into the AI 日誌 trace.
function InstallProgress({ jobId, jobQuery }) {
	if (jobId === null) {
		return null;
	}
	const job = jobQuery.data;

	if (jobQuery.isError) {
		return (
			<Alert color="red" title="無法取得安裝進度">
				<Text size="sm">
					{jobQuery.error?.status === 404
						? "找不到這個安裝工作，後端可能已重新啟動，請重新送出安裝"
						: (jobQuery.error?.message ?? "請稍後再試")}
				</Text>
			</Alert>
		);
	}

	if (!job || !isTerminalInstallState(job.state)) {
		return (
			<Card withBorder padding="md" radius="md">
				<Group gap="sm" wrap="nowrap">
					<Loader size="sm" />
					<Text size="sm">
						AI 正在建置工具，可能需要數分鐘……
						{job?.state === "queued" ? "（排隊中）" : ""}
					</Text>
				</Group>
			</Card>
		);
	}

	if (job.state === "succeeded") {
		return (
			<Alert color="green" title="安裝完成">
				<Stack gap="xs" align="flex-start">
					<Text size="sm">已安裝工具「{job.tool_name}」。</Text>
					{job.summary ? <Text size="sm">{job.summary}</Text> : null}
					<LogLink llmLogId={job.llm_log_id} />
				</Stack>
			</Alert>
		);
	}

	return (
		<Alert color="red" title="安裝失敗">
			<Stack gap="xs" align="flex-start">
				<Text size="sm">{job.error ?? "未知錯誤"}</Text>
				{job.summary ? (
					<Text size="sm" c="dimmed">
						AI 回報：{job.summary}
					</Text>
				) : null}
				<LogLink llmLogId={job.llm_log_id} />
			</Stack>
		</Alert>
	);
}

// The 安裝新工具 tab: URL + instructions form, then the polled progress card.
function InstallPanel() {
	const queryClient = useQueryClient();
	const [jobId, setJobId] = useState(null);
	const [notConfigured, setNotConfigured] = useState(false);
	const [conflictMessage, setConflictMessage] = useState(null);

	const { control, handleSubmit, getValues } = useForm({
		defaultValues: {
			openapi_url: "",
			instructions: "",
			secret_name: "",
			secret_value: "",
		},
	});

	const installMutation = useMutation({
		mutationFn: (payload) => apiPost("/api/tools/install", payload),
		onSuccess: (data) => {
			setJobId(data.job_id);
		},
		onError: (submitError) => {
			// The one structured "feature off" signal (503 tools_not_configured,
			// the only endpoint that can emit it) renders as the explanatory
			// Alert below rather than a transient notification.
			if (submitError?.code === "tools_not_configured") {
				setNotConfigured(true);
				return;
			}
			// One install runs at a time (backend 409 install_in_progress): show
			// the backend's zh-TW reason inline on the form so the user knows to
			// wait for the running install, rather than a transient toast. jobId is
			// deliberately NOT touched here (and submit no longer clears it), so the
			// still-running job stays tracked and its progress card keeps polling --
			// the conflict is only that a SECOND install cannot start yet.
			if (submitError?.code === "install_in_progress") {
				setConflictMessage(
					submitError.message ?? "已有安裝正在進行中，請等待其完成",
				);
				return;
			}
			notifications.show({
				color: "red",
				title: "無法開始安裝",
				message: submitError?.message ?? "請稍後再試",
			});
		},
	});

	const jobQuery = useQuery({
		queryKey: ["tool-install", jobId],
		queryFn: () => apiGet(`/api/tools/install/${jobId}`),
		enabled: jobId !== null,
		refetchInterval: installJobRefetchInterval,
	});
	const job = jobQuery.data;

	// A succeeded install added a row the 已安裝工具 tab must show; invalidating
	// on the state transition (idempotent -- StrictMode's double effect just
	// invalidates twice) keeps the two tabs consistent without a manual refresh.
	useEffect(() => {
		if (job?.state === "succeeded") {
			queryClient.invalidateQueries({ queryKey: ["tools"] });
		}
	}, [job?.state, queryClient]);

	// One install at a time from this form: the job stays "active" -- form locked,
	// progress card polling -- until it reaches a terminal state OR its poll 404s
	// (the job is gone, e.g. after a backend restart). Crucially a NON-404 poll
	// error (a transient 500, a network blip) keeps it active: releasing on such a
	// blip would let a resubmit fire against a job that is still running on the
	// backend, hit the 409, and orphan a job we can no longer poll. This mirrors
	// installJobRefetchInterval's stop rule exactly (see isInstallJobActive), so
	// the form-lock and the poll cadence never disagree.
	const jobActive = isInstallJobActive({
		jobId,
		state: job?.state,
		errorStatus: jobQuery.error?.status,
	});

	const submit = handleSubmit((values) => {
		if (installMutation.isPending || jobActive) {
			return;
		}
		setNotConfigured(false);
		setConflictMessage(null);
		// The secret pair is optional and validated both-or-neither above, so by
		// the time submit runs either both are set or both are empty. Include them
		// only when supplied (D36); the VALUE rides in this POST body ONCE and is
		// never kept in job state (see the backend's tool_builder / redaction).
		const secretName = values.secret_name.trim();
		const secretValue = values.secret_value.trim();
		// Do NOT clear jobId here: if this POST comes back 409 (a job is still
		// running), the old id must survive so its polling continues -- clearing it
		// up front would orphan that live job. The id is replaced only when a new
		// POST succeeds (onSuccess -> setJobId), so a 409 or 500 leaves the old job
		// tracked and pollable; a fresh 202 swaps in the new job.
		installMutation.mutate({
			openapi_url: values.openapi_url.trim(),
			instructions: values.instructions.trim(),
			...(secretName || secretValue
				? { secret_name: secretName, secret_value: secretValue }
				: {}),
		});
	});

	return (
		<Stack gap="md">
			{notConfigured ? (
				<Alert color="orange" title="工具功能尚未啟用">
					<Text size="sm">
						後端尚未設定工具目錄（TOOLS_DIR）。以 uvx
						安裝的版本會自動啟用；開發模式請在 backend/.env 設定 TOOLS_DIR
						後重新啟動後端，詳見 README。
					</Text>
				</Alert>
			) : null}

			{conflictMessage ? (
				<Alert color="orange" title="無法開始安裝">
					<Text size="sm">{conflictMessage}</Text>
				</Alert>
			) : null}

			<form onSubmit={submit}>
				<Stack gap="md">
					<Controller
						name="openapi_url"
						control={control}
						rules={{
							validate: (value) => {
								const trimmed = value.trim();
								if (trimmed === "") {
									return "請輸入 OpenAPI 文件網址";
								}
								if (!isHttpUrl(trimmed)) {
									return "網址必須以 http:// 或 https:// 開頭";
								}
								return true;
							},
						}}
						render={({ field, fieldState }) => (
							<TextInput
								{...field}
								withAsterisk
								label="OpenAPI JSON 網址"
								placeholder="https://kb.internal.example/openapi.json"
								error={fieldState.error?.message}
								disabled={installMutation.isPending || jobActive}
							/>
						)}
					/>
					<Controller
						name="instructions"
						control={control}
						rules={{
							validate: (value) => {
								const trimmed = value.trim();
								if (trimmed === "") {
									return "請描述要建立的工具";
								}
								if (codePointLength(trimmed) > INSTRUCTIONS_MAX) {
									return `指示不可超過 ${INSTRUCTIONS_MAX} 字`;
								}
								return true;
							},
						}}
						render={({ field, fieldState }) => (
							<div>
								<Textarea
									{...field}
									withAsterisk
									label="給 AI 的指示"
									description="描述要建立什麼工具：要查什麼資料、用哪個端點、認證方式。金鑰請填在下方「秘密值」欄位，不要貼在這裡（貼在指示裡會被記錄）。"
									placeholder="例如：建立一個用關鍵字搜尋內部知識庫的工具，使用 /search 端點；API key 放在 X-Api-Key 標頭（金鑰請填下方秘密欄位）。"
									autosize
									minRows={5}
									error={fieldState.error?.message}
									disabled={installMutation.isPending || jobActive}
								/>
								<CharCounter value={field.value} max={INSTRUCTIONS_MAX} />
							</div>
						)}
					/>
					<Controller
						name="secret_name"
						control={control}
						rules={{
							// Cross-field (both-or-neither): reads the value too, and
							// re-validates the value field when the name changes.
							deps: ["secret_value"],
							validate: (value) =>
								secretNameError(value, getValues("secret_value")) ?? true,
						}}
						render={({ field, fieldState }) => (
							<TextInput
								{...field}
								label="秘密名稱（選填，例：KB_API_KEY）"
								placeholder="KB_API_KEY"
								error={fieldState.error?.message}
								disabled={installMutation.isPending || jobActive}
							/>
						)}
					/>
					<Controller
						name="secret_value"
						control={control}
						rules={{
							deps: ["secret_name"],
							validate: (value) =>
								secretValueError(getValues("secret_name"), value) ?? true,
						}}
						render={({ field, fieldState }) => (
							<PasswordInput
								{...field}
								label="秘密值（選填）"
								placeholder="貼上 API key……"
								error={fieldState.error?.message}
								disabled={installMutation.isPending || jobActive}
							/>
						)}
					/>
					<Text size="xs" c="dimmed">
						在此輸入的金鑰會直接寫入工具自己的 .env 與即時測試環境，並會從 AI
						日誌中遮蔽；若改把金鑰貼在上方指示文字裡，仍會被記錄（只有比對到已知秘密時才遮蔽）。
					</Text>
					<Group justify="flex-end">
						<Button
							type="submit"
							loading={installMutation.isPending}
							disabled={jobActive}
						>
							開始安裝
						</Button>
					</Group>
				</Stack>
			</form>

			<InstallProgress jobId={jobId} jobQuery={jobQuery} />
		</Stack>
	);
}

// The 工具 page: installed-tools management plus the AI web installer, as two
// tabs (panels stay mounted -- Mantine's default -- so an install keeps
// polling while the user looks at the list).
export function ToolsPage() {
	usePageTitle("工具");

	return (
		<Stack gap="md">
			<div>
				<Title order={2}>工具</Title>
				<Text c="dimmed" size="sm">
					管理 AI 可呼叫的工具，或貼上 OpenAPI 文件網址讓 AI 建立新工具。
				</Text>
			</div>

			<Tabs defaultValue="installed">
				<Tabs.List>
					<Tabs.Tab value="installed">已安裝工具</Tabs.Tab>
					<Tabs.Tab value="install">安裝新工具</Tabs.Tab>
				</Tabs.List>
				<Tabs.Panel value="installed" pt="md">
					<InstalledToolsPanel />
				</Tabs.Panel>
				<Tabs.Panel value="install" pt="md">
					<InstallPanel />
				</Tabs.Panel>
			</Tabs>

			<Text size="xs" c="dimmed">
				安裝過程會由 AI 在伺服器上寫檔（write_file
				僅限工具暫存目錄）並以服務自身權限執行 shell 指令，請只安裝你信任的 API
				描述與指示。
			</Text>
		</Stack>
	);
}
