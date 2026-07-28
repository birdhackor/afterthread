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
import { useCallback, useEffect, useState } from "react";
import { Controller, useForm } from "react-hook-form";
import { apiDelete, apiGet, apiPatch, apiPost } from "../api/client.js";
import { CharCounter } from "../components/CharCounter.jsx";
import { formatDate } from "../components/DateText.jsx";
import { EmptyState } from "../components/EmptyState.jsx";
import { SECTION_MAX_LENGTH } from "../constants/sections.js";
import { usePageTitle } from "../hooks/usePageTitle.js";
import { codePointLength } from "../utils/text.js";
import {
	toolDeleteNotification,
	toolDiscardNotification,
} from "../utils/toolDelete.js";
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
	acceptSummaryForVersion,
	buildDiscardRequest,
	buildRegenerateRequest,
	buildReviseRequest,
	clearLineageUnavailable,
	logLinkSearch,
	markLineageUnavailable,
	ownSummaryBusy,
	summaryErrorRevalidates,
	toolIdentityConsumers,
	toolLineageControls,
	toolSummaryKeyPrefix,
	versionWriteConflictReaction,
	writeSummaryDetailIfPresent,
} from "../utils/toolSummary.js";

// The install instructions textarea and the revise feedback textarea (D40)
// both share the backend's AI-input bound (20000 chars, the same
// _MAX_AI_INPUT_CHARS every AI free-text field carries --
// ToolInstallRequest.instructions and ToolReviseRequest.feedback are both
// `Field(min_length=1, max_length=_MAX_AI_INPUT_CHARS)`).
const AI_INPUT_MAX = SECTION_MAX_LENGTH;

const VERSION_WRITE_CODE_COPY = {
	version_mismatch: "工具版本已變更，正在重新整理最新版本",
	job_busy: "已有工具任務正在進行中，請稍後再試",
	ai_job_in_progress: "AI 任務進行中，請稍後再試",
	lineage_unavailable:
		"前一版已不存在或版本關係已損壞，無法丟掉目前版本；可改為刪除整個工具",
};

