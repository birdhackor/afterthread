import {
	Alert,
	Badge,
	Center,
	Code,
	Group,
	Loader,
	Stack,
	Table,
	Text,
} from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import { apiGet } from "../api/client.js";
import {
	configEntryFlags,
	settingsNotices,
	toolSettingsQueryKey,
} from "../utils/toolSettings.js";

// The read-only 設定 section of one tool row (web-v6 §9): the package's
// config.env pairs and its .env KEY NAMES, both package-layer files that outlive
// every version. Fetched lazily (enabled: expanded) like the AI-summary panel,
// and independent of it: a package whose `current` is broken still has
// settings worth reading, so this renders even where the summary cannot.
//
// Read-only on purpose. Editing stays a hand edit of the files, the supported
// way to change them (D21); a web editor would first need the write-side rules
// (atomic replace, mode bits) that `afterthread init-env` already had to learn.
export function ToolSettingsSection({ name, expanded }) {
	const { data, error, isError, isFetching } = useQuery({
		queryKey: toolSettingsQueryKey(name),
		queryFn: () => apiGet(`/api/tools/${name}/settings`),
		enabled: expanded,
	});

	if (data === undefined && isFetching) {
		return (
			<Center py="sm">
				<Loader size="sm" />
			</Center>
		);
	}
	if (data === undefined) {
		return isError ? (
			<Alert color="red" title="無法載入設定">
				<Text size="sm">{error?.message ?? "請稍後再試"}</Text>
			</Alert>
		) : null;
	}

	const notices = settingsNotices(data);
	return (
		<Stack gap="sm" pt="xs">
			{notices.map((notice) => (
				<Alert key={notice.text} color={notice.color}>
					<Text size="sm">{notice.text}</Text>
				</Alert>
			))}

			<Stack gap={4}>
				<Text size="sm" fw={600}>
					config.env：一般設定（不會被遮蔽）
				</Text>
				{data.config.length === 0 ? (
					<Text size="sm" c="dimmed">
						沒有 config.env。TTL、網址這類不是秘密的設定請放這裡。
					</Text>
				) : (
					<Table withTableBorder={false} verticalSpacing={4}>
						<Table.Tbody>
							{data.config.map((entry) => (
								<Table.Tr key={entry.key}>
									<Table.Td>
										<Code>{entry.key}</Code>
									</Table.Td>
									<Table.Td>
										<Text size="sm" style={{ wordBreak: "break-all" }}>
											{entry.value}
										</Text>
									</Table.Td>
									<Table.Td>
										<Group gap={4}>
											{configEntryFlags(entry).map((flag) => (
												<Badge
													key={flag.label}
													size="sm"
													variant="light"
													color={flag.color}
													// Badge upper-cases by default, which would print the
													// file name as ".ENV".
													tt="none"
												>
													{flag.label}
												</Badge>
											))}
										</Group>
									</Table.Td>
								</Table.Tr>
							))}
						</Table.Tbody>
					</Table>
				)}
			</Stack>

			<Stack gap={4}>
				<Text size="sm" fw={600}>
					.env：秘密（只列名稱）
				</Text>
				{data.env_keys.length === 0 ? (
					<Text size="sm" c="dimmed">
						沒有 .env。
					</Text>
				) : (
					<Group gap={4}>
						{data.env_keys.map((key) => (
							<Code key={key}>{key}</Code>
						))}
					</Group>
				)}
				<Text size="xs" c="dimmed">
					.env 的每個值都會被當成秘密遮蔽；不是秘密的設定請移到同目錄的
					config.env。
				</Text>
			</Stack>

			<Text size="xs" c="dimmed">
				兩個檔案都在這個工具的目錄裡，請直接編輯。執行時 config.env
				會蓋過程式內的預設值，.env 又會蓋過 config.env。
			</Text>
		</Stack>
	);
}
