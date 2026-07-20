import { Button, Stack, Text, Title } from "@mantine/core";
import { Link } from "@tanstack/react-router";
import { usePageTitle } from "../hooks/usePageTitle.js";

// Styled fallback for unmatched routes (wired as the router's
// defaultNotFoundComponent).
export function NotFoundPage() {
	usePageTitle("找不到頁面");
	return (
		<Stack gap="md" align="flex-start" py="xl">
			<Title order={2}>找不到頁面</Title>
			<Text c="dimmed">您要找的頁面不存在，可能連結有誤或已被移除。</Text>
			<Button component={Link} to="/" variant="light">
				返回總覽
			</Button>
		</Stack>
	);
}