// The shared client maps ANY 404 to the item-flavored 找不到項目 copy; a tool
// mutation's 404 means the tool row itself is gone (deleted elsewhere, or the
// backend restarted with a different TOOLS_DIR), so it gets its own wording.
//
// `codeCopy` is the opt-in override for a structured code whose backend copy
// names the wrong action for the caller; anything not listed keeps the backend
// message.
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
// * `job_busy` -- 「已有工具任務正在進行中，請等待完成」 names no
//   action at all, deliberately (routers.tools: it can be raised by a job the
//   user did not start from this control), and reads correctly under BOTH the
//   revise, regenerate and discard titles. Versioned writes use the local copy
//   above to say explicitly that retrying shortly is safe.
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
// link deep-links to that record via `?log=<id>`: LlmLogsPage auto-expands the
// matching row, or -- when the record is older than the rows that page lists --
// fetches that one record by id and shows it on its own (only a record the
// backend has really dropped reports itself gone). With no id it is a plain jump.
//
// `llmLogProcess` is the id's PROVENANCE, carried along as `?logProcess=` (see
// utils/toolSummary.logLinkSearch, which owns both halves of that contract).
// The job card passes it and the summary panel does not, and the asymmetry is
// the backend's, not this component's: a job response is cached by this client
// and never refetched once the job is terminal, so its id can outlive the
// process that minted it, while the summary route re-filters its own id against
// the answering process on every read.
function LogLink({ llmLogId, llmLogProcess }) {
	return (
		<Anchor
			component={Link}
			to="/llm-logs"
			search={logLinkSearch(llmLogId, llmLogProcess)}
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
		// A 404 means the job RECORD is gone (jobs are process-local), NOT that the
		// work did not happen -- and telling the operator to resubmit was wrong
		// (R12-1). The backend applies the package change FIRST, generates the
		// summary SECOND, and marks the job done LAST, so a restart during the
		// summary step loses the record while the install or revision is already
		// live. "Resubmit" then buys a second unwanted revision of the already
		// revised tool (another multi-minute LLM run, another rewrite of content
		// the user did not ask to change) or an install that fails on a name that
		// is now taken. What we actually know is that the outcome is unknown, so
		// that is what this says, and the list has just been refetched for exactly
		// this reason -- the answer is on screen.
		const unknownOutcome =
			kind === "revise"
				? "找不到這個修訂工作，後端可能已重新啟動。修訂可能已經完成——請先看上方清單與這個工具的總結，確認之後再決定要不要重送。"
				: "找不到這個安裝工作，後端可能已重新啟動。工具可能已經安裝好了——請先看已安裝工具清單，確認之後再決定要不要重裝。";
		return (
			<Alert color="orange" title={`無法確認${verb}結果`}>
				<Text size="sm">
					{jobQuery.error?.status === 404
						? unknownOutcome
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
					<LogLink
						llmLogId={job.llm_log_id}
						llmLogProcess={job.llm_log_process}
					/>
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
				<LogLink
					llmLogId={job.llm_log_id}
					llmLogProcess={job.llm_log_process}
				/>
			</Stack>
		</Alert>
	);
}

// The AI-summary detail for one tool row (D40). Fetched lazily (enabled:
// expanded) so a collapsed row never pulls its summary body -- mirrors
// LlmLogsPage's LogDetailPanel, including that component's two-part defence
// against a reused address: the exact `current_vid` is folded into the query
// key, and the response's own `current_vid` is compared before the query
// function returns. The latter is invariant J's race closure: a name-addressed
// GET that actually read another version throws, so that payload reaches
// neither TanStack Query's cache nor the renderer.
//
// `writesBlocked` carries the reasons the version-addressed AI writes
// (重新產生, 送出修訂) must not be issued that this panel cannot see for itself:
// the panel-wide single-flight mirror, a known-stale tool list, and a just-ended
// job whose re-read has not landed (see InstalledToolsPanel's
// versionWritesBlocked for all three).
// 送出修訂 additionally answers to `displayedMayBeStale`, which is what this
// panel knows first-hand about its OWN summary query.
//
// What is deliberately NO LONGER here: an in-flight 啟用 toggle
// (`isTogglingThisTool`, R7-2) used to gate both writes as well. It was the
// mirror half of a filesystem race web-v5 P1 removed at the root -- a toggle
// writes the package's .afterthread-state.json and never rewrites tool.json, so it cannot
// move the manifest identity a revise or a regenerate is holding across its LLM
// round trip. See the enable Switch in ToolRow for the whole chain.
function ToolSummaryPanel({
	name,
	currentVid,
	summaryQueryKey,
	jobAttributionKey,
	expanded,
	writesBlocked,
	settlingJobEnd,
	isRegenerating,
	onRegenerate,
	reviseMutation,
	isSubmittingRevise,
	reviseJobId,
	reviseJobQuery,
}) {
	const { data, error, isError, isFetching } = useQuery({
		queryKey: summaryQueryKey,
		queryFn: async () =>
			acceptSummaryForVersion(
				currentVid,
				await apiGet(`/api/tools/${name}/summary`),
			),
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
		updated_at: null,
		llm_log_id: null,
	};

	// The three ways "what is on screen may not be current" can be true, named
	// once because TWO controls need the same answer: `settlingJobEnd` (a finished
	// job's re-read is still in flight), `isFetching` (any background refresh is),
	// and `isError` (the refresh FAILED and left the previous copy rendered --
	// react-query keeps `data` and only flips status, so `isFetching` is back to
	// false while the content is stale).
	//
	// Authoring feedback about stale displayed content applies instructions to an
	// implementation the user is no longer looking at. Concretely: while this row
	// was collapsed its summary
	// query was disabled, so the revalidation a finished revise fired resolved
	// without issuing a GET and cleared the settling gate anyway; re-expanding
	// then renders the CACHED pre-revise summary (data present, so no Loader)
	// while a refetch is in flight or has failed. Writing 修訂意見 about that text
	// and pressing 送出修訂 sends feedback describing the OLD implementation to a
	// builder session that will apply it to code that has already changed -- and
	// and that spends minutes of LLM time rewriting a package.
	const displayedMayBeStale = settlingJobEnd || isFetching || isError;

	const submitRevise = handleSubmit((values) => {
		if (writesBlocked || displayedMayBeStale) {
			return;
		}
		const feedback = values.feedback.trim();
		// Cleared only once the job is actually QUEUED (202), mirroring
		// InstallPanel's own form: the mutation resolving is not "AI is done" for
		// a job-shaped action, just "the request landed" -- see the job progress
		// card below for the part that takes minutes.
		reviseMutation.mutate(
			{ name, currentVid, jobAttributionKey, feedback },
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
			    still shown because it is the best available reading, but it is now
			    labelled. */}
			{isError ? (
				<Alert color="orange" title="無法更新總結">
					<Text size="sm">
						{toolErrorMessage(error, "請稍後再試")}
						。以下是先前讀到的內容，可能已過期。
					</Text>
				</Alert>
			) : null}

			<Group gap="xs" wrap="wrap">
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
					// NOT gated by an in-flight 啟用 toggle any more, and the gate that
					// stood here is worth naming because its reasoning was sound until P1
					// (R7-2): POST .../summary/regenerate resolves the package and
					// captures its manifest identity up front
					// (tool_meta.regenerate_summary -> _resolve_package), then awaits a
					// whole LLM round trip before the sidecar write re-checks that
					// identity -- and PATCH /api/tools/{name} used to rewrite tool.json in
					// place to flip `enabled`, MOVING it, so a toggle landing inside the
					// round trip turned a finished generation into 404「工具不存在」.
					// web-v5 P1 moved the toggle into the package's own .afterthread-state.json:
					// tools.set_enabled never opens tool.json, so the identity
					// tool_meta._store_meta re-checks (tools._write_package_file_atomic ->
					// _still_the_expected_package) cannot be moved by a switch at all.
					// Nothing is left for that term to prevent.
					disabled={writesBlocked}
					onClick={onRegenerate}
				>
					重新產生
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
									// Gated by `displayedMayBeStale` for the same reason the submit
									// below is: feedback is written ABOUT the summary rendered above
									// it, so it must not be authored against content already known to
									// be superseded. Disabling the FIELD and not only the button is
									// deliberate -- letting someone type a paragraph and only then
									// discover the button is dead is a worse version of the same
									// refusal.
									disabled={writesBlocked || displayedMayBeStale}
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
							// The `isTogglingThisTool` term that used to be here is gone,
							// and it is the same retirement as 重新產生's above. It was the
							// reverse half of the enable switch's own revise gate (see
							// ToolRow) and it guarded a real filesystem race: a revise
							// session pins the package's identity as
							// `(st_dev, st_ino, st_ctime_ns)` of its tool.json at start and
							// re-checks it immediately before the swap
							// (tool_builder._package_identity / run_revise), while PATCH
							// /api/tools/{name} used to REWRITE that same tool.json in place
							// to flip `enabled` -- so a toggle landing anywhere inside a
							// revise moved the manifest's ctime and the whole multi-minute
							// build was discarded with 「原工具在修訂期間被改動或重新安裝」.
							// Since web-v5 P1 tools.set_enabled writes only the package's
							// .afterthread-state.json, the pre-swap check reads the same identity it
							// recorded, and the toggle itself is either carried across the
							// swap (tools.carry_package_state, the first statement of the
							// locked tail) or lands on the already-published package -- both
							// halves under tools._STATE_PUBLISH_LOCK, so neither can be lost.
							//
							// `displayedMayBeStale` gates this action because it reasons
							// from what is on screen: this submit
							// sends feedback the user wrote ABOUT the summary above it, so
							// issuing it while that text is known to be superseded hands a
							// builder session instructions for an implementation that no
							// longer exists. See `displayedMayBeStale` for the rule.
							disabled={writesBlocked || displayedMayBeStale}
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

// One installed tool row: name, validity badge, description, the enable switch,
// delete, and an inline-expandable AI-summary
// panel (D40). The switch is disabled for an invalid package on purpose -- a
// broken package is never advertised/executable regardless of its flag
// (backend contract), so offering the toggle would suggest a state change
// that cannot have any effect; delete is the meaningful action.
function ToolRow({
	tool,
	identity,
	onToggle,
	onDelete,
	onDiscard,
	lineageUnavailable,
	mutating,
	writesBlocked,
	settlingJobEnd,
	isRegenerating,
	onRegenerate,
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
	const lineageControls = toolLineageControls(tool, lineageUnavailable);

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
						</Group>
						{lineageControls.unresolved ? (
							<Text size="xs" c="red">
								{tool.error ?? "目前沒有可用的工具版本"}
							</Text>
						) : (
							<>
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
							</>
						)}
					</Stack>
					<Group gap="sm" wrap="nowrap">
						{lineageControls.showVersionControls ? (
							<Switch
								size="sm"
								label="啟用"
								labelPosition="left"
								checked={tool.enabled}
								// TWO terms, and what is NOT here is the point (web-v5 P1R2-2).
								// This switch used to be disabled for the whole of a revise
								// (`reviseBusyForThisTool`) and of a regenerate
								// (`isRegenerating`), because PATCH /api/tools/{name} rewrote
								// THIS package's tool.json in place to flip `enabled` -- and
								// tool.json's (st_dev, st_ino, st_ctime_ns) is exactly the
								// identity a revise records at start and re-checks before the
								// swap (tool_builder._package_identity), and the one a
								// regenerate holds across its LLM round trip. A toggle therefore
								// doomed either one. web-v5 P1 moved `enabled` into the
								// package's own .afterthread-state.json and tools.set_enabled never opens
								// the manifest, so that identity cannot move; the revise's own
								// tail then either CARRIES a toggle across the swap
								// (tools.carry_package_state, read from the live package as the
								// first statement of the tail) or -- both being serialized by
								// tools._STATE_PUBLISH_LOCK, whose hold set_enabled joins AFTER
								// resolving the name -- takes it on the package the swap just
								// published. Either way it is honoured, so holding the operator
								// away from the switch for the minutes a revise runs would
								// prevent nothing: an operator who decides mid-revise that a
								// tool must be off can now say so.
								//
								// The two that stay are not identity arguments and do not
								// answer to that change: `!tool.valid` (see this component's
								// own comment above), and `mutating` because a PATCH or DELETE
								// is already on the wire -- a double-fire question, not an
								// identity one.
								//
								// 刪除 is deliberately NOT gated by a revise either: deleting
								// mid-revise is answered by the backend's own target-missing
								// refusal, it is what a user who has given up on the tool
								// actually wants, and it runs through its own confirm modal.
								disabled={!tool.valid || mutating}
								onChange={(event) =>
									onToggle(tool.name, event.currentTarget.checked)
								}
							/>
						) : null}
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

				{lineageControls.discardAction === "delete-tool" ? (
					<Group gap="sm" align="center">
						<Text size="xs" c="dimmed">
							目前只有這一版；丟掉後會刪除整個工具。
						</Text>
						<Button
							size="xs"
							variant="light"
							color="red"
							disabled={mutating}
							onClick={() => onDelete(tool.name)}
						>
							丟掉這一版
						</Button>
					</Group>
				) : null}

				{lineageControls.discardAction === "discard-version" ? (
					<Group gap="sm" align="center">
						<Text size="xs" c="dimmed">
							可丟掉目前版本，回到前一版。
						</Text>
						<Button
							size="xs"
							variant="light"
							color="orange"
							disabled={mutating || writesBlocked}
							onClick={() => onDiscard(tool, identity)}
						>
							丟掉這一版
						</Button>
					</Group>
				) : null}

				{lineageControls.showBrokenLineage ? (
					<Alert color="orange" title="無法退回前一版">
						<Text size="sm">
							前一版不存在、無效或指回目前版本，不能丟掉這一版。此狀態不會自行修復；若不再使用，請用右上角的「刪除」移除整個工具。
						</Text>
					</Alert>
				) : null}

				{lineageControls.showVersionControls ? (
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
					    taking the summary GET, 重新產生 and the whole revise form
					    down with it. Nothing in this project could catch that: a wrong
					    prop NAME is valid JS, valid JSX, and valid to Biome, and there
					    is no jsdom to render against (see the P4 addendum in
					    docs/web-v4-decisions.md). */}
						<Collapse expanded={expanded}>
							<ToolSummaryPanel
								name={tool.name}
								currentVid={tool.current_vid}
								summaryQueryKey={identity.summaryQueryKey}
								jobAttributionKey={identity.jobAttributionKey}
								expanded={expanded}
								writesBlocked={writesBlocked}
								settlingJobEnd={settlingJobEnd}
								isRegenerating={isRegenerating}
								onRegenerate={onRegenerate}
								reviseMutation={reviseMutation}
								isSubmittingRevise={isSubmittingRevise}
								reviseJobId={reviseJobId}
								reviseJobQuery={reviseJobQuery}
							/>
						</Collapse>
					</div>
				) : null}
			</Stack>
		</Card>
	);
}

// The 已安裝工具 tab: list + enable toggle + delete (confirm modal) + each
// row's AI-summary panel (D40: regenerate / revise).
function InstalledToolsPanel({ externalBusy = false, onBusyChange }) {
	const queryClient = useQueryClient();
	const [deleteTarget, setDeleteTarget] = useState(null);
	const [discardTarget, setDiscardTarget] = useState(null);
	// A late lineage_unavailable means the row's previously "usable" lineage was
	// hand-broken after the list read. It will not heal by refetching, so remember
	// every independently proven instance locally and render those controls as
	// `broken`; one tool's later 409 must not erase another tool's evidence.
	const [lineageUnavailableKeys, setLineageUnavailableKeys] = useState(
		() => new Set(),
	);
	// The one revise job this panel is currently tracking, and which tool
	// INSTANCE it belongs to (the canonical job attribution key captured at
	// submit time -- see activeJobKey). Mirrors InstallPanel's single `jobId` for the same
	// reason: the backend's single-flight (D40) admits only ONE install/revise
	// job at a time across every tool, so there is never more than one to track.
	const [activeJob, setActiveJob] = useState(null); // { name, jobAttributionKey, jobId } | null
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
		onSuccess: async (data, name) => {
			setDeleteTarget(null);
			notifications.show(toolDeleteNotification(name, data));
			// removeQueries, not invalidateQueries: the tool is GONE, so there is
			// nothing left to revalidate against -- and a same-name reinstall
			// inside TanStack Query's default 5-minute gc window is a DIFFERENT
			// tool, whose panel must start cold rather than pre-populated with the
			// deleted tool's summary/llm_log_id (or, if the reinstall's own
			// refetch then transiently fails, with the deleted tool's stale data
			// rendered as though it belonged to the new one).
			//
			// PREFIX filter, and no `exact`: the summary key carries a third
			// element (the canonical instance key), so
			// `exact: true` would have matched NOTHING and silently stopped
			// clearing anything at all. TanStack Query's default element-wise
			// partial match is what is wanted here anyway -- it drops EVERY cached
			// instance of this name, including entries left by earlier versions --
			// and it still cannot reach another tool, because the
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

	// The four structured 409 codes are instructions with intentionally
	// different side effects. In particular, job_busy and lineage_unavailable
	// must never fall through to the old broad refetch path.
	const reactToVersionWriteConflict = (mutationError, instanceKey = null) => {
		const reaction = versionWriteConflictReaction(mutationError ?? {});
		if (reaction === "refresh") {
			// The list response carries the new current_vid. Once it lands,
			// toolIdentityConsumers changes the row key and React drops every piece
			// of row-local state, including unsent revise feedback.
			queryClient.invalidateQueries({ queryKey: ["tools"], exact: true });
		} else if (reaction === "delete-tool" && instanceKey !== null) {
			// No refetch: a broken previous pointer is stable filesystem state.
			setLineageUnavailableKeys((current) =>
				markLineageUnavailable(current, instanceKey),
			);
		}
		return reaction;
	};

	const discardMutation = useMutation({
		mutationFn: ({ name, currentVid }) => {
			const request = buildDiscardRequest({ name, currentVid });
			return apiDelete(request.path);
		},
		onSuccess: async (data, variables) => {
			setDiscardTarget(null);
			setLineageUnavailableKeys((current) =>
				clearLineageUnavailable(current, variables.jobAttributionKey),
			);
			// V is no longer current and may already be removed. Clear every
			// version of this name; P will fetch into its own current_vid key.
			queryClient.removeQueries({
				queryKey: toolSummaryKeyPrefix(variables.name),
			});
			await queryClient.invalidateQueries({
				queryKey: ["tools"],
				exact: true,
			});
			notifications.show(toolDiscardNotification(variables.name, data));
		},
		onError: (mutationError, variables) => {
			const reaction = reactToVersionWriteConflict(
				mutationError,
				variables.jobAttributionKey,
			);
			if (reaction !== "retry") {
				// job_busy deliberately keeps this lightweight confirmation open so
				// the operator can retry without reconstructing the action.
				setDiscardTarget(null);
			}
			if (reaction === null && summaryErrorRevalidates(mutationError ?? {})) {
				queryClient.invalidateQueries({ queryKey: ["tools"], exact: true });
			}
			notifications.show({
				color: "red",
				title: "無法丟掉這一版",
				message: toolErrorMessage(
					mutationError,
					"無法丟掉目前版本",
					VERSION_WRITE_CODE_COPY,
				),
			});
		},
	});

	// Regenerate and revise are lifted to this panel so one row's activity can
	// disable the other rows against the backend's global tool-job slot.

	// Re-read both the detail and list after a revise job settles. The detail uses
	// a name prefix because the revision creates a new current_vid.
	const revalidateSummaryAndList = useCallback(
		(name) =>
			Promise.all([
				queryClient.invalidateQueries({ queryKey: toolSummaryKeyPrefix(name) }),
				queryClient.invalidateQueries({ queryKey: ["tools"] }),
			]),
		[queryClient],
	);

	// A 404 says this name no longer resolves, so the old broad summary/list
	// revalidation remains useful. Structured 409s never enter this helper:
	// reactToVersionWriteConflict gives each one its own reaction.
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
		mutationFn: ({ name, currentVid }) => {
			const request = buildRegenerateRequest({ name, currentVid });
			return apiPost(request.path, request.body);
		},
		onSuccess: async (detail, variables) => {
			const summaryKey = variables.summaryQueryKey;
			// Cancel an older GET before writing the authoritative mutation
			// response, or that GET could land afterwards and restore old text.
			await queryClient.cancelQueries({ queryKey: summaryKey });
			// Do not recreate an entry a concurrent delete removed.
			queryClient.setQueryData(summaryKey, writeSummaryDetailIfPresent(detail));
			notifications.show({
				color: "green",
				message: `已重新產生「${variables.name}」的總結`,
			});
		},
		onError: (mutationError, variables) => {
			const reaction = reactToVersionWriteConflict(
				mutationError,
				variables.jobAttributionKey,
			);
			notifications.show({
				color: "red",
				title: "重新產生失敗",
				message: toolErrorMessage(
					mutationError,
					"無法重新產生總結",
					VERSION_WRITE_CODE_COPY,
				),
			});
			if (reaction === null) {
				revalidateAfterSummaryError(mutationError, variables.name);
			}
		},
	});

	const reviseMutation = useMutation({
		mutationFn: ({ name, currentVid, feedback }) => {
			const request = buildReviseRequest({ name, currentVid, feedback });
			return apiPost(request.path, request.body);
		},
		onSuccess: (result, { name, jobAttributionKey }) => {
			// Captured from toolIdentityConsumers at submit time, so the card stays
			// with V even if the same name now points at P.
			setActiveJob({ name, jobAttributionKey, jobId: result.job_id });
		},
		onError: (mutationError, variables) => {
			const reaction = reactToVersionWriteConflict(
				mutationError,
				variables.jobAttributionKey,
			);
			notifications.show({
				color: "red",
				title: "無法送出修訂",
				message: toolErrorMessage(
					mutationError,
					"無法送出修訂",
					VERSION_WRITE_CODE_COPY,
				),
			});
			if (reaction === null) {
				revalidateAfterSummaryError(mutationError, variables.name);
			}
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

	// A settled revise means the row's own summary detail and the list may both be
	// stale -- on SUCCESS because the job
	// regenerated the sidecar and replaced the package (mirrors InstallPanel's own
	// transition effect, for the same reason: a row's data the OTHER tab's query
	// owns), and on FAILURE because most of the ways a revise can fail ARE the
	// server saying it is no longer what we are showing.
	//
	// FAILURE is included deliberately, and it is a wider rule than the code-by-
	// code one revalidateAfterSummaryError applies to the immediate refusals. The
	// reason is that the job payload carries no structured cause: ToolJobStatus is
	// `{job_id, state, created_at, finished_at, error, tool_name, summary,
	// llm_log_id, llm_log_process}` (backend schemas.py) -- `error` is friendly
	// zh-TW prose the backend rewords every review round, so matching on it would
	// be a gate that silently opens. What CAN be said precisely is the failure
	// vocabulary: of tool_builder's revise outcomes, 找不到要修訂的工具 / 原工具已被刪除 /
	// 原工具目錄已被替換為連結 / 原工具在修訂期間被改動或重新安裝 /
	// 無法確認原工具的內容 all assert a
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
	// Derived from WHICH job we have already revalidated for, not from a boolean a
	//ffect sets and a finally clears (R9-1/R9-2). That shape had two defects and
	// both are structural, not slips: it is false for one render after the job
	// turns terminal (the effect has not run yet), which is a frame in which the
	// gate is open over pre-job data; and if a NEW job starts before the old
	// revalidation settles, the cleanup suppresses the clear and nothing else ever
	// runs it, so the flag sticks true forever. Deriving it cannot do either --
	// it is true from the very render the ending appears, and a new job id makes
	// it false immediately because that job has not ended yet.
	const [settledJobId, setSettledJobId] = useState(null);
	const settlingJobEnd =
		activeJob !== null && jobEnded && settledJobId !== activeJob.jobId;
	useEffect(() => {
		if (!(activeJob && jobEnded) || settledJobId === activeJob.jobId) {
			return;
		}
		const endedJobId = activeJob.jobId;
		// Prefix filter (partial match), so it reaches this tool's entry whatever
		// current_vid it is keyed under -- which matters precisely here because a
		// successful revise publishes a new version.
		revalidateSummaryAndList(activeJob.name).finally(() => {
			// Record the id rather than clearing a flag: an unmount or a newer job
			// cannot leave this stuck, and a stale settle for an older job simply
			// records an id nothing is comparing against any more.
			setSettledJobId(endedJobId);
		});
	}, [activeJob, jobEnded, settledJobId, revalidateSummaryAndList]);

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
	// authority, and the 409 (job_busy) this gate is trying to
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
	// TWO values, not one, and the split is what stops the mirror from echoing.
	// `ownBusy` is what this panel knows FIRST-HAND and is the only thing it
	// reports upward (see ownSummaryBusy); `summaryBusy` additionally honours the
	// other tab's flag and is the single-flight half of the local write gate (the
	// other half is staleList -- see versionWritesBlocked, which is what actually
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
	const mutating =
		toggleMutation.isPending ||
		deleteMutation.isPending ||
		discardMutation.isPending;

	// THE gate for all version-specific writes -- 重新產生, 送出修訂 and 丟掉這一版.
	// One value is passed down so a control cannot be gated on only half of the
	// reasons. The backend's expected_vid check is the authority; this keeps the
	// operator from authoring or confirming against a list already known to be
	// stale, and holds a version_mismatch closed until its refetch lands.
	//
	// `staleList` is in here because a warning is not a guarantee. r3 added the
	// orange 「無法更新工具清單」 banner for the state where a background
	// GET /api/tools fails and the rows stay on screen; the banner explained the
	// hazard and then let the user act on it anyway. Concretely: unsent feedback
	// typed for tool A, a same-name reinstall as B in another tab, GET /api/tools
	// failing. The row keeps A's identity (nothing told it otherwise), so it is
	// not remounted and the draft survives. expected_vid prevents the backend
	// from spending work on B, but the only honest UI while it cannot identify
	// the current version is still to stop offering the write.
	//
	// Reading is NOT gated: expanding a panel and its GET .../summary write
	// nothing, and the panel's own banner already labels what it shows.
	// 啟用/停用 and whole-tool 刪除 stay outside:
	// both are name-addressed by intent ("the tool called X"), both are
	// reversible or confirmed, and neither carries content authored against one
	// specific instance the way a revise draft does.
	const versionWritesBlocked =
		summaryBusy || staleList || isFetching || settlingJobEnd;

	// The INSTANCE the tracked revise job was submitted against -- the same
	// identity string the rows are keyed by, captured from the same
	// toolIdentityConsumers result at submit time. Association by NAME alone
	// would follow the name from V to P after a discard.
	const activeJobKey = activeJob?.jobAttributionKey ?? null;
	// ...and if NO row owns it, it is shown at panel level instead (below), so a
	// job can never become invisible while it is still holding the busy gate.
	const orphanedJob =
		activeJob !== null &&
		!tools.some(
			(tool) =>
				toolIdentityConsumers(tool.name, tool.current_vid).jobAttributionKey ===
				activeJobKey,
		);
	const discardTargetIsCurrent =
		discardTarget === null ||
		tools.some(
			(tool) =>
				toolIdentityConsumers(tool.name, tool.current_vid).jobAttributionKey ===
				discardTarget.jobAttributionKey,
		);
	useEffect(() => {
		if (discardTarget !== null && !discardTargetIsCurrent) {
			// A list refresh changed this row's current_vid while its lightweight
			// confirmation was open. Close it in the same render it stops matching;
			// the operator must confirm against the newly mounted row instead.
			setDiscardTarget(null);
		}
	}, [discardTarget, discardTargetIsCurrent]);

	return (
		<Stack gap="md">
			<Group justify="flex-end">
				<Button
					variant="light"
					loading={isFetching}
					onClick={() => {
						// Refresh means refresh EVERYTHING on screen, not just the list
						// (R5-3). The current_vid key prevents cross-version reuse, while
						// the name prefix still reaches every expanded version entry.
						setLineageUnavailableKeys(new Set());
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

			    The summary query now rejects a response whose current_vid differs
			    from its row, so a successful name-addressed GET cannot mix a new
			    version into an old cache key. The banner still matters for the
			    opposite direction: the list itself can remain on an older successful
			    result after its background refetch fails.

			    The banner also has to SAY that the version-specific actions are
			    inert, because they now are (see versionWritesBlocked): a banner that
			    described a hazard and left the buttons live was the guarantee r3
			    claimed to have established, minus the enforcement. */}
			{staleList ? (
				<Alert color="orange" title="無法更新工具清單">
					<Text size="sm">
						{error?.message ?? "請稍後再試"}
						。以下清單是先前讀到的內容，可能已過期（工具可能已被刪除或重新安裝），展開的
						AI
						總結則可能來自更新後的工具。在清單重新讀取成功前，「重新產生」、「送出修訂」與「丟掉這一版」已暫時停用；後端會以版本編號拒絕過期操作，畫面先停止送出，避免反覆失敗。
					</Text>
				</Alert>
			) : null}

			{/* A tracked revise job whose row is no longer in the list -- deleted,
			    replaced, discarded, or successfully promoted to a new version. The progress card
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
						」的 AI
						修訂進度（送出時的版本已不是目前版本，或工具已被刪除；不再對應下方任何一列）
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
				// One call yields all three surviving invariant-H consumers. No
				// component call site spells name + current_vid for itself.
				const identity = toolIdentityConsumers(tool.name, tool.current_vid);
				const rowKey = identity.rowKey;
				const isSubmittingRevise =
					reviseMutation.isPending &&
					reviseMutation.variables?.name === tool.name;
				// The two per-row values that used to live here -- "a revise is
				// touching THIS package's files" and "a PATCH is already on the wire
				// for THIS tool" -- are gone with the gates they fed (web-v5 P1R2-2):
				// both existed only to keep a toggle and an AI write apart while a
				// toggle rewrote tool.json, and it no longer does. Nothing else read
				// them, so keeping either would be a dead binding rather than a
				// smaller lock. See the enable Switch for the backend mechanism that
				// replaced them.
				return (
					<ToolRow
						// A discard from V to P changes current_vid even when description
						// is byte-identical, remounting this form before feedback written
						// for V can be submitted against P.
						key={rowKey}
						tool={tool}
						identity={identity}
						mutating={mutating}
						onToggle={(name, enabled) =>
							toggleMutation.mutate({ name, enabled })
						}
						onDelete={(name) => setDeleteTarget(name)}
						onDiscard={(targetTool, targetIdentity) =>
							setDiscardTarget({
								name: targetTool.name,
								currentVid: targetTool.current_vid,
								jobAttributionKey: targetIdentity.jobAttributionKey,
							})
						}
						lineageUnavailable={lineageUnavailableKeys.has(rowKey)}
						writesBlocked={versionWritesBlocked}
						settlingJobEnd={settlingJobEnd}
						isRegenerating={
							regenerateMutation.isPending &&
							regenerateMutation.variables?.name === tool.name
						}
						// Re-checked HERE and not only on the button's `disabled`, for the
						// same reason submitRevise re-checks it inside the panel: a
						// disabled prop is a rendering, and the two AI writes must be
						// impossible to issue while the gate holds, not merely awkward.
						// The re-check follows the gate, in both directions: when
						// `isTogglingThisTool` left this button's `disabled` (web-v5
						// P1R2-2) it left this line with it, or the guard would have
						// outlived the rendering it exists to mirror.
						onRegenerate={() => {
							if (versionWritesBlocked) {
								return;
							}
							regenerateMutation.mutate({
								name: tool.name,
								currentVid: tool.current_vid,
								summaryQueryKey: identity.summaryQueryKey,
								jobAttributionKey: identity.jobAttributionKey,
							});
						}}
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
				opened={discardTarget !== null && discardTargetIsCurrent}
				onClose={() => setDiscardTarget(null)}
				title="退回前一版"
				centered
				closeOnEscape={!discardMutation.isPending}
				closeOnClickOutside={!discardMutation.isPending}
				withCloseButton={!discardMutation.isPending}
			>
				<Stack gap="md">
					<Text>
						確定要丟掉「{discardTarget?.name}
						」目前這一版並退回前一版嗎？目前版本會停止使用；只有在系統能確認沒有版本執行中時才會刪除檔案，否則會暫留在磁碟。前一版的內容與總結會重新顯示。
					</Text>
					<Group justify="flex-end">
						<Button
							variant="default"
							onClick={() => setDiscardTarget(null)}
							disabled={discardMutation.isPending}
						>
							取消
						</Button>
						<Button
							color="orange"
							loading={discardMutation.isPending}
							disabled={versionWritesBlocked || !discardTargetIsCurrent}
							onClick={() => {
								if (
									discardTarget !== null &&
									discardTargetIsCurrent &&
									!versionWritesBlocked
								) {
									discardMutation.mutate(discardTarget);
								}
							}}
						>
							丟掉並退回
						</Button>
					</Group>
				</Stack>
			</Modal>

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
						確定要將「{deleteTarget}
						」從工具清單移除嗎？工具會立即停止出現在清單與 AI
						可用工具中。若系統能確認所有版本都未在執行，會刪除整個工具目錄；若無法確認，目錄（含設定與金鑰檔）會保留在磁碟，完成後會顯示保留路徑供你手動處理。
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
			// mechanism used by other name-addressed mutation conflicts): the backend's
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
	// Derived, for the reasons spelled out on the revise side's settledJobId
	// (R9-1/R9-2): a boolean set in an effect is open for one render and can stick
	// true forever if a new job starts before the old revalidation settles -- and
	// on THIS side "forever" meant the other tab's AI controls stayed locked until
	// a page reload, because a second install that FAILS does not even match
	// installEnded, so nothing would ever have cleared it.
	const [settledInstallJobId, setSettledInstallJobId] = useState(null);
	const settlingInstallEnd =
		jobId !== null && installEnded && settledInstallJobId !== jobId;
	useEffect(() => {
		if (installEnded && jobId !== null && settledInstallJobId !== jobId) {
			const endedJobId = jobId;
			// The summary prefix too (R7-3): this form does not know which
			// version-keyed panels are open. The new current_vid remounts the row,
			// while the broad invalidation also retires any still-observed old
			// entry; revalidating a closed one is free because its query is disabled.
			Promise.all([
				queryClient.invalidateQueries({ queryKey: ["tools"] }),
				queryClient.invalidateQueries({ queryKey: ["tool-summary"] }),
			]).finally(() => {
				setSettledInstallJobId(endedJobId);
			});
		}
	}, [installEnded, jobId, settledInstallJobId, queryClient]);

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

	// `settlingInstallEnd` is in here too, not only in the reported `busy` (R9-2):
	// a second install started during the first one's revalidation window would
	// have raced the very refetch that makes the new row appear, and under the old
	// sticky-flag shape it also stranded the other tab's controls permanently.
	const fieldsDisabled =
		installMutation.isPending ||
		jobActive ||
		settlingInstallEnd ||
		externalBusy;

	const submit = handleSubmit((values) => {
		if (
			installMutation.isPending ||
			jobActive ||
			settlingInstallEnd ||
			externalBusy
		) {
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
