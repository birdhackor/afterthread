import {
	Alert,
	AppShell,
	Button,
	Container,
	Group,
	NavLink,
	Stack,
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
import { llmStatusAtom, loadLlmStatusAtom } from "./atoms/llm.js";
import { LLM_NOT_CONFIGURED_NOTICE } from "./constants/labels.js";
import { useConnectivityMonitor } from "./hooks/useConnectivityMonitor.js";
import { CapturePage } from "./pages/CapturePage.jsx";
import { HomePage } from "./pages/HomePage.jsx";
import { ItemDetailPage } from "./pages/ItemDetailPage.jsx";
import { ItemEditPage } from "./pages/ItemEditPage.jsx";
import { ItemNewPage } from "./pages/ItemNewPage.jsx";
import { ItemsListPage } from "./pages/ItemsListPage.jsx";
import { LlmLogsPage } from "./pages/LlmLogsPage.jsx";
import { NotFoundPage } from "./pages/NotFoundPage.jsx";
import { ToolsPage } from "./pages/ToolsPage.jsx";

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
// UX rules; AI action buttons on other pages are disabled separately. Also
// offers a 重新檢查 action -- without it, a user who fixes backend/.env and
// restarts the backend has no way back to "已設定" short of a full page
// reload, since the status is otherwise only ever fetched once on mount (see
// RootLayout below).
function LlmBanner() {
	const status = useAtomValue(llmStatusAtom);
	const loadLlmStatus = useSetAtom(loadLlmStatusAtom);
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
			<Stack gap="xs" align="flex-start">
				<Text size="sm">{LLM_NOT_CONFIGURED_NOTICE}</Text>
				<Button
					size="xs"
					variant="light"
					loading={status.loading}
					onClick={() => loadLlmStatus({ force: true })}
				>
					重新檢查
				</Button>
			</Stack>
		</Alert>
	);
}

// Root layout shared by every route: Mantine AppShell with a header and a
// navbar; page content is rendered into AppShell.Main via <Outlet />. Loads
// the LLM status once on mount so the banner and AI buttons can react to it,
// and hosts the app-wide connectivity monitor -- RootLayout is the one
// component that stays mounted for the whole session, so it is the only
// place a mount-once poll/listener set belongs.
function RootLayout() {
	const loadLlmStatus = useSetAtom(loadLlmStatusAtom);

	// 30s /api/health poll + focus/online/visibility pings feeding
	// backendStatusAtom, plus the automatic LLM re-probe when the backend
	// comes back up (see hooks/useConnectivityMonitor.js).
	useConnectivityMonitor();

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
				<NavItem to="/items" label="記憶清單" exact />
				<NavItem to="/items/new" label="新增項目" />
				<NavItem to="/tools" label="工具" />
				<NavItem to="/llm-logs" label="AI 日誌" />
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

const llmLogsRoute = createRoute({
	getParentRoute: () => rootRoute,
	path: "/llm-logs",
	component: LlmLogsPage,
	// Optional deep-link target: /llm-logs?log=<id> auto-expands that record's
	// row on load (see LlmLogsPage). Coerce to a positive integer or drop it --
	// validateSearch must never throw on junk, so a malformed ?log= just resolves
	// to "no target" rather than breaking navigation to the page.
	validateSearch: (search) => {
		const raw = search?.log;
		const id = typeof raw === "number" ? raw : Number.parseInt(raw, 10);
		return Number.isInteger(id) && id > 0 ? { log: id } : {};
	},
});

const toolsRoute = createRoute({
	getParentRoute: () => rootRoute,
	path: "/tools",
	component: ToolsPage,
});

// Remount-per-id contract: TanStack Router reuses the matched route's
// component instance across navigations that only change a path param (e.g.
// /items/1 -> /items/2 stays the same ItemDetailPage instance, just
// re-rendered with a new itemId). ItemDetailPage and ItemEditPage each keep
// per-item refs (reqRef, isMountedRef) and in-flight async closures (AI/
// progress refresh, edit-page PATCH -> backToDetail) that capture the itemId
// current at the time they were created; without a forced remount those
// closures can resolve after the param has moved on and act on/navigate to
// the wrong item. Keying the rendered page on itemId forces React to unmount
// the old instance (running its cleanup, so isMountedRef flips false and the
// old reqRef is discarded) and mount a fresh one whenever itemId changes, so
// every stale callback from the previous item becomes a no-op. The pages
// still read itemId via useParams internally -- this wrapper only adds the
// key.
const itemDetailRoute = createRoute({
	getParentRoute: () => rootRoute,
	path: "/items/$itemId",
	component: () => {
		const { itemId } = itemDetailRoute.useParams();
		return <ItemDetailPage key={itemId} />;
	},
});

const itemEditRoute = createRoute({
	getParentRoute: () => rootRoute,
	path: "/items/$itemId/edit",
	component: () => {
		const { itemId } = itemEditRoute.useParams();
		return <ItemEditPage key={itemId} />;
	},
});

const routeTree = rootRoute.addChildren([
	indexRoute,
	captureRoute,
	itemsRoute,
	itemNewRoute,
	toolsRoute,
	llmLogsRoute,
	itemDetailRoute,
	itemEditRoute,
]);

export const router = createRouter({
	routeTree,
	defaultNotFoundComponent: NotFoundPage,
});
