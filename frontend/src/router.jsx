import { AppShell, Badge, Group, NavLink, Text, Title } from "@mantine/core";
import {
	createRootRoute,
	createRoute,
	createRouter,
	Link,
	Outlet,
} from "@tanstack/react-router";
import { useEffect, useState } from "react";

// Root layout shared by every route: Mantine AppShell with a header and a
// navbar, page content is rendered into AppShell.Main via <Outlet />.
function RootLayout() {
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
				<NavLink component={Link} to="/" label="總覽" />
				<NavLink component={Link} to="/capture" label="快速捕捉" />
				<NavLink component={Link} to="/items" label="記憶清單" />
			</AppShell.Navbar>
			<AppShell.Main>
				<Outlet />
			</AppShell.Main>
		</AppShell>
	);
}

// Home page: pings the backend health endpoint on mount and reports the
// connection status. The backend may not be running yet, so a fetch
// failure (network error, non-OK status, etc.) must not crash the page.
function HomePage() {
	const [connected, setConnected] = useState(false);

	useEffect(() => {
		let cancelled = false;

		fetch("/api/health")
			.then((response) => {
				if (!cancelled) {
					setConnected(response.ok);
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

function ItemsPage() {
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
	component: ItemsPage,
});

const routeTree = rootRoute.addChildren([indexRoute, captureRoute, itemsRoute]);

export const router = createRouter({
	routeTree,
	defaultNotFoundComponent: NotFoundPage,
});
