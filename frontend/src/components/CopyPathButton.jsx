import { Button, Group, Text } from "@mantine/core";
import { useClipboard } from "@mantine/hooks";

// 複製路徑 for a path the operator has to open by hand (a draft's files, a
// workspace a failed publish left behind). The browser only offers the clipboard to
// a secure context (https or localhost), so a backend opened by its network address
// cannot copy: that is said, and callers show the path in a block that selects in
// one click, so copying by hand always works.
export function CopyPathButton({ path }) {
	const clipboard = useClipboard({ timeout: 1500 });
	return (
		<Group gap={4} wrap="nowrap">
			<Button
				size="compact-xs"
				variant="subtle"
				onClick={() => clipboard.copy(path)}
			>
				{clipboard.copied ? "已複製" : "複製路徑"}
			</Button>
			{clipboard.error ? (
				<Text size="xs" c="red">
					無法自動複製，請自行選取路徑
				</Text>
			) : null}
		</Group>
	);
}
