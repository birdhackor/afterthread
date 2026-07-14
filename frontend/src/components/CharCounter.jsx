import { Text } from "@mantine/core";

// Soft character counter rendered under a length-bounded field; turns red
// once the current value exceeds `max`. Shared by every form field that
// enforces a backend length bound (capture textarea, item form fields).
export function CharCounter({ value, max }) {
	const length = value?.length ?? 0;
	return (
		<Text size="xs" c={length > max ? "red" : "dimmed"} ta="right" mt={4}>
			{length} / {max}
		</Text>
	);
}
