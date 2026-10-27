import {
	Alert,
	Button,
	Checkbox,
	Group,
	Loader,
	Modal,
	Select,
	Stack,
	Table,
	Text,
} from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { apiPost } from "../api/client.js";
import {
	PURGE_AGE_OPTIONS,
	PURGE_OUTCOME_OPTIONS,
	purgePreviewText,
	purgeRequestBody,
	purgeResultMessage,
	purgeScopeLines,
	toolRunStatsQueryKey,
} from "../utils/toolRunsPurge.js";

// The purge dialog (web-v6 §7): preview, then delete exactly what the preview
// counted. `target` is { scope, now, editable } or null (closed); `now` is taken
// by the opener, once, so every preview and the confirmation share one cutoff.
// An editable target (the page-wide 清除紀錄…) shows the scope controls; a
// fixed one (a tool's own 清除紀錄 menu) shows only what it will remove.
//
// The confirmation carries the preview's max_id, so a record written after the
// operator read the count -- another tab's tool call failing meanwhile -- is
// never removed with it.
export function ToolRunsPurgeDialog({ target, onClose }) {
	return (
		<Modal
			opened={target !== null}
			onClose={onClose}
			title="清除執行紀錄"
			centered
		>
			{target !== null ? (
				// Keyed by the opening instant: every open starts from its own scope
				// and an empty mutation, never from the previous dialog's state.
				<PurgeBody
					key={target.now.getTime()}
					target={target}
					onClose={onClose}
				/>
			) : null}
		</Modal>
	);
}

function PurgeBody({ target, onClose }) {
	const queryClient = useQueryClient();
	const [scope, setScope] = useState(target.scope);
	const body = purgeRequestBody(scope, target.now);
	const preview = useQuery({
		queryKey: ["tool-run-purge-preview", body],
		queryFn: () => apiPost("/api/tool-runs/purge", { ...body, dry_run: true }),
		// The number on screen is the number confirmed: no background refetch
		// may change it (and its max_id) while the operator decides.
		staleTime: Number.POSITIVE_INFINITY,
		gcTime: 0,
		refetchOnWindowFocus: false,
	});
	const purge = useMutation({
		mutationFn: () =>
			apiPost("/api/tool-runs/purge", {
				...body,
				dry_run: false,
				max_id: preview.data.max_id,
			}),
		onSuccess: async (result) => {
			notifications.show({
				color: "green",
				message: purgeResultMessage(result.deleted),
			});
			onClose();
			// Any tool's list, last_run and the header counts may have changed.
			await Promise.all([
				queryClient.invalidateQueries({ queryKey: ["tool-runs"] }),
				queryClient.invalidateQueries({ queryKey: ["tools"] }),
				queryClient.invalidateQueries({ queryKey: toolRunStatsQueryKey }),
			]);
		},
		onError: (error) => {
			notifications.show({
				color: "red",
				title: "清除失敗",
				message: error?.message ?? "無法清除執行紀錄",
			});
		},
	});

	const matched = preview.data?.matched ?? 0;
	return (
		<Stack gap="sm">
			{target.editable ? (
				<Stack gap="xs">
					<Select
						label="結果"
						data={PURGE_OUTCOME_OPTIONS}
						value={scope.outcome ?? "all"}
						allowDeselect={false}
						onChange={(value) => setScope({ ...scope, outcome: value })}
					/>
					<Select
						label="時間"
						data={PURGE_AGE_OPTIONS}
						value={scope.olderThanDays ? String(scope.olderThanDays) : ""}
						allowDeselect={false}
						onChange={(value) =>
							setScope({
								...scope,
								olderThanDays: value ? Number(value) : null,
							})
						}
					/>
					<Checkbox
						label="只清已刪除的工具留下的紀錄"
						checked={Boolean(scope.orphansOnly)}
						onChange={(event) =>
							setScope({ ...scope, orphansOnly: event.currentTarget.checked })
						}
					/>
				</Stack>
			) : (
				<Table withTableBorder={false} verticalSpacing={2}>
					<Table.Tbody>
						{purgeScopeLines(scope).map((line) => (
							<Table.Tr key={line.label}>
								<Table.Td w={64}>
									<Text size="sm" c="dimmed">
										{line.label}
									</Text>
								</Table.Td>
								<Table.Td>
									<Text size="sm">{line.value}</Text>
								</Table.Td>
							</Table.Tr>
						))}
					</Table.Tbody>
				</Table>
			)}

			{preview.isPending ? (
				<Group gap="xs">
					<Loader size="xs" />
					<Text size="sm">計算中…</Text>
				</Group>
			) : preview.isError ? (
				<Alert color="red" title="無法計算要刪除的筆數">
					<Text size="sm">{preview.error?.message ?? "請稍後再試"}</Text>
				</Alert>
			) : (
				<Text size="sm" fw={600}>
					{purgePreviewText(matched)}
				</Text>
			)}

			<Group justify="flex-end" gap="sm">
				<Button variant="default" onClick={onClose} disabled={purge.isPending}>
					取消
				</Button>
				<Button
					color="red"
					loading={purge.isPending}
					disabled={!preview.isSuccess || matched === 0}
					onClick={() => purge.mutate()}
				>
					刪除
				</Button>
			</Group>
		</Stack>
	);
}
