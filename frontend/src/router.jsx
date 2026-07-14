import {
	Alert,
	AppShell,
	Container,
	Group,
	NavLink,
	Title,
} from "@mantine/core";
import {
	createRootRoute,
	createRoute,
	createRouter,
	Link,
	Outlet,
} from "@tanstack/react-router";
import { useAtomValue, useSetAtom } from "jotai";
import { useEffect, useState } from "react";
import { llmStatusAtom, loadLlmStatusAtom } from "./atoms/llm.js";
import { CapturePage } from "./pages/CapturePage.jsx";
import { HomePage } from "./pages/HomePage.jsx";
import { ItemDetailPage } from "./pages/ItemDetailPage.jsx";
import { ItemEditPage } from "./pages/ItemEditPage.jsx";
import { ItemNewPage } from "./pages/ItemNewPage.jsx";
import { ItemsListPage } from "./pages/ItemsListPage.jsx";
import { NotFoundPage } from "./pages/NotFoundPage.jsx";

// Navbar entry rendered as a TanStack Link. Active highlighting is driven by
// `activeProps` (data-active), which Mantine's NavLink styles via its
// [data-active] selector; `exact` is used for the index route so it is not
// marked active on every sub-path.
function NavItem({ to, label, exact }) {
	return (
		<NavLink
			component={Link}
			to={to}
			label={label}
			activeOptions={exact ? { exact: true } : undefined}
			activeProps={{ "data-active": true }}
		/>
	);
}

// Dismissible top banner shown only once the LLM status has loaded and the
// backend reports the endpoint is not configured. Content matches the shared
// UX rules; AI action buttons on other pages are disabled separately.
function LlmBanner() {
	const status = useAtomValue(llmStatusAtom);
	const [dismissed, setDismissed] = useState(false);

	if (dismissed || !status.loaded || status.configured) {
		return null;
	}

	return (
		<Alert
			color="orange"
			withCloseButton
			onClose={() => setDismissed(true)}
			mb="md"
		>
			AI 功能未設定：請在 backend/.env 填入 OPENAI_BASE_URL 後重啟
		</Alert>
	);
}

// Root layout shared by every route: Mantine AppShell with a header and a
// navbar; page content is rendered into AppShell.Main via <Outlet />. Loads
// the LLM status once on mount so the banner and AI buttons can react to it.
function RootLayout() {
	const loadLlmStatus = useSetAtom(loadLlmStatusAtom);

	useEffect(() => {
		loadLlmStatus();
	}, [loadLlmStatus]);

	return (
		<AppShell
			header={{ height: 60 }}
			navbar={{ width: 220, breakpoint: "sm" }}
			padding="md"
		>
			<AppShell.Header>
				<Group h="100%" px="md">
					<Title order={3}>Context Memory</Title>
				</Group>
			</AppShell.Header>
			<AppShell.Navbar p="md">
				<NavItem to="/" label="總覽" exact />
				<NavItem to="/capture" label="快速捕捉" />
				<NavItem to="/items" label="記憶清單" />
				<NavItem to="/items/new" label="新增項目" />
			</AppShell.Navbar>
			<AppShell.Main>
				<Container size="lg" px={0}>
					<LlmBanner />
					<Outlet />
				</Container>
			</AppShell.Main>
		</AppShell>
	);
}

const rootRoute = createRootRoute({
	component: RootLayout,
});

const indexRoute = createRoute({
	getParentRoute: () => rootRoute,
	path: "/",
	component: HomePage,
});

const captureRoute = createRoute({
	getParentRoute: () => rootRoute,
	path: "/capture",
	component: CapturePage,
});

const itemsRoute = createRoute({
	getParentRoute: () => rootRoute,
	path: "/items",
	component: ItemsListPage,
});

const itemNewRoute = createRoute({
	getParentRoute: () => rootRoute,
	path: "/items/new",
	component: ItemNewPage,
});

const itemDetailRoute = createRoute({
	getParentRoute: () => rootRoute,
	path: "/items/$itemId",
	component: ItemDetailPage,
});

const itemEditRoute = createRoute({
	getParentRoute: () => rootRoute,
	path: "/items/$itemId/edit",
	component: ItemEditPage,
});

const routeTree = rootRoute.addChildren([
	indexRoute,
	captureRoute,
	itemsRoute,
	itemNewRoute,
	itemDetailRoute,
	itemEditRoute,
]);

export const router = createRouter({
	routeTree,
	defaultNotFoundComponent: NotFoundPage,
});
