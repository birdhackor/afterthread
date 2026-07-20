import { Stack, Text } from "@mantine/core";

// Generic empty-state block: a dimmed message with optional action content
// (buttons/links) rendered below. `align` switches between centered (no data)
// and left-aligned (filtered-to-empty) layouts.
export function EmptyState({ message, align = "center", children, ...props }) {
	return (
		<Stack gap="sm" align={align} py="xl" {...props}>
			<Text c="dimmed">{message}</Text>
			{children}
		</Stack>
	);
}
