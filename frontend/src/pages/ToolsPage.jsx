import {
	Alert,
	Anchor,
	Badge,
	Button,
	Card,
	Center,
	Collapse,
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
import { useDisclosure } from "@mantine/hooks";
import { notifications } from "@mantine/notifications";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useEffect, useState } from "react";
import { Controller, useForm } from "react-hook-form";
import { apiDelete, apiGet, apiPatch, apiPost } from "../api/client.js";
import { CharCounter } from "../components/CharCounter.jsx";
import { formatDate } from "../components/DateText.jsx";
import { EmptyState } from "../components/EmptyState.jsx";
import { SECTION_MAX_LENGTH } from "../constants/sections.js";
import { usePageTitle } from "../hooks/usePageTitle.js";
import { codePointLength } from "../utils/text.js";
import {
	isHttpUrl,
	isTerminalToolJobState,
	isToolJobActive,
	secretNameError,
	secretValueError,
	toolJobRefetchInterval,
} from "../utils/toolInstall.js";
import { canFinalizeSummary, summaryStatusMeta } from "../utils/toolSummary.js";

// The install instructions textarea and the revise feedback textarea (D40)
// both share the backend's AI-input bound (20000 chars, the same
// _MAX_AI_INPUT_CHARS every AI free-text field carries --
// ToolInstallRequest.instructions and ToolReviseRequest.feedback are both
// `Field(min_length=1, max_length=_MAX_AI_INPUT_CHARS)`).
const AI_INPUT_MAX = SECTION_MAX_LENGTH;

// The shared client maps ANY 404 to the item-flavored 找不到項目 copy; a tool
// mutation's 404 means the tool row itself is gone (deleted elsewhere, or the
// backend restarted with a different TOOLS_DIR), so it gets its own wording.
//
// D40's new codes (tool_finalized / tool_job_in_progress / summary_missing on
// the three summary/revise endpoints, llm_not_configured on regenerate) are
// deliberately NOT branched on here: the shared client's messageFor already
// renders llm_not_configured (503) and any 502 correctly for every AI
// endpoint (including these), and for the three 409s it falls through to
// `rawMessage` -- which already IS the ready-made zh-TW copy routers.tools
// sends (_TOOL_FINALIZED_MESSAGE / _TOOL_JOB_IN_PROGRESS_MESSAGE /
// _SUMMARY_MISSING_MESSAGE). A branch that just reproduced the same string
// would be dead code, so this keeps the one 404 override below and lets every
// other status/code pass through untouched.
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

