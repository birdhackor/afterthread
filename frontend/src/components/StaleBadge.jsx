import { Badge } from "@mantine/core";

// Yellow "陳舊" badge for stale items. Renders nothing unless `stale` is true,
// so callers can drop it inline without their own guard.
export function StaleBadge({ stale, ...props }) {
	if (!stale) {
		return null;
	}
	return (
		<Badge color="yellow" variant="light" size="sm" {...props}>
			陳舊
		</Badge>
	);
}
