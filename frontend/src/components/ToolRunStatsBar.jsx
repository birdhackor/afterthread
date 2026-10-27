import { Button, Group, Text } from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { apiGet } from "../api/client.js";
import {
	statsSummaryText,
	toolRunStatsQueryKey,
} from "../utils/toolRunsPurge.js";
import { ToolRunsPurgeDialog } from "./ToolRunsPurgeDialog.jsx";

// The 工具 page header line (web-v6 §7, §9): how many run records are stored,
// how many are failures or of tools that no longer exist, and the page-wide
// 清除紀錄…. Records are never deleted automatically, so this count is the
// operator's cue that it is time to clear some.
export function ToolRunStatsBar() {
	const [target, setTarget] = useState(null);
	const { data, isError } = useQuery({
		queryKey: toolRunStatsQueryKey,
		queryFn: () => apiGet("/api/tool-runs/stats"),
	});
	if (data === undefined && !isError) {
		return null;
	}
	// Without counts (the request failed) the line says so and the purge stays
	// offered: its own preview counts what it would delete.
	return (
		<Group gap="sm" justify="space-between" wrap="wrap">
			<Text size="sm" c={data === undefined ? "red" : "dimmed"}>
				{data === undefined
					? "無法讀取工具執行紀錄的筆數"
					: statsSummaryText(data)}
			</Text>
			<Button
				size="xs"
				variant="subtle"
				color="red"
				disabled={data !== undefined && data.total === 0}
				onClick={() =>
					setTarget({
						scope: {
							outcome: "all",
							orphansOnly: (data?.orphans ?? 0) > 0,
						},
						now: new Date(),
						editable: true,
					})
				}
			>
				清除紀錄…
			</Button>
			<ToolRunsPurgeDialog target={target} onClose={() => setTarget(null)} />
		</Group>
	);
}
