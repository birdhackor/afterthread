import { Alert, Anchor, Button, Group, Stack, Text } from "@mantine/core";
import { useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useState } from "react";
import { apiGet, apiPost } from "../api/client.js";
import {
	fixDecision,
	fixRefusalMessage,
	toolFailureGroups,
	toolFailureText,
} from "../utils/toolFailures.js";

// The tool failures of one AI result (web-v6 §8.2), shown beside it so a
// failure is seen where it happened rather than discovered later on the 工具
// page. One reminder per tool, each with 請 AI 修正 when its failures were
// recorded: the fix is a revise of that tool attaching them, judged at the
// click against a fresh tool list, because this page cannot know the tool's
// current version. Once started, the job is followed on the 工具 page
// (/tools?job=<id>).
export function ToolFailureNotice({ failures }) {
	const groups = toolFailureGroups(failures);
	if (groups.length === 0) {
		return null;
	}
	return (
		<Stack gap="xs">
			{groups.map((group) => (
				<FailureGroup key={group.toolName} group={group} />
			))}
		</Stack>
	);
}

function FailureGroup({ group }) {
	const queryClient = useQueryClient();
	// idle | checking | started (jobId) | refused (message)
	const [state, setState] = useState({ kind: "idle" });

	const fix = async () => {
		setState({ kind: "checking" });
		// One re-read on version_mismatch: the list this decision came from may
		// predate a revise that just published; a second mismatch is reported.
		for (let attempt = 0; attempt < 2; attempt += 1) {
			let tools;
			try {
				tools = (
					await queryClient.fetchQuery({
						queryKey: ["tools"],
						queryFn: () => apiGet("/api/tools"),
						staleTime: 0,
					})
				).tools;
			} catch (error) {
				setState({ kind: "refused", message: fixRefusalMessage(error) });
				return;
			}
			const decision = fixDecision(group, tools);
			if (decision.kind !== "submit") {
				setState({ kind: "refused", message: decision.message });
				return;
			}
			try {
				const accepted = await apiPost(`/api/tools/${group.toolName}/revise`, {
					run_ids: group.runIds,
					expected_vid: decision.expectedVid,
				});
				setState({ kind: "started", jobId: accepted.job_id });
				return;
			} catch (error) {
				const retry =
					attempt === 0 &&
					error?.status === 409 &&
					error?.code === "version_mismatch";
				if (!retry) {
					setState({ kind: "refused", message: fixRefusalMessage(error) });
					return;
				}
			}
		}
	};

	const offerFix = group.runIds.length > 0;
	return (
		<Alert color="orange" title="工具執行失敗">
			<Stack gap="xs" align="flex-start">
				<Text size="sm">{toolFailureText(group)}</Text>
				{state.kind === "started" ? (
					<Group gap="xs">
						<Text size="sm">已請 AI 修正「{group.toolName}」。</Text>
						<Anchor
							component={Link}
							to="/tools"
							search={{ job: state.jobId }}
							size="sm"
						>
							到工具頁看進度
						</Anchor>
					</Group>
				) : (
					<Group gap="sm">
						{offerFix ? (
							<Button
								size="xs"
								color="red"
								variant="light"
								loading={state.kind === "checking"}
								onClick={fix}
							>
								請 AI 修正
							</Button>
						) : null}
						<Anchor component={Link} to="/tools" size="sm">
							到工具頁查看
						</Anchor>
					</Group>
				)}
				{state.kind === "refused" ? (
					<Text size="sm" c="red">
						{state.message}
					</Text>
				) : null}
			</Stack>
		</Alert>
	);
}
