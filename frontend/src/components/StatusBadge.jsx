import { Badge } from "@mantine/core";
import { STATUS_META } from "../constants/labels.js";

// Colored zh-TW badge for a memory item status. Unknown values fall back to a
// gray badge showing the raw value. Extra props (size, variant, ...) override
// the defaults so callers can restyle without a new component.
export function StatusBadge({ status, ...props }) {
	const meta = STATUS_META[status] ?? { label: status, color: "gray" };
	return (
		<Badge color={meta.color} variant="light" {...props}>
			{meta.label}
		</Badge>
	);
}
