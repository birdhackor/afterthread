import { Text } from "@mantine/core";

// Soft character counter rendered under a length-bounded field; turns red
// once the current value exceeds `max`. Shared by every form field that
// enforces a backend length bound (capture textarea, item form fields).
// `suppressOverLimit` keeps the neutral (dimmed) style even past `max` -- used
// by ItemForm for an untouched edit-mode section that already holds more than
// `max` chars server-side, so the count reads honestly without the "too long"
// treatment reserved for a value the user is actually about to submit.
export function CharCounter({ value, max, suppressOverLimit = false }) {
	const length = value?.length ?? 0;
	const overLimit = !suppressOverLimit && length > max;
	return (
		<Text size="xs" c={overLimit ? "red" : "dimmed"} ta="right" mt={4}>
			{length} / {max}
		</Text>
	);
}
