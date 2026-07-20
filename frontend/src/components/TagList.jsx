import { Badge, Group } from "@mantine/core";

// Renders memory item tags as a row of outline badges. Absent/empty tags render
// nothing. `gap`/`size` are overridable; extra props pass through to the Group.
// Keyed by `${tag}-${occurrence}` (occurrence = how many times this exact tag
// was already seen in this pass) rather than the bare tag: the backend
// contract does not guarantee tags are unique, and a bare-value key collides
// silently on duplicates. Not a raw array-index key -- order-preserving but
// still stable per distinct value, and clean for biome's noArrayIndexKey.
export function TagList({ tags, gap = 4, size = "sm", ...props }) {
	if (!tags || tags.length === 0) {
		return null;
	}
	const seen = new Map();
	return (
		<Group gap={gap} {...props}>
			{tags.map((tag) => {
				const occurrence = seen.get(tag) ?? 0;
				seen.set(tag, occurrence + 1);
				return (
					<Badge
						key={`${tag}-${occurrence}`}
						variant="outline"
						color="gray"
						size={size}
					>
						{tag}
					</Badge>
				);
			})}
		</Group>
	);
}
