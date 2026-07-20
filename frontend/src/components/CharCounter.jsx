import { Text } from "@mantine/core";
import { codePointLength } from "../utils/text.js";

// Soft character counter rendered under a length-bounded field; turns red
// once the current value exceeds `max`. Shared by every form field that
// enforces a backend length bound (capture textarea, item form fields).
// `suppressOverLimit` keeps the neutral (dimmed) style even past `max` -- used
// by ItemForm for an untouched edit-mode field (title or a section) that
// already holds more than `max` chars server-side, so the count reads
// honestly without the "too long" treatment reserved for a value the user is
// actually about to submit.
//
// Counts Unicode code points (via codePointLength), matching the backend
// Pydantic bound `max` mirrors -- not JS's UTF-16-code-unit `.length`, which
// would over-count non-BMP characters like most emoji.
export function CharCounter({ value, max, suppressOverLimit = false }) {
	const length = value ? codePointLength(value) : 0;
	const overLimit = !suppressOverLimit && length > max;
	return (
		<Text size="xs" c={overLimit ? "red" : "dimmed"} ta="right" mt={4}>
			{length} / {max}
		</Text>
	);
}
