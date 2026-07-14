import {
	Alert,
	AppShell,
	Badge,
	Group,
	NavLink,
	Text,
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
import { apiGet } from "./api/client.js";
import { llmStatusAtom, loadLlmStatusAtom } from "./atoms/llm.js";
import { ItemDetailPage } from "./pages/ItemDetailPage.jsx";
import { ItemEditPage } from "./pages/ItemEditPage.jsx";
import { ItemNewPage } from "./pages/ItemNewPage.jsx";
import { ItemsListPage } from "./pages/ItemsListPage.jsx";

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
				<LlmBanner />
				<Outlet />
			</AppShell.Main>
		</AppShell>
	);
}

// Home page: pings the backend health endpoint on mount and reports the
// connection status. A fetch failure (backend down, network error) must not
// crash the page. Agent 3 replaces this with the review dashboard.
function HomePage() {
	const [connected, setConnected] = useState(false);

	useEffect(() => {
		let cancelled = false;

		apiGet("/api/health")
			.then((data) => {
				if (!cancelled) {
					setConnected(data?.status === "ok");
				}
			})
			.catch(() => {
				if (!cancelled) {
					setConnected(false);
				}
			});

		return () => {
			cancelled = true;
		};
	}, []);

	return (
		<div>
			<Title order={2} mb="md">
				總覽
			</Title>
			<Badge color={connected ? "green" : "red"}>
				{connected ? "後端連線正常" : "無法連線後端"}
			</Badge>
		</div>
	);
}

function CapturePage() {
	return <Text>將於後續階段實作</Text>;
}

function NotFoundPage() {
	return <Text>找不到頁面</Text>;
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
