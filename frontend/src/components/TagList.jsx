import { Badge, Group } from "@mantine/core";

// Renders memory item tags as a row of outline badges. Absent/empty tags render
// nothing. `gap`/`size` are overridable; extra props pass through to the Group.
export function TagList({ tags, gap = 4, size = "sm", ...props }) {
	if (!tags || tags.length === 0) {
		return null;
	}
	return (
		<Group gap={gap} {...props}>
			{tags.map((tag) => (
				<Badge key={tag} variant="outline" color="gray" size={size}>
					{tag}
				</Badge>
			))}
		</Group>
	);
}
