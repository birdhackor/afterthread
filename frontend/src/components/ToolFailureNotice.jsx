import { Alert, Anchor, Button, Group, Stack, Text } from "@mantine/core";
import { useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useState } from "react";
import { apiGet, apiPost } from "../api/client.js";
import {
	failureNoticeView,
	requestFix,
	toolFailureGroups,
	toolFailureText,
} from "../utils/toolFailures.js";
import { readToolList } from "../utils/toolList.js";

// The tool failures of one AI result (web-v6 §8.2), shown beside it so a
// failure is seen where it happened rather than discovered later on the 工具
// page. One reminder per tool, each with 請 AI 修正 when its failures were
// recorded: the fix is a revise of that tool attaching them, judged at the
// click against a fresh tool list, because this page cannot know the tool's
// current version, or whether it has a draft. A tool with a draft gets no fix
// from here, only the words and the way to the 工具 page (toolFailures.requestFix).
// Once started, the job is followed on the 工具 page (/tools?job=<id>).
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
		setState(
			await requestFix(group, {
				// The same reader as the 工具 page's list, so the cache entry they
				// share always says which read produced it (toolList.readToolList).
				readTools: async () =>
					(
						await queryClient.fetchQuery({
							queryKey: ["tools"],
							queryFn: () => readToolList(apiGet),
							staleTime: 0,
						})
					).tools,
				post: apiPost,
			}),
		);
	};

	const view = failureNoticeView(group, state);
	return (
		<Alert color="orange" title="工具執行失敗">
			<Stack gap="xs" align="flex-start">
				<Text size="sm">{toolFailureText(group)}</Text>
				<Group gap={view.started === null ? "sm" : "xs"}>
					{view.started === null ? null : <Text size="sm">{view.started}</Text>}
					{view.fix === null ? null : (
						<Button
							size="xs"
							color="red"
							variant="light"
							loading={view.fix.checking}
							onClick={fix}
						>
							請 AI 修正
						</Button>
					)}
					<Anchor
						component={Link}
						to="/tools"
						search={view.link.search}
						size="sm"
					>
						{view.link.label}
					</Anchor>
				</Group>
				{view.refusal === null ? null : (
					<Text size="sm" c="red">
						{view.refusal}
					</Text>
				)}
			</Stack>
		</Alert>
	);
}
