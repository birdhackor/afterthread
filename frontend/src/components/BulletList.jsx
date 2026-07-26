import { List } from "@mantine/core";

// Deliberately NON-interactive bullet list for AI-provided strings (capture
// follow-up questions, enrichment gaps, ...). Per D38 this replaces a former
// `<Checkbox checked={false} readOnly label={...} />` rendering: Mantine's
// readOnly Checkbox looks interactive but isn't -- users tried to click it
// and got confused, and there was nowhere the checked state could persist
// anyway. `List` is semantic (ul/li) and honestly non-interactive instead of
// faking a checkbox that can never be checked.
// Keyed by `${item}-${occurrence}` (occurrence = how many times this exact
// string was already seen in this pass), not the bare string or a raw array
// index -- the backend can legitimately repeat a string verbatim, and a
// bare-value key would collide silently on duplicates.
export function BulletList({ items }) {
	const seen = new Map();
	return (
		<List size="sm" spacing={4}>
			{items.map((item) => {
				const occurrence = seen.get(item) ?? 0;
				seen.set(item, occurrence + 1);
				return <List.Item key={`${item}-${occurrence}`}>{item}</List.Item>;
			})}
		</List>
	);
}
