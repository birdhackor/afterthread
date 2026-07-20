import { Text } from "@mantine/core";

// Format an ISO datetime string as YYYY-MM-DD in the viewer's local zone.
// Returns "" for missing/invalid input so callers can supply their own
// fallback. Exported standalone for non-Text callers (labels, titles).
export function formatDate(value) {
	if (!value) {
		return "";
	}
	const date = new Date(value);
	if (Number.isNaN(date.getTime())) {
		return "";
	}
	const year = date.getFullYear();
	const month = String(date.getMonth() + 1).padStart(2, "0");
	const day = String(date.getDate()).padStart(2, "0");
	return `${year}-${month}-${day}`;
}

// Renders a formatted date inside a Mantine Text. Extra props (size, c, ...)
// pass through; `fallback` is shown when the value is missing/invalid.
export function DateText({ value, fallback = "—", ...props }) {
	const formatted = formatDate(value);
	return <Text {...props}>{formatted || fallback}</Text>;
}