// The tool-job progress card: spinner while the job runs (2s poll, stopping
// on a terminal state -- see toolJobRefetchInterval), then the green/red
// outcome with a link into the AI 日誌 trace. Shared by BOTH job kinds this
// one job table serves (D40): `kind` only swaps the zh-TW copy naming what
// the job WAS ("安裝"/"修訂") -- the queued/running/failed shell is otherwise
// identical, so this is one component (renamed from the original
// install-only InstallProgress) rather than a second copy for revise.
function ToolJobProgress({ kind, jobId, jobQuery }) {
	if (jobId === null) {
		return null;
	}
	const job = jobQuery.data;
	const verb = kind === "revise" ? "修訂" : "安裝";

	if (jobQuery.isError) {
		return (
			<Alert color="red" title={`無法取得${verb}進度`}>
				<Text size="sm">
					{jobQuery.error?.status === 404
						? `找不到這個${verb}工作，後端可能已重新啟動，請重新送出${verb}`
						: (jobQuery.error?.message ?? "請稍後再試")}
				</Text>
			</Alert>
		);
	}

	if (!job || !isTerminalToolJobState(job.state)) {
		return (
			<Card withBorder padding="md" radius="md">
				<Group gap="sm" wrap="nowrap">
					<Loader size="sm" />
					<Text size="sm">
						AI 正在{verb}工具，可能需要數分鐘……
						{job?.state === "queued" ? "（排隊中）" : ""}
					</Text>
				</Group>
			</Card>
		);
	}

	if (job.state === "succeeded") {
		return (
			<Alert color="green" title={`${verb}完成`}>
				<Stack gap="xs" align="flex-start">
					<Text size="sm">
						{kind === "revise"
							? `已依意見更新工具「${job.tool_name}」。`
							: `已安裝工具「${job.tool_name}」。`}
					</Text>
					{job.summary ? <Text size="sm">{job.summary}</Text> : null}
					<LogLink llmLogId={job.llm_log_id} />
				</Stack>
			</Alert>
		);
	}

	return (
		<Alert color="red" title={`${verb}失敗`}>
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

// draft/final/null -> a small Badge. Shared by the row-level list badge (no
// extra request -- `summary_status` already rides on GET /api/tools, see
// ToolRow) and the expanded panel's own freshly-fetched status (ToolSummaryPanel):
// both read the same three-value vocabulary, so one component keeps the two
// badges visually identical.
function SummaryStatusBadge({ status }) {
	const meta = summaryStatusMeta(status);
	return (
		<Badge size="sm" variant="light" color={meta.color}>
			{meta.label}
		</Badge>
	);
}

// The AI-summary detail for one tool row (D40). Fetched lazily (enabled:
// expanded) so a collapsed row never pulls its summary body -- mirrors
// LlmLogsPage's LogDetailPanel exactly, minus that component's restart-id-reuse
// discriminator: a tool NAME is the resource's own stable address (unlike an
// LLM-log row's integer id, it is never reassigned to a different record after
// a restart), so the query key needs nothing beyond it.
//
// `busy` is the panel-wide single-flight mirror (see InstalledToolsPanel's
// summaryBusy) and gates regenerate + the revise form; it deliberately does
// NOT gate the 定版/解除定版 button -- see that button's own disabled comment
// below for why.
function ToolSummaryPanel({
	name,
	expanded,
	busy,
	isRegenerating,
	onRegenerate,
	isUpdatingStatus,
	onSetStatus,
	reviseMutation,
	isSubmittingRevise,
	reviseJobId,
	reviseJobQuery,
}) {
	const { data, error, isError, isFetching } = useQuery({
		queryKey: ["tool-summary", name],
		queryFn: () => apiGet(`/api/tools/${name}/summary`),
		enabled: expanded,
	});

	const { control, handleSubmit, reset } = useForm({
		defaultValues: { feedback: "" },
	});

	if (data === undefined && isFetching) {
		return (
			<Center py="sm">
				<Loader size="sm" />
			</Center>
		);
	}

	if (isError && data === undefined) {
		return (
			<Alert color="red" title="無法載入總結">
				<Text size="sm">{toolErrorMessage(error, "請稍後再試")}</Text>
			</Alert>
		);
	}

	// A tool with no sidecar yet is a real, all-null 200 (backend
	// ToolSummaryDetail) rather than an error -- this default only covers the
	// (in practice unreachable once the two guards above have passed) case of
	// `data` itself being nullish, so every field access below stays safe.
	const detail = data ?? {
		summary: null,
		status: null,
		updated_at: null,
		llm_log_id: null,
	};
	const isFinal = detail.status === "final";

	const submitRevise = handleSubmit((values) => {
		if (busy || isFinal) {
			return;
		}
		const feedback = values.feedback.trim();
		// Cleared only once the job is actually QUEUED (202), mirroring
		// InstallPanel's own form: the mutation resolving is not "AI is done" for
		// a job-shaped action, just "the request landed" -- see the job progress
		// card below for the part that takes minutes.
		reviseMutation.mutate(
			{ name, feedback },
			{ onSuccess: () => reset({ feedback: "" }) },
		);
	});

	return (
		<Stack gap="sm" pt="xs">
			<Group gap="xs" wrap="wrap">
				<SummaryStatusBadge status={detail.status} />
				{detail.updated_at ? (
					// formatDate (components/DateText.jsx) is the app's existing
					// ISO-datetime -> local YYYY-MM-DD formatter, reused here rather
					// than dumping the raw ISO string or re-deriving LlmLogsPage's own
					// (page-local, unexported) time-of-day formatter.
					<Text size="xs" c="dimmed">
						更新於 {formatDate(detail.updated_at)}
					</Text>
				) : null}
				{detail.llm_log_id != null ? (
					<LogLink llmLogId={detail.llm_log_id} />
				) : null}
			</Group>

			<Text
				size="sm"
				c={detail.summary ? undefined : "dimmed"}
				style={{ whiteSpace: "pre-wrap" }}
			>
				{detail.summary || "尚無總結"}
			</Text>

			<Group gap="sm">
				<Button
					size="xs"
					variant="light"
					loading={isRegenerating}
					disabled={busy || isFinal}
					onClick={onRegenerate}
				>
					重新產生
				</Button>
				<Button
					size="xs"
					variant="light"
					color={isFinal ? "gray" : "teal"}
					loading={isUpdatingStatus}
					// Deliberately NOT gated by `busy`: PATCH .../summary
					// (定版/解除定版) does not touch the backend's job single-flight
					// at all (routers.tools' update_tool_summary_status never reads
					// `_JOBS`/`_SYNC_OPS`), and the backend explicitly supports 定版
					// landing mid-job -- a revise re-checks it again right before
					// swapping the package (docs/web-v4-decisions.md D40 P3b
					// self-review), and a regenerate's own store re-checks it at
					// write time (`_store_meta` -> StoreRefusal.FINALIZED). Gating
					// this on `busy` would block a use the backend was built to
					// support: freezing a tool to stop an in-flight AI iteration
					// the user has changed their mind about. Only the OTHER
					// direction needs a content gate (nothing to freeze without
					// text) -- 解除定版 is the unconditional escape hatch.
					disabled={
						isUpdatingStatus ||
						(!isFinal && !canFinalizeSummary(detail.summary))
					}
					onClick={() => onSetStatus(isFinal ? "draft" : "final")}
				>
					{isFinal ? "解除定版" : "定版"}
				</Button>
			</Group>

			<form onSubmit={submitRevise}>
				<Stack gap={4}>
					<Controller
						name="feedback"
						control={control}
						rules={{
							validate: (value) => {
								const trimmed = value.trim();
								if (trimmed === "") {
									return "請輸入修訂意見";
								}
								if (codePointLength(trimmed) > AI_INPUT_MAX) {
									return `修訂意見不可超過 ${AI_INPUT_MAX} 字`;
								}
								return true;
							},
						}}
						render={({ field, fieldState }) => (
							<div>
								<Textarea
									{...field}
									label="修訂意見"
									description="描述想讓 AI 調整的地方，AI 會依此修改這個工具的實作。"
									placeholder="例如：回應請改成只列出前 5 筆結果。"
									autosize
									minRows={2}
									disabled={busy || isFinal}
									error={fieldState.error?.message}
								/>
								<CharCounter value={field.value} max={AI_INPUT_MAX} />
							</div>
						)}
					/>
					<Group justify="flex-end">
						<Button
							type="submit"
							size="xs"
							loading={isSubmittingRevise}
							disabled={busy || isFinal}
						>
							送出修訂
						</Button>
					</Group>
				</Stack>
			</form>

			<ToolJobProgress
				kind="revise"
				jobId={reviseJobId}
				jobQuery={reviseJobQuery}
			/>
		</Stack>
	);
}

// One installed tool row: name + validity badge + summary-status badge,
// description, the enable switch, delete, and an inline-expandable AI-summary
// panel (D40). The switch is disabled for an invalid package on purpose -- a
// broken package is never advertised/executable regardless of its flag
// (backend contract), so offering the toggle would suggest a state change
// that cannot have any effect; delete is the meaningful action.
function ToolRow({
	tool,
	onToggle,
	onDelete,
	mutating,
	summaryBusy,
	isRegenerating,
	onRegenerate,
	isUpdatingStatus,
	onSetStatus,
	reviseMutation,
	isSubmittingRevise,
	reviseJobId,
	reviseJobQuery,
}) {
	// Local, independent per row (unlike LlmLogsPage's single-open Accordion):
	// there is no reason comparing two tools' summaries side by side should
	// force one closed, and expand/collapse is a pure UI state that is never
	// itself gated.
	const [expanded, { toggle: toggleExpanded }] = useDisclosure(false);

	return (
		<Card withBorder padding="md" radius="md">
			<Stack gap="sm">
				<Group
					justify="space-between"
					align="flex-start"
					wrap="nowrap"
					gap="md"
				>
					<Stack gap={4} style={{ minWidth: 0 }}>
						<Group gap="xs">
							<Text fw={600}>{tool.name}</Text>
							{!tool.valid ? (
								<Badge size="sm" variant="light" color="red">
									無效
								</Badge>
							) : null}
							<SummaryStatusBadge status={tool.summary_status} />
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

				<div>
					<Button variant="subtle" size="xs" px={0} onClick={toggleExpanded}>
						{expanded ? "收合 AI 總結" : "AI 總結"}
					</Button>
					<Collapse in={expanded}>
						<ToolSummaryPanel
							name={tool.name}
							expanded={expanded}
							busy={summaryBusy}
							isRegenerating={isRegenerating}
							onRegenerate={onRegenerate}
							isUpdatingStatus={isUpdatingStatus}
							onSetStatus={onSetStatus}
							reviseMutation={reviseMutation}
							isSubmittingRevise={isSubmittingRevise}
							reviseJobId={reviseJobId}
							reviseJobQuery={reviseJobQuery}
						/>
					</Collapse>
				</div>
			</Stack>
		</Card>
	);
}

// The 已安裝工具 tab: list + enable toggle + delete (confirm modal) + each
// row's AI-summary panel (D40: regenerate / 定版 / 解除定版 / revise).
function InstalledToolsPanel({ externalBusy = false, onBusyChange }) {
	const queryClient = useQueryClient();
	const [deleteTarget, setDeleteTarget] = useState(null);
	// The one revise job this panel is currently tracking, and which tool it
	// belongs to. Mirrors InstallPanel's single `jobId` for the same reason:
	// the backend's single-flight (D40) admits only ONE install/revise job at a
	// time across every tool, so there is never more than one to track.
	const [activeJob, setActiveJob] = useState(null); // { name, jobId } | null

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

	// Regenerate/finalize/revise (D40) are lifted to this panel rather than
	// owned by each ToolRow, for the same reason toggle/delete already are: the
	// backend's single global tool-job slot (see the longer note on
	// ToolsPage) means only ONE of these can ever be legitimately in flight
	// across every row at once, so a shared instance is what lets one row's
	// activity disable every OTHER row's controls (see summaryBusy below).
	const regenerateMutation = useMutation({
		mutationFn: (name) => apiPost(`/api/tools/${name}/summary/regenerate`),
		onSuccess: async (_data, name) => {
			await Promise.all([
				queryClient.invalidateQueries({ queryKey: ["tool-summary", name] }),
				queryClient.invalidateQueries({ queryKey: ["tools"] }),
			]);
			notifications.show({
				color: "green",
				message: `已重新產生「${name}」的總結`,
			});
		},
		onError: (mutationError) => {
			notifications.show({
				color: "red",
				title: "重新產生失敗",
				message: toolErrorMessage(mutationError, "無法重新產生總結"),
			});
		},
	});

	const statusMutation = useMutation({
		mutationFn: ({ name, status }) =>
			apiPatch(`/api/tools/${name}/summary`, { status }),
		onSuccess: async (_data, { name, status }) => {
			await Promise.all([
				queryClient.invalidateQueries({ queryKey: ["tool-summary", name] }),
				queryClient.invalidateQueries({ queryKey: ["tools"] }),
			]);
			notifications.show({
				color: "green",
				message:
					status === "final" ? `已定版「${name}」` : `已解除定版「${name}」`,
			});
		},
		onError: (mutationError) => {
			notifications.show({
				color: "red",
				title: "更新總結狀態失敗",
				message: toolErrorMessage(mutationError, "無法更新總結狀態"),
			});
		},
	});

	const reviseMutation = useMutation({
		mutationFn: ({ name, feedback }) =>
			apiPost(`/api/tools/${name}/revise`, { feedback }),
		onSuccess: (result, { name }) => {
			setActiveJob({ name, jobId: result.job_id });
		},
		onError: (mutationError) => {
			notifications.show({
				color: "red",
				title: "無法送出修訂",
				message: toolErrorMessage(mutationError, "無法送出修訂"),
			});
		},
	});

	const jobQuery = useQuery({
		queryKey: ["tool-job", activeJob?.jobId],
		queryFn: () => apiGet(`/api/tools/jobs/${activeJob.jobId}`),
		enabled: activeJob !== null,
		refetchInterval: toolJobRefetchInterval,
	});
	const job = jobQuery.data;

	// A succeeded revise regenerated the sidecar and replaced the package, so
	// both the row's own summary detail and the list's summary_status badge
	// are stale -- mirrors InstallPanel's own succeeded-transition effect for
	// the exact same reason (a new row's data the OTHER tab's query owns).
	useEffect(() => {
		if (activeJob && job?.state === "succeeded") {
			queryClient.invalidateQueries({
				queryKey: ["tool-summary", activeJob.name],
			});
			queryClient.invalidateQueries({ queryKey: ["tools"] });
		}
	}, [activeJob, job?.state, queryClient]);

	const reviseJobActive = isToolJobActive({
		jobId: activeJob?.jobId ?? null,
		state: job?.state,
		errorStatus: jobQuery.error?.status,
	});

	// Any control that would race the backend's single global tool-job slot
	// (D40: install, revise, and a synchronous regenerate all contend for the
	// SAME `_JOBS`/`_SYNC_OPS` admission -- see the R7-3 addendum in
	// docs/web-v4-decisions.md) must disable together, in BOTH tabs --
	// `externalBusy` carries the install form's own activity in (see
	// ToolsPage). This is a best-effort, LOCAL mirror of that slot (only jobs
	// THIS page instance started or knows about); the backend remains the
	// authority, and the 409 (tool_job_in_progress) this gate is trying to
	// avoid is still handled by regenerateMutation/reviseMutation's onError
	// above for whatever race this local knowledge cannot see -- another
	// browser tab, or a job this page instance never learned about.
	//
	// Deliberately does NOT include statusMutation.isPending: see the
	// 定版/解除定版 button's own disabled comment in ToolSummaryPanel for why
	// PATCH .../summary is exempt from this gate entirely.
	const summaryBusy =
		regenerateMutation.isPending || reviseJobActive || externalBusy;

	useEffect(() => {
		onBusyChange?.(summaryBusy);
	}, [summaryBusy, onBusyChange]);

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

			{externalBusy ? (
				<Alert color="orange" title="請稍候">
					<Text size="sm">
						「安裝新工具」正在進行中，工具總結相關操作暫時無法使用
					</Text>
				</Alert>
			) : null}

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
					summaryBusy={summaryBusy}
					isRegenerating={
						regenerateMutation.isPending &&
						regenerateMutation.variables === tool.name
					}
					onRegenerate={() => regenerateMutation.mutate(tool.name)}
					isUpdatingStatus={
						statusMutation.isPending &&
						statusMutation.variables?.name === tool.name
					}
					onSetStatus={(status) =>
						statusMutation.mutate({ name: tool.name, status })
					}
					reviseMutation={reviseMutation}
					isSubmittingRevise={
						reviseMutation.isPending &&
						reviseMutation.variables?.name === tool.name
					}
					reviseJobId={activeJob?.name === tool.name ? activeJob.jobId : null}
					reviseJobQuery={jobQuery}
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

// The 安裝新工具 tab: URL + instructions form, then the polled progress card.
function InstallPanel({ externalBusy = false, onBusyChange }) {
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
		queryFn: () => apiGet(`/api/tools/jobs/${jobId}`),
		enabled: jobId !== null,
		refetchInterval: toolJobRefetchInterval,
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
	// toolJobRefetchInterval's stop rule exactly (see isToolJobActive), so
	// the form-lock and the poll cadence never disagree.
	const jobActive = isToolJobActive({
		jobId,
		state: job?.state,
		errorStatus: jobQuery.error?.status,
	});

	// Reported up to ToolsPage so the OTHER tab's summary-panel controls
	// (regenerate/revise, which share the backend's single tool-job slot with
	// this form -- D40) disable while this form's own submit or job is live;
	// see the longer note on ToolsPage and on InstalledToolsPanel's
	// summaryBusy for why this crosses tabs at all.
	const busy = installMutation.isPending || jobActive;
	useEffect(() => {
		onBusyChange?.(busy);
	}, [busy, onBusyChange]);

	const fieldsDisabled = installMutation.isPending || jobActive || externalBusy;

	const submit = handleSubmit((values) => {
		if (installMutation.isPending || jobActive || externalBusy) {
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

			{externalBusy && !jobActive ? (
				<Alert color="orange" title="請稍候">
					<Text size="sm">
						「已安裝工具」頁面有 AI 任務正在進行中，請等待完成後再安裝新工具
					</Text>
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
								disabled={fieldsDisabled}
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
								if (codePointLength(trimmed) > AI_INPUT_MAX) {
									return `指示不可超過 ${AI_INPUT_MAX} 字`;
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
									disabled={fieldsDisabled}
								/>
								<CharCounter value={field.value} max={AI_INPUT_MAX} />
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
								disabled={fieldsDisabled}
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
								disabled={fieldsDisabled}
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
							disabled={jobActive || externalBusy}
						>
							開始安裝
						</Button>
					</Group>
				</Stack>
			</form>

			<ToolJobProgress kind="install" jobId={jobId} jobQuery={jobQuery} />
		</Stack>
	);
}

// The 工具 page: installed-tools management plus the AI web installer, as two
// tabs (panels stay mounted -- Mantine's default -- so an install keeps
// polling while the user looks at the list).
export function ToolsPage() {
	usePageTitle("工具");

	// D40's backend job table (`_JOBS`/`_SYNC_OPS`) is ONE global single-flight
	// shared by an install job, a revise job, AND a synchronous regenerate --
	// so a job running in one tab must also lock the OTHER tab's job-starting
	// controls, not just its own. This is only visible to the user because both
	// tabs stay mounted (see the comment above): each panel reports its own
	// busy state up here and receives the other's back down as `externalBusy`.
	// It is a best-effort, LOCAL mirror of the backend's slot (only jobs this
	// page instance itself started/knows about) -- the backend remains the
	// authority, and each mutation's onError still handles the 409 this cannot
	// prevent (another browser tab, or a job this page instance never learned
	// about); see InstalledToolsPanel's summaryBusy comment for the full case.
	const [installBusy, setInstallBusy] = useState(false);
	const [summaryBusy, setSummaryBusy] = useState(false);

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
					<InstalledToolsPanel
						externalBusy={installBusy}
						onBusyChange={setSummaryBusy}
					/>
				</Tabs.Panel>
				<Tabs.Panel value="install" pt="md">
					<InstallPanel
						externalBusy={summaryBusy}
						onBusyChange={setInstallBusy}
					/>
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
