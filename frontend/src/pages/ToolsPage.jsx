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
	toolJobQueryEnabled,
	toolJobRefetchInterval,
} from "../utils/toolInstall.js";
import {
	canFinalizeSummary,
	patchToolRowSummaryStatus,
	summaryStatusMeta,
	toolSummaryKeyPrefix,
	toolSummaryQueryKey,
} from "../utils/toolSummary.js";

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
// `codeCopy` is the opt-in override for a structured code whose BACKEND copy
// names the wrong action for the caller (see the revise path's tool_finalized
// below); anything not listed there keeps the backend's own message. Every D40
// code was checked against the action that can raise it, and only ONE needs
// local copy:
//
// * `tool_finalized` -- _TOOL_FINALIZED_MESSAGE is
//   「總結已定版，請先解除定版再重新產生」, which names REGENERATE. Correct for
//   POST .../summary/regenerate, wrong for POST .../revise, where it appeared
//   under the title 「無法送出修訂」 and told the user to 重新產生 something
//   they never asked to regenerate. The revise call site supplies its own copy;
//   the regenerate call site deliberately does not.
// * `tool_job_in_progress` -- 「已有工具任務正在進行中，請等待完成」 names no
//   action at all, deliberately (routers.tools: it can be raised by a job the
//   user did not start from this control), and reads correctly under BOTH
//   titles. No local copy: a branch reproducing an equally-good string would be
//   dead weight.
// * `summary_missing` -- 「尚無總結可定版」 is raised ONLY by PATCH .../summary
//   and already names that one action. No local copy.
// * `llm_not_configured` (503) / any 502 -- the shared client's messageFor
//   already renders these ("AI 功能尚未設定" / "AI 服務暫時無法使用，請稍後再
//   試") and both are action-neutral. No local copy.
// * `tools_not_configured` / `install_in_progress` -- install-form only, and
//   already handled inline by InstallPanel's own onError. Not reachable here.
function toolErrorMessage(error, fallback, codeCopy = null) {
	if (error?.status === 404) {
		return "找不到這個工具，清單可能已過期，請重新整理";
	}
	const localCopy = error?.code ? codeCopy?.[error.code] : null;
	if (localCopy) {
		return localCopy;
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
// LlmLogsPage's LogDetailPanel, including that component's two-part defence
// against a reused address: an instance discriminator folded into the query key
// (toolSummaryQueryKey -- there for why `description` is the discriminator and
// how far it can be trusted) PLUS a guard on what actually gets rendered (the
// stale-content banner below).
//
// The residual the discriminator cannot close, stated plainly: two installs
// under the same name whose AI-authored descriptions come out byte-identical
// share a cache key. The window that leaves open is only the instant between a
// cache hit and its background refetch landing -- GET /api/tools/{name}/summary
// addresses by NAME, so a SUCCESSFUL refetch always returns the CURRENT tool's
// sidecar. What made that window dangerous was the refetch FAILING: react-query
// keeps `data` and flips status to 'error', and the `isError && data ===
// undefined` arm below is false in that state, so the previous tool's summary
// stayed on screen indefinitely with nothing saying so. The banner is what
// removes that silence.
//
// `busy` is the panel-wide single-flight mirror (see InstalledToolsPanel's
// summaryBusy) and gates regenerate + the revise form; it deliberately does
// NOT gate the 定版/解除定版 button -- see that button's own disabled comment
// below for why. `isTogglingThisTool` is a SEPARATE gate with a different
// cause; see the revise submit for the causal chain.
function ToolSummaryPanel({
	name,
	description,
	expanded,
	busy,
	isTogglingThisTool,
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
		queryKey: toolSummaryQueryKey(name, description),
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
		if (busy || isFinal || isTogglingThisTool) {
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
			{/* Reached only with `data` in hand (the two arms above own the
			    no-data cases), i.e. exactly the state react-query leaves after a
			    BACKGROUND refetch fails: status 'error', previous data retained.
			    Before this, that state rendered the old body as though it had just
			    been verified -- silently, and indefinitely, since nothing else
			    refetches once focus/mount refetches keep failing. The content is
			    still shown (it is the best available reading, and hiding it would
			    lose the 定版 controls with it), but it is now labelled. */}
			{isError ? (
				<Alert color="orange" title="無法更新總結">
					<Text size="sm">
						{toolErrorMessage(error, "請稍後再試")}
						。以下是先前讀到的內容，可能已過期。
					</Text>
				</Alert>
			) : null}

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
									disabled={busy || isFinal || isTogglingThisTool}
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
							// `isTogglingThisTool` is the reverse half of the enable
							// switch's own revise gate (see ToolRow), and it is a
							// FILESYSTEM race, not UI tidiness. A revise session pins the
							// package's identity as `(st_dev, st_ino, st_ctime_ns)` of its
							// tool.json at start and re-checks it immediately before the
							// swap (tool_builder._package_identity / run_revise), while
							// PATCH /api/tools/{name} REWRITES that same tool.json in place
							// to flip `enabled` (tools.set_enabled). So a toggle landing
							// anywhere inside a revise changes the manifest's ctime, the
							// pre-swap check reads a different identity, and the whole
							// multi-minute build is discarded with 「原工具在修訂期間被改動
							// 或重新安裝」. Holding the submit for the few hundred ms a
							// toggle is in flight costs nothing and removes the half of
							// that race the switch's own gate cannot see (a PATCH already
							// on the wire when the revise is queued).
							disabled={busy || isFinal || isTogglingThisTool}
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
	isTogglingThisTool,
	reviseBusyForThisTool,
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
							// `reviseBusyForThisTool` is not over-locking, it is the one
							// coupling the backend cannot absorb. PATCH /api/tools/{name}
							// rewrites THIS package's tool.json in place to flip `enabled`
							// (tools.set_enabled) -- and tool.json's
							// (st_dev, st_ino, st_ctime_ns) is precisely the identity a
							// revise session records at start and re-checks immediately
							// before swapping the rebuilt package in
							// (tool_builder._package_identity, chosen over the directory's
							// own inode exactly BECAUSE the manifest is what an install
							// rewrites). So toggling 啟用 during a revise silently dooms
							// it: minutes later the swap is refused with 「原工具在修訂期間
							// 被改動或重新安裝」 and the whole build is thrown away, with
							// no hint that a toggle caused it. Per-ROW, not panel-wide:
							// only the revised package's own manifest is at stake, so
							// another tool's switch stays live.
							//
							// 刪除 is deliberately NOT in this gate: deleting mid-revise is
							// answered by the backend's own target-missing refusal (the
							// revise finds nothing to swap), and it is what a user who has
							// given up on the tool actually wants -- it also runs through
							// its own confirm modal rather than a one-click toggle.
							disabled={!tool.valid || mutating || reviseBusyForThisTool}
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
					{/* `expanded`, NOT `in`. Mantine 9.4.1's Collapse destructures
					    `expanded` (node_modules/@mantine/core/esm/components/Collapse/
					    Collapse.mjs line 20; CollapseProps declares it REQUIRED in
					    lib/components/Collapse/Collapse.d.ts) -- `in` was React
					    Transition Group's spelling, not this component's. An unknown
					    prop is inert here: it lands in `...others` and is spread onto
					    the wrapper Box, so `expanded` stayed undefined, the panel was
					    permanently collapsed, and only the toggle LABEL responded --
					    taking the summary GET, 重新產生, 定版 and the whole revise form
					    down with it. Nothing in this project could catch that: a wrong
					    prop NAME is valid JS, valid JSX, and valid to Biome, and there
					    is no jsdom to render against (see the P4 addendum in
					    docs/web-v4-decisions.md). */}
					<Collapse expanded={expanded}>
						<ToolSummaryPanel
							name={tool.name}
							description={tool.description}
							expanded={expanded}
							busy={summaryBusy}
							isTogglingThisTool={isTogglingThisTool}
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
			// removeQueries, not invalidateQueries: the tool is GONE, so there is
			// nothing left to revalidate against -- and a same-name reinstall
			// inside TanStack Query's default 5-minute gc window is a DIFFERENT
			// tool, whose panel must start cold rather than pre-populated with the
			// deleted tool's summary/status/llm_log_id (or, if the reinstall's own
			// refetch then transiently fails, with the deleted tool's stale data
			// rendered as though it belonged to the new one).
			//
			// PREFIX filter, and no `exact`: the summary key now carries a third
			// element (the instance discriminator, see toolSummaryQueryKey), so
			// `exact: true` would have matched NOTHING and silently stopped
			// clearing anything at all. TanStack Query's default element-wise
			// partial match is what is wanted here anyway -- it drops EVERY cached
			// instance of this name, including entries left by earlier
			// descriptions -- and it still cannot reach another tool, because the
			// name is the second element of the prefix.
			queryClient.removeQueries({ queryKey: toolSummaryKeyPrefix(name) });
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
	// Both summary-writing mutations end the same way: the response IS the fresh
	// sidecar, so it is written straight into the detail cache, the ONE list
	// field it authoritatively settles is patched onto the row, and the list is
	// still invalidated for everything else. Factored out so the two can never
	// drift into doing this differently.
	//
	// `description` rides in the mutate variables purely as the row's cache
	// discriminator (toolSummaryQueryKey): the acting ROW is the only place that
	// knows which instance it is writing for, and reading it back out of the
	// list cache here would just be the same value fetched less reliably.
	const applySummaryDetail = (detail, { name, description }) => {
		queryClient.setQueryData(toolSummaryQueryKey(name, description), detail);
		// Patch ONLY summary_status on ONLY this row -- never fabricate a row (see
		// patchToolRowSummaryStatus). The value is safe to cross over: the list's
		// `summary_status` and the detail's `status` are narrowed through the same
		// backend vocabulary check (services/tools._narrowed_summary_status vs
		// routers/tools._summary_detail, both filtering on _SUMMARY_STATUSES), so
		// this can only ever write "draft" | "final" | null -- exactly what
		// GET /api/tools would have returned for that field.
		queryClient.setQueryData(["tools"], (listBody) =>
			patchToolRowSummaryStatus(listBody, name, detail.status),
		);
		// Kept as the eventual-consistency backstop for the REST of the row
		// (enabled, valid, description, error), which this response says nothing
		// about. Its refetch error is still swallowed by TanStack Query -- that is
		// exactly why the two writes above exist rather than relying on it.
		return queryClient.invalidateQueries({ queryKey: ["tools"] });
	};

	const regenerateMutation = useMutation({
		mutationFn: ({ name }) => apiPost(`/api/tools/${name}/summary/regenerate`),
		onSuccess: async (detail, variables) => {
			// Written straight into the cache rather than invalidated: this
			// response body IS the fresh ToolSummaryDetail -- routers.tools'
			// regenerate_tool_summary returns `_summary_detail(meta)` over the
			// just-stored sidecar, the exact same builder GET .../summary calls
			// over the exact same shape (`{summary, status, updated_at,
			// llm_log_id}`, all four fields always present) -- so there is
			// nothing an invalidation's background refetch would tell us that we
			// do not already have in hand, and TanStack Query swallows THAT
			// refetch's error by default: a transient failure right after this
			// success would otherwise leave a green toast next to a stale panel
			// (stuck loading indicator or, worse, the pre-regenerate text/status).
			await applySummaryDetail(detail, variables);
			notifications.show({
				color: "green",
				message: `已重新產生「${variables.name}」的總結`,
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
		onSuccess: async (detail, variables) => {
			// Same reasoning as regenerateMutation above: update_tool_summary_status
			// also returns `_summary_detail(meta)` over a fresh re-read of the
			// sidecar it just wrote (routers/tools.py), the identical builder and
			// shape GET .../summary uses -- so writing it straight into the cache
			// (rather than relying on an invalidation whose refetch error TanStack
			// Query would silently drop) is what keeps the 定版/解除定版 button
			// label, the row's own badge and the AI controls' disabled state from
			// lagging behind their own success toast.
			await applySummaryDetail(detail, variables);
			const { name, status } = variables;
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
				// The backend's tool_finalized copy names 重新產生 because the
				// SAME code also answers a regenerate; under 「無法送出修訂」 it
				// prescribed an action the user never took. This is the race path
				// (the submit button is already disabled on a locally-known
				// 已定版), so it fires when another browser tab or process froze
				// the tool first -- exactly when clear copy matters. See
				// toolErrorMessage for every other code that was checked and
				// deliberately left on the backend's own wording.
				message: toolErrorMessage(mutationError, "無法送出修訂", {
					tool_finalized: "總結已定版，請先解除定版再送出修訂",
				}),
			});
		},
	});

	const jobQuery = useQuery({
		queryKey: ["tool-job", activeJob?.jobId],
		queryFn: () => apiGet(`/api/tools/jobs/${activeJob.jobId}`),
		// Functional `enabled`, computed by the SAME rule that stops the poll (see
		// toolJobQueryEnabled): a plain `activeJob !== null` stayed true forever
		// after the job settled, and with the app's refetchOnWindowFocus + 5s
		// staleTime defaults every refocus re-fetched a finished job -- eventually
		// 404-ing (bounded, process-local job table) and turning a settled
		// 「修訂完成」 card into a 「找不到這個修訂工作」 error card.
		enabled: toolJobQueryEnabled(activeJob?.jobId ?? null),
		refetchInterval: toolJobRefetchInterval,
	});
	const job = jobQuery.data;

	// A succeeded revise regenerated the sidecar and replaced the package, so
	// both the row's own summary detail and the list's summary_status badge
	// are stale -- mirrors InstallPanel's own succeeded-transition effect for
	// the exact same reason (a new row's data the OTHER tab's query owns).
	useEffect(() => {
		if (activeJob && job?.state === "succeeded") {
			// Prefix filter (partial match), so it reaches this tool's entry
			// whatever discriminator it is keyed under -- which matters most
			// precisely here: a revise REBUILDS the package, so the description
			// this tool is keyed on is one of the things that may have just
			// changed.
			queryClient.invalidateQueries({
				queryKey: toolSummaryKeyPrefix(activeJob.name),
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
	// `reviseMutation.isPending` is its own term and NOT redundant with
	// `reviseJobActive`: the latter only turns true once `activeJob` is set,
	// which happens in reviseMutation's onSuccess (the 202 carrying the new
	// job_id) -- so the gap between the user pressing 送出修訂 and that
	// response landing has isPending true while reviseJobActive is still
	// false. Without this term, every OTHER control this gate covers stayed
	// enabled through that gap, so a regenerate/revise fired against a
	// different row (or an install started from the other tab) could win the
	// backend's single-flight first and make the user's OWN revise request --
	// already in flight -- the one that comes back 409.
	//
	// Deliberately does NOT include statusMutation.isPending: see the
	// 定版/解除定版 button's own disabled comment in ToolSummaryPanel for why
	// PATCH .../summary is exempt from this gate entirely.
	const summaryBusy =
		regenerateMutation.isPending ||
		reviseMutation.isPending ||
		reviseJobActive ||
		externalBusy;

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

			{tools.map((tool) => {
				const isSubmittingRevise =
					reviseMutation.isPending &&
					reviseMutation.variables?.name === tool.name;
				// "A revise is touching THIS package's files, or is about to."
				// Both terms are needed for the same reason summaryBusy needs both
				// of its own: `reviseJobActive` only turns true once the 202 has
				// landed and set `activeJob`, so the submit-in-flight window before
				// that is covered by isSubmittingRevise. Per-row because the hazard
				// it gates (a tool.json rewrite invalidating the revise's package
				// identity -- see the Switch) is confined to the revised package.
				const reviseBusyForThisTool =
					isSubmittingRevise ||
					(activeJob?.name === tool.name && reviseJobActive);
				// The mirror-image term: a PATCH already on the wire for THIS tool.
				// One toggleMutation serves every row, so the row must be matched
				// explicitly -- toggling tool A does not endanger a revise of B.
				const isTogglingThisTool =
					toggleMutation.isPending &&
					toggleMutation.variables?.name === tool.name;
				return (
					<ToolRow
						key={tool.name}
						tool={tool}
						mutating={mutating}
						isTogglingThisTool={isTogglingThisTool}
						reviseBusyForThisTool={reviseBusyForThisTool}
						onToggle={(name, enabled) =>
							toggleMutation.mutate({ name, enabled })
						}
						onDelete={(name) => setDeleteTarget(name)}
						summaryBusy={summaryBusy}
						isRegenerating={
							regenerateMutation.isPending &&
							regenerateMutation.variables?.name === tool.name
						}
						// `description` rides along purely as this row's summary-cache
						// discriminator (toolSummaryQueryKey); the request itself is
						// still addressed by name alone.
						onRegenerate={() =>
							regenerateMutation.mutate({
								name: tool.name,
								description: tool.description,
							})
						}
						isUpdatingStatus={
							statusMutation.isPending &&
							statusMutation.variables?.name === tool.name
						}
						onSetStatus={(status) =>
							statusMutation.mutate({
								name: tool.name,
								description: tool.description,
								status,
							})
						}
						reviseMutation={reviseMutation}
						isSubmittingRevise={isSubmittingRevise}
						reviseJobId={activeJob?.name === tool.name ? activeJob.jobId : null}
						reviseJobQuery={jobQuery}
					/>
				);
			})}

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
		// Same rule as the revise poll's own `enabled` (see InstalledToolsPanel):
		// once the install has settled -- or its id has 404'd -- nothing more can
		// be learned by asking again, and leaving it enabled meant every window
		// refocus re-fetched it, eventually replacing a finished 安裝完成 card
		// with a 404 error card when the job aged out of the bounded table.
		enabled: toolJobQueryEnabled(jobId),
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
// tabs -- see the keepMountedMode comment on the Tabs element below for what
// actually keeps an in-flight install/revise job polling no matter which tab
// is showing.
export function ToolsPage() {
	usePageTitle("工具");

	// D40's backend job table (`_JOBS`/`_SYNC_OPS`) is ONE global single-flight
	// shared by an install job, a revise job, AND a synchronous regenerate --
	// so a job running in one tab must also lock the OTHER tab's job-starting
	// controls, not just its own. This is only visible to the user because both
	// tabs stay mounted AND keep polling in the background (see the
	// keepMountedMode comment on the Tabs element below): each panel reports
	// its own busy state up here and receives the other's back down as
	// `externalBusy`.
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

			{/* keepMountedMode="display-none" is load-bearing, not a redundant prop
			    to tidy away. Mantine's OWN default (keepMountedMode="activity") wraps
			    an inactive Tabs.Panel's children in React's Activity with
			    mode="hidden" whenever keepMounted is true (also Mantine's default --
			    see node_modules/@mantine/core/esm/components/Tabs/TabsPanel/
			    TabsPanel.mjs lines 23-34). A hidden Activity boundary PRESERVES
			    component state but DESTROYS effects, and react-query's
			    refetchInterval (toolJobRefetchInterval) is effect-driven -- so
			    without this prop, a revise job polled from InstalledToolsPanel would
			    silently stop advancing the instant the user switched to the install
			    tab (symmetrically for an in-flight install), and the busy flag
			    mirrored across tabs (installBusy/summaryBusy above) would stay stuck
			    at whatever it last reported -- locking the OTHER tab's controls for
			    no reason visible to the user. "display-none" mode hides the inactive
			    panel with a plain CSS `display: none` on the panel's own wrapper Box
			    instead of an Activity boundary; that style is already applied to the
			    wrapper in EITHER mode (see the cited source), so this changes
			    nothing about what is visible or focusable, only whether the
			    children's effects keep running while off-screen. No jsdom in this
			    repo's tests to assert this with a render (see toolInstall.test.js/
			    toolSummary.test.js for the pure logic that IS covered), so this is a
			    code+library-source argument checked against the installed 9.4.1
			    sources cited above, not a test. */}
			<Tabs defaultValue="installed" keepMountedMode="display-none">
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
