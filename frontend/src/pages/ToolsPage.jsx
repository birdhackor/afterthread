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
import { useCallback, useEffect, useRef, useState } from "react";
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
	claimLatestSummaryWrite,
	createSummaryWriteLedger,
	nextSummaryWriteStamp,
	ownSummaryBusy,
	patchToolRowSummaryStatus,
	summaryErrorRevalidates,
	summaryStatusMeta,
	toolInstanceKey,
	toolSummaryKeyPrefix,
	toolSummaryQueryKey,
	writeSummaryDetailIfPresent,
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
// * `install_in_progress` -- _INSTALL_IN_PROGRESS_MESSAGE is 「已有安裝正在進行
//   中，請等待其完成」, which names INSTALL. That was true when only installs
//   contended for the slot; since D40 the SAME admission (`_JOBS`/`_SYNC_OPS`)
//   is taken by a revise job and by a synchronous regenerate too, and
//   `POST /api/tools/install` still answers all three refusals with this one
//   install-flavoured code (routers/tools.py: `start_install_job` returning None
//   -> _INSTALL_IN_PROGRESS_CODE; backend test_tool_builder.py pins install
//   getting exactly this code while a REGENERATE holds the slot). So an install
//   refused because another tab is revising told the user to wait for an install
//   nobody started. The install call site supplies its own neutral copy.
// * `tool_job_in_progress` -- 「已有工具任務正在進行中，請等待完成」 names no
//   action at all, deliberately (routers.tools: it can be raised by a job the
//   user did not start from this control), and reads correctly under BOTH the
//   revise and the regenerate titles -- the reverse of the install case above,
//   which is why only that one needed new copy. No local copy: a branch
//   reproducing an equally-good string would be dead weight.
// * `summary_missing` -- 「尚無總結可定版」 is raised ONLY by PATCH .../summary
//   and already names that one action. No local copy.
// * `llm_not_configured` (503) / any 502 -- the shared client's messageFor
//   already renders these ("AI 功能尚未設定" / "AI 服務暫時無法使用，請稍後再
//   試") and both are action-neutral. No local copy.
// * `tools_not_configured` -- install-form only, and already handled inline by
//   InstallPanel's own onError as an explanatory Alert. Not reachable here.
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
// `writesBlocked` is every reason the two name-addressed AI WRITES (重新產生,
// 送出修訂) must not be issued right now: the panel-wide single-flight mirror
// AND a known-stale tool list (see InstalledToolsPanel's summaryWritesBlocked
// for both halves). It deliberately does NOT gate the 定版/解除定版 button --
// see that button's own disabled comment below for why. `isTogglingThisTool` is
// a SEPARATE gate with a different cause; see the revise submit for the causal
// chain.
function ToolSummaryPanel({
	name,
	description,
	expanded,
	writesBlocked,
	settlingJobEnd,
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
		if (writesBlocked || isFinal || isTogglingThisTool) {
			return;
		}
		const feedback = values.feedback.trim();
		// Cleared only once the job is actually QUEUED (202), mirroring
		// InstallPanel's own form: the mutation resolving is not "AI is done" for
		// a job-shaped action, just "the request landed" -- see the job progress
		// card below for the part that takes minutes.
		reviseMutation.mutate(
			{ name, description, feedback },
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
					disabled={writesBlocked || isFinal}
					onClick={onRegenerate}
				>
					重新產生
				</Button>
				<Button
					size="xs"
					variant="light"
					color={isFinal ? "gray" : "teal"}
					loading={isUpdatingStatus}
					// Deliberately NOT gated by `writesBlocked`, for BOTH of the
					// reasons that value folds together.
					//
					// Not by the busy half: PATCH .../summary (定版/解除定版) does not
					// touch the backend's job single-flight at all (routers.tools'
					// update_tool_summary_status never reads `_JOBS`/`_SYNC_OPS`), and
					// the backend explicitly supports 定版 landing mid-job -- a revise
					// re-checks it again right before swapping the package
					// (docs/web-v4-decisions.md D40 P3b self-review), and a
					// regenerate's own store re-checks it at write time (`_store_meta`
					// -> StoreRefusal.FINALIZED). Gating this would block a use the
					// backend was built to support: freezing a tool to stop an
					// in-flight AI iteration the user has changed their mind about.
					//
					// Not by the stale-list half either, and that is a deliberate
					// asymmetry with 重新產生/送出修訂 rather than an oversight. This
					// endpoint is name-addressed like they are, so it CAN land on a
					// tool that is no longer the one this row describes -- but (a) it
					// is the documented escape hatch (D40 r6: 解除定版 is
					// unconditional on the backend precisely because it is the one
					// recovery path for a sidecar nothing else can unstick), and with
					// 重新產生 and 送出修訂 already refused by tool_finalized on a
					// frozen tool, disabling this one too leaves an operator with a
					// flapping GET /api/tools and NO action at all; (b) it writes one
					// enum field, reversible by pressing the other direction, where a
					// misdirected revise rebuilds a package from feedback authored for
					// a different tool; (c) its enablement is computed from
					// `detail.summary` -- this panel's OWN name-addressed
					// GET .../summary, whose successful read is always the current
					// tool's sidecar -- not from the stale list row, so it is the one
					// control here that is not reasoning from the stale data.
					//
					// Only the 定版 direction needs a content gate (nothing to freeze
					// without text); 解除定版 stays unconditional.
					//
					// `settlingJobEnd` DOES gate it, and that is not a contradiction of
					// the paragraph above (R8-1). staleList is a persistent condition --
					// gating on it could strand an operator with no action at all --
					// while this is one round trip that clears itself. And what it
					// protects is specific: a revise just rewrote the summary, so the
					// text on screen is the PREVIOUS one; freezing during that window
					// would finalize content the user has never seen.
					disabled={
						isUpdatingStatus ||
						settlingJobEnd ||
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
									disabled={writesBlocked || isFinal || isTogglingThisTool}
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
							disabled={writesBlocked || isFinal || isTogglingThisTool}
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
	writesBlocked,
	settlingJobEnd,
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
							writesBlocked={writesBlocked}
							settlingJobEnd={settlingJobEnd}
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
	// The one revise job this panel is currently tracking, and which tool
	// INSTANCE it belongs to (name + the row's discriminator at submit time --
	// see activeJobKey). Mirrors InstallPanel's single `jobId` for the same
	// reason: the backend's single-flight (D40) admits only ONE install/revise
	// job at a time across every tool, so there is never more than one to track.
	const [activeJob, setActiveJob] = useState(null); // { name, description, jobId } | null
	// Orders the two summary WRITES against each other (see
	// createSummaryWriteLedger). A ref, not state: nothing renders from it, and a
	// re-render must never reset it -- it is the only record of which write is the
	// newest for a given tool instance.
	const writeLedger = useRef(createSummaryWriteLedger());

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
	//
	// ONE instance identity drives BOTH writes: `summaryKey` addresses the detail
	// entry, `instanceKey` addresses the row whose badge is patched, and the two
	// are the same value in two encodings (toolInstanceKey is toolSummaryQueryKey
	// stringified). That is the whole point -- a response that cannot be proven to
	// be about a row does not touch that row, instead of being stamped onto
	// whatever answers to the name now.
	//
	// Whether the tool list on screen is known to be stale, read LIVE from the
	// cache rather than from a render-time snapshot: this runs inside a mutation
	// callback that may settle several renders after the closure was created, and
	// the answer must describe the moment of the WRITE. Same shape as the
	// `staleList` the panel renders its banner from (an error with data still
	// present), asked of the query client instead of this render's props.
	const listIsStale = () =>
		queryClient.getQueryState(["tools"])?.status === "error" &&
		queryClient.getQueryData(["tools"]) !== undefined;

	// Re-read BOTH halves of what a summary response describes: the panel's own
	// detail entry and the row badge that shows the same sidecar field. By NAME
	// prefix, because a revalidation asks the name-addressed server to answer
	// again -- writes address an instance, re-reads address the name.
	// useCallback because an effect depends on it (see the job-ending effect): an
	// identity that changed every render would re-run that effect every render,
	// and it sets state.
	const revalidateSummaryAndList = useCallback(
		(name) =>
			Promise.all([
				queryClient.invalidateQueries({ queryKey: toolSummaryKeyPrefix(name) }),
				queryClient.invalidateQueries({ queryKey: ["tools"] }),
			]),
		[queryClient],
	);

	// `stamp` orders this write against the OTHER mutation that can be writing the
	// same entry concurrently (see createSummaryWriteLedger).
	const applySummaryDetail = async (detail, { name, description }, stamp) => {
		const summaryKey = toolSummaryQueryKey(name, description);
		const instanceKey = toolInstanceKey(name, description);
		// Ordering gate, BEFORE the cancel: a response that a newer write has
		// already superseded must not even cancel queries -- the newer write's
		// trailing invalidation is a fresh read of the post-write server, and
		// cancelling it would throw away the only thing that could still correct
		// the row.
		if (!claimLatestSummaryWrite(writeLedger.current, instanceKey, stamp)) {
			// Superseded by a newer WRITE, but still invalidate (R5-2). An issue
			// stamp orders our own requests, not the SERVER's writes: two HTTP
			// requests can reach the sidecar lock in the opposite order, so the
			// response we are dropping may be the one describing the LATER server
			// state. Dropping its value is right (we cannot tell), dropping the
			// re-read too is not -- that is how the panel ends up permanently
			// showing draft for a tool the server has already finalized, with a
			// green toast next to it. The re-read costs one GET and can only ever
			// return the post-write truth.
			//
			// BOTH keys, not just the list (R6-1): the entry whose value we just
			// dropped is the SUMMARY one, so re-reading only the row badge would
			// leave the expanded panel asserting the state we decided we could not
			// trust -- the same permanent disagreement one line up, moved from the
			// badge to the panel.
			return revalidateSummaryAndList(name);
		}
		// Cancel BEFORE writing, for both keys about to be written. A write that
		// races a read it did not cancel is a write that can be undone by older
		// data: setQueryData does not touch in-flight fetches, so a GET .../summary
		// that a window refocus started BEFORE this PATCH -- and that read 「draft」
		// -- can land AFTER this line and put 「draft」 back over the authoritative
		// 「final」 we were just handed. Nothing would report it, because that GET
		// SUCCEEDED: no error banner, no failed refetch, just the panel silently
		// reverting to the pre-mutation state seconds after a green success toast.
		// Same hazard for the row badge and its ["tools"] read.
		//
		// Verified against @tanstack/query-core 5.101.2 rather than assumed:
		// queryClient.cancelQueries -> query.cancel({revert: true}) -> the retryer's
		// `cancel` REJECTS its thenable synchronously (retryer.js lines 29-35), so
		// query.#fetch takes its CancelledError path and never calls setData with
		// the late response (query.js lines 308-318) -- and cancelQueries itself
		// swallows everything (`.then(noop).catch(noop)`, queryClient.js line 146),
		// so it can never reject this onSuccess. The invalidation at the end then
		// starts a FRESH read, one that can only have seen the post-write server.
		await Promise.all([
			queryClient.cancelQueries({ queryKey: summaryKey }),
			queryClient.cancelQueries({ queryKey: ["tools"] }),
		]);
		// Re-ask the ordering gate: the cancel above is an await, so a newer
		// response can be admitted and start its own cancel inside this window,
		// and then whichever cancel settles LAST would write last. Asking twice
		// with the same stamp is idempotent by construction (only a STRICTLY newer
		// applied stamp refuses).
		if (!claimLatestSummaryWrite(writeLedger.current, instanceKey, stamp)) {
			return revalidateSummaryAndList(name);
		}
		// Write only if the entry is still there. removeQueries on delete cannot
		// stop a request already on the wire, and a plain value write would rebuild
		// the entry it just cleared (see writeSummaryDetailIfPresent).
		queryClient.setQueryData(summaryKey, writeSummaryDetailIfPresent(detail));
		// Patch ONLY summary_status on ONLY the row with this INSTANCE identity --
		// never fabricate a row, and never a same-name row we cannot prove is the
		// one this response is about (see patchToolRowSummaryStatus). The value is
		// safe to cross over: the list's `summary_status` and the detail's `status`
		// are narrowed through the same backend vocabulary check
		// (services/tools._narrowed_summary_status vs routers/tools._summary_detail,
		// both filtering on _SUMMARY_STATUSES), so this can only ever write
		// "draft" | "final" | null -- exactly what GET /api/tools would have
		// returned for that field.
		// ... and NOT while the list is known stale (R5-1). Two reasons, either
		// alone sufficient: the row we would patch carries an identity we have
		// already decided not to trust (it is what blocks the AI writes), and a
		// value write flips the query from error back to success -- clearing the
		// very `staleList` flag that gate reads, so a permitted 定版 would silently
		// re-enable a revise submit whose draft belongs to the tool the stale row
		// describes. The invalidation below is the honest alternative: it re-reads
		// instead of asserting, and a still-failing refetch keeps the error state.
		if (!listIsStale()) {
			queryClient.setQueryData(["tools"], (listBody) =>
				patchToolRowSummaryStatus(listBody, instanceKey, detail.status),
			);
		}
		// Then re-read BOTH, always (R7-1). The writes above are for immediacy --
		// they show the answer we were just handed without waiting for a round
		// trip, and they survive an invalidation whose error TanStack Query
		// swallows. They are NOT a claim that we know the server's order: two
		// summary mutations can be in flight together (finalize must stay
		// available during a regenerate), and neither our issue stamps nor the
		// arrival order tells us which one the backend applied last -- server
		// order and response order can interleave in either direction. So the
		// value we write is the best guess, and this re-read is the truth. It also
		// covers the REST of the row (enabled, valid, description, error), which
		// this response says nothing about.
		return revalidateSummaryAndList(name);
	};

	// The failure counterpart of applySummaryDetail, shared by all three
	// name-addressed summary mutations. A refusal is not only a message: some
	// refusals are the server TELLING us it is no longer what we are showing, and
	// until r4 every one of them merely raised a toast over a panel that kept
	// asserting the state the error had just contradicted -- including the case
	// where the remedy the message names (解除定版) is a button that only appears
	// once the panel knows the tool is final. summaryErrorRevalidates decides
	// which ones prove that, code by code, so this is not a blanket refetch.
	//
	// The summary side uses the NAME PREFIX, not the instance key, and that is the
	// same split r3 recorded: writes must name an instance, but a REVALIDATION
	// just asks the server again and can only ever come back with the current
	// answer for that name -- so reaching every entry filed under the name is the
	// conservative direction (identical to deleteMutation's removeQueries and the
	// revise job's own invalidation).
	const revalidateAfterSummaryError = (mutationError, name) => {
		if (!summaryErrorRevalidates(mutationError ?? {})) {
			return;
		}
		queryClient.invalidateQueries({ queryKey: toolSummaryKeyPrefix(name) });
		queryClient.invalidateQueries({ queryKey: ["tools"] });
	};

	const regenerateMutation = useMutation({
		// Stamp taken here, not in onSuccess: onMutate runs before the request is
		// sent, so the stamps rank the two writes by when the user ISSUED them --
		// which is the order that has to win when the responses come back swapped.
		onMutate: () => ({ stamp: nextSummaryWriteStamp(writeLedger.current) }),
		mutationFn: ({ name }) => apiPost(`/api/tools/${name}/summary/regenerate`),
		onSuccess: async (detail, variables, context) => {
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
			await applySummaryDetail(detail, variables, context.stamp);
			notifications.show({
				color: "green",
				message: `已重新產生「${variables.name}」的總結`,
			});
		},
		onError: (mutationError, variables) => {
			notifications.show({
				color: "red",
				title: "重新產生失敗",
				message: toolErrorMessage(mutationError, "無法重新產生總結"),
			});
			revalidateAfterSummaryError(mutationError, variables.name);
		},
	});

	const statusMutation = useMutation({
		// See regenerateMutation: the two share one cache entry and can be in
		// flight together (定版 is deliberately outside the busy gate), so both
		// stamp their writes from the same ledger.
		onMutate: () => ({ stamp: nextSummaryWriteStamp(writeLedger.current) }),
		mutationFn: ({ name, status }) =>
			apiPatch(`/api/tools/${name}/summary`, { status }),
		onSuccess: async (detail, variables, context) => {
			// Same reasoning as regenerateMutation above: update_tool_summary_status
			// also returns `_summary_detail(meta)` over a fresh re-read of the
			// sidecar it just wrote (routers/tools.py), the identical builder and
			// shape GET .../summary uses -- so writing it straight into the cache
			// (rather than relying on an invalidation whose refetch error TanStack
			// Query would silently drop) is what keeps the 定版/解除定版 button
			// label, the row's own badge and the AI controls' disabled state from
			// lagging behind their own success toast.
			await applySummaryDetail(detail, variables, context.stamp);
			const { name, status } = variables;
			notifications.show({
				color: "green",
				message:
					status === "final" ? `已定版「${name}」` : `已解除定版「${name}」`,
			});
		},
		onError: (mutationError, variables) => {
			notifications.show({
				color: "red",
				title: "更新總結狀態失敗",
				message: toolErrorMessage(mutationError, "無法更新總結狀態"),
			});
			revalidateAfterSummaryError(mutationError, variables.name);
		},
	});

	const reviseMutation = useMutation({
		mutationFn: ({ name, feedback }) =>
			apiPost(`/api/tools/${name}/revise`, { feedback }),
		onSuccess: (result, { name, description }) => {
			// `description` is recorded, not sent: like the two summary mutations it
			// rides in the variables purely as the acting row's instance
			// discriminator, and it is what lets the progress card stay attached to
			// the tool the revise was submitted against rather than to whatever
			// package answers to that name later (see activeJobKey).
			setActiveJob({ name, description, jobId: result.job_id });
		},
		onError: (mutationError, variables) => {
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
			// No stamp here: this mutation never writes the summary caches (its 202
			// only records the job id), so it has nothing to order. It still owes the
			// same revalidation on a refusal that proves the server moved -- the
			// tool_finalized copy right above tells the user to 解除定版, and that
			// button does not exist until the panel knows the tool is final.
			revalidateAfterSummaryError(mutationError, variables.name);
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

	// A settled revise means the row's own summary detail and the list's
	// summary_status badge may both be stale -- on SUCCESS because the job
	// regenerated the sidecar and replaced the package (mirrors InstallPanel's own
	// transition effect, for the same reason: a row's data the OTHER tab's query
	// owns), and on FAILURE because most of the ways a revise can fail ARE the
	// server saying it is no longer what we are showing.
	//
	// FAILURE is included deliberately, and it is a wider rule than the code-by-
	// code one revalidateAfterSummaryError applies to the immediate refusals. The
	// reason is that the job payload carries no structured cause: ToolJobStatus is
	// `{job_id, state, created_at, finished_at, error, tool_name, summary,
	// llm_log_id}` (backend schemas.py) -- `error` is friendly zh-TW prose the
	// backend rewords every review round, so matching on it would be a gate that
	// silently opens. What CAN be said precisely is the failure vocabulary: of
	// tool_builder's revise outcomes, 找不到要修訂的工具 / 原工具已被刪除 /
	// 原工具目錄已被替換為連結 / 原工具在修訂期間被改動或重新安裝 /
	// 總結已定版 / 無法確認總結是否已定版 / 無法確認原工具的內容 all assert a
	// change we are not showing, and the `.env` ones say the package was
	// hand-edited; only the build/LLM failures assert nothing. Refetching on the
	// whole terminal transition is therefore mostly right and never wasteful in
	// the way a blanket error refetch is: this fires at most ONCE per job, after
	// minutes of work, not once per retry of a flapping request.
	// A 404 from the poll counts as an ending too (R6-2). It is not a terminal
	// STATE -- the cached body may still say "running" -- but it is the end of
	// what we can observe, and the reason for it is usually that the backend
	// restarted (jobs are process-local) or that the bounded table evicted this
	// one. Either way the work may well have completed: a revise that promoted
	// its package and then lost its job row leaves us holding a stale row and a
	// stale summary while the gate re-opens, so the user's next action lands on
	// the package the vanished job already replaced.
	const jobEnded =
		isTerminalToolJobState(job?.state) || jobQuery.error?.status === 404;
	// The gate must stay CLOSED until that re-read lands (R7-2). A job ending
	// releases isToolJobActive immediately, but what is on screen at that instant
	// is still the pre-job row and the pre-job summary -- and after a revise that
	// is exactly the data the job just invalidated by rebuilding the package. Left
	// open, the user can regenerate or submit new feedback against the OLD tool's
	// state during the refetch, which the 404 card actively invites them to do
	// ("送出修訂" is its stated remedy). So this flag is set with the ending and
	// cleared only when both re-reads settle; it joins the write gate below.
	const [settlingJobEnd, setSettlingJobEnd] = useState(false);
	useEffect(() => {
		if (!(activeJob && jobEnded)) {
			return;
		}
		setSettlingJobEnd(true);
		let cancelled = false;
		// Prefix filter (partial match), so it reaches this tool's entry
		// whatever discriminator it is keyed under -- which matters most
		// precisely here: a revise REBUILDS the package, so the description
		// this tool is keyed on is one of the things that may have just
		// changed.
		revalidateSummaryAndList(activeJob.name).finally(() => {
			if (!cancelled) {
				setSettlingJobEnd(false);
			}
		});
		return () => {
			cancelled = true;
		};
	}, [activeJob, jobEnded, revalidateSummaryAndList]);

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
	//
	// TWO values, not one, and the split is what stops the mirror from echoing.
	// `ownBusy` is what this panel knows FIRST-HAND and is the only thing it
	// reports upward (see ownSummaryBusy); `summaryBusy` additionally honours the
	// other tab's flag and is the single-flight half of the local write gate (the
	// other half is staleList -- see summaryWritesBlocked, which is what actually
	// reaches the controls). Reporting `summaryBusy` upward
	// fed `externalBusy` straight back into the value the 工具 page mirrors, so
	// the install form's own submit (installBusy -> our externalBusy -> our
	// report -> the page's summaryBusy -> the install form's externalBusy) came
	// back to it as 「已安裝工具」頁面有 AI 任務正在進行中 -- during that submit,
	// before any job id anywhere existed.
	const ownBusy = ownSummaryBusy({
		regeneratePending: regenerateMutation.isPending,
		revisePending: reviseMutation.isPending,
		reviseJobActive,
	});
	const summaryBusy = ownBusy || externalBusy;

	useEffect(() => {
		onBusyChange?.(ownBusy);
	}, [ownBusy, onBusyChange]);

	// Same first-load / retry discipline as the AI 日誌 page: `data === undefined
	// && isFetching` re-shows the Loader on 重新整理 after a failure, while a
	// failed background refetch that still has rows falls through to them.
	const loading = data === undefined && isFetching;
	const showError = isError && data === undefined && !isFetching;
	// The OTHER failure shape, which used to render as nothing at all: a
	// background GET /api/tools failed while rows are still on screen. See the
	// Alert below for why silence here was worse than a stale list.
	const staleList = isError && data !== undefined;
	const tools = data?.tools ?? [];
	const mutating = toggleMutation.isPending || deleteMutation.isPending;

	// THE gate for the AI actions that WRITE through a name-addressed endpoint --
	// 重新產生 (POST .../summary/regenerate) and 送出修訂 (POST .../revise). One
	// value, passed down as one prop, so a control cannot be gated on half of it.
	//
	// `staleList` is in here because a warning is not a guarantee. r3 added the
	// orange 「無法更新工具清單」 banner for the state where a background
	// GET /api/tools fails and the rows stay on screen; the banner explained the
	// hazard and then let the user act on it anyway. Concretely: unsent feedback
	// typed for tool A, a same-name reinstall as B in another tab, GET /api/tools
	// failing. The row keeps A's identity (nothing told it otherwise), so it is
	// not remounted and the draft survives -- and 送出修訂 posts to
	// /api/tools/A-the-name/revise, which is B. The instance key that was supposed
	// to make that impossible is stale in exactly the same way the row is; the
	// only honest thing the client can say in that state is "I cannot tell you
	// which tool this is", and the only safe response is to stop writing.
	//
	// Reading is NOT gated: expanding a panel and its GET .../summary write
	// nothing, and the panel's own banner already labels what it shows.
	// 定版/解除定版 is NOT gated either -- see that button's own comment for the
	// full argument; the short version is that it is the documented escape hatch
	// (D40 r6: 解除定版 is unconditional on the backend precisely because it is
	// the one recovery path), it is reversible by pressing it again, and its
	// enablement is computed from the summary query's own name-addressed response
	// rather than from the stale list row. 啟用/停用 and 刪除 stay outside too:
	// both are name-addressed by intent ("the tool called X"), both are
	// reversible or confirmed, and neither carries content authored against one
	// specific instance the way a revise draft does.
	const summaryWritesBlocked = summaryBusy || staleList || settlingJobEnd;

	// The INSTANCE the tracked revise job was submitted against -- the same
	// identity string the rows are keyed by (toolInstanceKey), captured at submit
	// time. Association by NAME alone would follow the name to whatever package
	// answers to it now, so a same-name reinstall would hand the old job's
	// progress card to a row that is a different tool.
	//
	// This is DISPLAY ownership only. The per-row LOCKS below stay keyed on the
	// NAME on purpose, and the difference is not an inconsistency: PATCH
	// /api/tools/{name} and POST /api/tools/{name}/revise both address the
	// backend BY NAME, so the filesystem hazard they gate lands on whatever
	// package currently holds that name -- matching more loosely there is the
	// conservative direction. Deciding which row a card belongs to is the
	// opposite: matching loosely puts a card under a tool it does not describe.
	const activeJobKey = activeJob
		? toolInstanceKey(activeJob.name, activeJob.description)
		: null;
	// ...and if NO row owns it, it is shown at panel level instead (below), so a
	// job can never become invisible while it is still holding the busy gate.
	const orphanedJob =
		activeJob !== null &&
		!tools.some(
			(tool) => toolInstanceKey(tool.name, tool.description) === activeJobKey,
		);

	return (
		<Stack gap="md">
			<Group justify="flex-end">
				<Button
					variant="light"
					loading={isFetching}
					onClick={() => {
						// Refresh means refresh EVERYTHING on screen, not just the list
						// (R5-3). An expanded panel's summary sits under its own key, and
						// a same-name reinstall whose description came out identical
						// REUSES that key -- so a list-only refetch would leave the old
						// tool's summary rendered as success next to a row that is now a
						// different package, with nothing stale-looking to warn about.
						// Revalidating by NAME prefix asks the server what answers to that
						// name now, which is the only question the key cannot answer.
						refetch();
						queryClient.invalidateQueries({ queryKey: ["tool-summary"] });
					}}
				>
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

			{/* The list's counterpart to ToolSummaryPanel's own 「無法更新總結」
			    banner, and for the same reason: react-query keeps `data` and only
			    flips status to 'error', so a failed BACKGROUND refetch left these
			    rows on screen looking freshly verified. Non-blocking on purpose --
			    stale rows beat a blanked page, and 重新整理 is right above.

			    What that silence allowed, concretely: the summary panel is keyed on
			    the ROW's description, but GET /api/tools/{name}/summary addresses by
			    NAME. So with a same-name reinstall, a FAILING GET /api/tools plus a
			    SUCCEEDING summary GET wrote the NEW tool's summary under the OLD
			    description's key, and the page rendered the new tool's summary beside
			    the old tool's row description -- a mixture no single request was
			    wrong about.

			    Mixtures still possible after this banner, now all announced rather
			    than silent: (a) exactly the one above -- the row fields (description,
			    enabled, valid, error, summary_status) are from before the failure
			    while an expanded panel's summary is current, because the summary
			    query keeps succeeding independently; (b) the reverse, list current
			    and summary stale, which the panel's own orange banner announces;
			    (c) the documented residual the discriminator cannot see at all --
			    two installs whose AI-written descriptions come out byte-identical
			    share both the row key and the cache key (see toolSummaryQueryKey),
			    and no banner fires because nothing failed.

			    Since r4 this banner also has to SAY that the AI write actions are
			    inert, because they now are (see summaryWritesBlocked): a banner that
			    described a hazard and left the buttons live was the guarantee r3
			    claimed to have established, minus the enforcement. */}
			{staleList ? (
				<Alert color="orange" title="無法更新工具清單">
					<Text size="sm">
						{error?.message ?? "請稍後再試"}
						。以下清單是先前讀到的內容，可能已過期（工具可能已被刪除或重新安裝），展開的
						AI
						總結則可能來自更新後的工具。在清單重新讀取成功前，「重新產生」與「送出修訂」已暫時停用——這兩個動作都以工具名稱送到後端，無法確認清單是否仍對應同一個工具時送出，可能會改到別的工具；「定版／解除定版」仍可使用。
					</Text>
				</Alert>
			) : null}

			{/* A tracked revise job whose row is no longer in the list -- deleted
			    mid-revise, or replaced by a same-name reinstall. The progress card
			    normally lives INSIDE the row, so when the row went away the job kept
			    polling and kept summaryBusy set while showing nothing: every other
			    summary control stayed disabled for minutes with no explanation, and
			    the eventual 「原工具在修訂期間被改動或重新安裝」/target-missing
			    failure was never displayed at all. Hoisting the same card here keeps
			    the invariant that a tracked job is visible EXACTLY once -- the row
			    renders it when it owns the instance (see reviseJobId below), this
			    renders it when no row does. */}
			{orphanedJob ? (
				<Stack gap="xs">
					<Text size="sm" c="dimmed">
						工具「{activeJob.name}
						」的 AI 修訂進度（這個工具已被刪除或重新安裝，不再對應下方任何一列）
					</Text>
					<ToolJobProgress
						kind="revise"
						jobId={activeJob.jobId}
						jobQuery={jobQuery}
					/>
				</Stack>
			) : null}

			{data && tools.length === 0 ? (
				<EmptyState message="尚未安裝任何工具" />
			) : null}

			{tools.map((tool) => {
				// The row's identity, not just its address. See toolInstanceKey: it
				// is the summary cache key stringified, so a row is remounted at
				// exactly the moment its summary query moves to a different entry.
				const rowKey = toolInstanceKey(tool.name, tool.description);
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
						// Keyed by INSTANCE, not by name. A name is reassignable (another
						// tab can delete a tool and install a different one under it), and
						// React reuses a component instance whose key is unchanged -- so
						// with `key={tool.name}` the row's own state survived a swap the
						// summary cache correctly treated as a new tool: the revise
						// feedback typed for the old tool stayed in the textarea, ready to
						// be submitted against the new one. This is the SAME identity
						// question toolSummaryQueryKey answers, which is why the key is
						// built from it and not spelled again -- if the discriminator ever
						// gets stronger, both move together or the two disagree.
						key={rowKey}
						tool={tool}
						mutating={mutating}
						isTogglingThisTool={isTogglingThisTool}
						reviseBusyForThisTool={reviseBusyForThisTool}
						onToggle={(name, enabled) =>
							toggleMutation.mutate({ name, enabled })
						}
						onDelete={(name) => setDeleteTarget(name)}
						writesBlocked={summaryWritesBlocked}
						settlingJobEnd={settlingJobEnd}
						isRegenerating={
							regenerateMutation.isPending &&
							regenerateMutation.variables?.name === tool.name
						}
						// `description` rides along purely as this row's summary-cache
						// discriminator (toolSummaryQueryKey); the request itself is
						// still addressed by name alone.
						//
						// Re-checked HERE and not only on the button's `disabled`, for the
						// same reason submitRevise re-checks it inside the panel: a
						// disabled prop is a rendering, and the two AI writes must be
						// impossible to issue while the gate holds, not merely awkward.
						onRegenerate={() => {
							if (summaryWritesBlocked) {
								return;
							}
							regenerateMutation.mutate({
								name: tool.name,
								description: tool.description,
							});
						}}
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
						// Instance match, so the card follows the tool it was submitted
						// against rather than the name. When nothing matches, the panel
						// shows it instead (see orphanedJob) -- the two conditions are
						// complements of each other over the same key, so the card is
						// rendered exactly once, never twice and never nowhere.
						reviseJobId={rowKey === activeJobKey ? activeJob.jobId : null}
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
			// One TOOL JOB runs at a time (backend 409 install_in_progress): show the
			// reason inline on the form so the user knows to wait, rather than as a
			// transient toast. jobId is deliberately NOT touched here (and submit no
			// longer clears it), so a still-running job of our own stays tracked and
			// its progress card keeps polling -- the conflict is only that a SECOND
			// job cannot start yet.
			//
			// Local copy, via toolErrorMessage's opt-in codeCopy override (the same
			// mechanism the revise path uses for tool_finalized): the backend's
			// message for this code says 「已有安裝正在進行中」, but since D40 the
			// slot it reports on is shared with AI 修訂 and 重新產生總結 -- so the
			// most common way to see this is an install refused because the OTHER
			// tab, or another browser, is revising. Naming an install that nobody
			// started sends the user looking for it. The 404 arm of toolErrorMessage
			// is unreachable from here: this branch is entered only on a 409.
			if (submitError?.code === "install_in_progress") {
				setConflictMessage(
					toolErrorMessage(submitError, "已有工具任務正在進行中，請等待完成", {
						install_in_progress:
							"已有工具任務正在進行中（安裝、AI 修訂或重新產生總結），請等待完成後再安裝",
					}),
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
	//
	// A 404 counts too (R6-2), and for the same reason it does on the revise side:
	// jobs are process-local, so a backend restart between promote and the poll
	// makes a job that DID install a tool disappear mid-flight. Refetching the
	// list on that ending is how the new row still shows up; treating 404 as
	// "nothing happened" is what left it invisible until a manual refresh.
	const installEnded =
		job?.state === "succeeded" || jobQuery.error?.status === 404;
	// Same reasoning as the revise side's settlingJobEnd (R8-2): jobActive drops
	// the instant the poll ends, but the OTHER tab is still showing the pre-install
	// list -- and an install can hand a name another process just deleted to a new
	// package. Holding the reported-busy flag across the re-read is what makes the
	// row (and its unsent feedback draft) remount before anything can be submitted
	// against the new tool.
	const [settlingInstallEnd, setSettlingInstallEnd] = useState(false);
	useEffect(() => {
		if (installEnded) {
			setSettlingInstallEnd(true);
			let cancelled = false;
			// The summary prefix too (R7-3): an install can hand the name of a
			// tool someone else just deleted to a BRAND NEW package, and when the
			// AI-authored description happens to match, the row key and the summary
			// key are unchanged -- so the list would rerender into the new tool
			// while an open panel kept showing the previous one's summary, status
			// and AI 日誌 link. The whole prefix, because this form does not know
			// which panels are open, and revalidating a closed one is free (its
			// query is disabled).
			Promise.all([
				queryClient.invalidateQueries({ queryKey: ["tools"] }),
				queryClient.invalidateQueries({ queryKey: ["tool-summary"] }),
			]).finally(() => {
				if (!cancelled) {
					setSettlingInstallEnd(false);
				}
			});
			return () => {
				cancelled = true;
			};
		}
	}, [installEnded, queryClient]);

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
	const busy = installMutation.isPending || jobActive || settlingInstallEnd;
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
