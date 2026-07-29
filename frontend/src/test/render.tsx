import { MantineProvider } from "@mantine/core";
import { Notifications } from "@mantine/notifications";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
	createMemoryHistory,
	createRootRoute,
	createRoute,
	createRouter,
	Outlet,
	RouterProvider,
} from "@tanstack/react-router";
import "@testing-library/jest-dom/vitest";
import { cleanup, type RenderOptions, render } from "@testing-library/react";
import type { ReactElement } from "react";
import { afterEach } from "vitest";

afterEach(cleanup);

// Every shim here was added because a test CRASHED without it, and the crash is
// named beside it. That policy is the point: a speculative no-op is exercised by
// nothing, so it can drift into a wrong answer while every test stays green.
// It also tells us exactly which shims are needed instead of guessing: mounting
// ItemDetailPage needed ResizeObserver, and asserting its Select then needed
// scrollIntoView — two of the five Mantine's guide lists, each added only when a
// real error named it. IntersectionObserver and getComputedStyle still are NOT
// here, and should stay absent until something actually crashes without them.
//
// A boundary we have not crossed is not a gap to pre-fill: an unshimmed API
// throws loudly and names itself, which is self-detecting and needs no upkeep.
// scripts/check-mantine-api-surface.mjs watches upstream references separately;
// a reference there is only a prompt to go look at a real failing test.
//
// jsdom exposes no media-query evaluator, while MantineProvider reads
// matchMedia during mount even when the test does not exercise color schemes.
if (typeof window.matchMedia !== "function") {
	Object.defineProperty(window, "matchMedia", {
		writable: true,
		value: (query: string): MediaQueryList => ({
			matches: false,
			media: query,
			onchange: null,
			addListener: () => {},
			removeListener: () => {},
			addEventListener: () => {},
			removeEventListener: () => {},
			dispatchEvent: () => false,
		}),
	});
}

// Mantine's autosizing Textarea subscribes to the CSS Font Loading API.
// jsdom does not implement that API, and layout is not measurable here anyway.
if (document.fonts === undefined) {
	Object.defineProperty(document, "fonts", {
		value: {
			addEventListener: () => {},
			removeEventListener: () => {},
		},
	});
}

// Mantine's ScrollArea observes its viewport to size the scrollbar. Mounting
// ItemDetailPage without this threw `ReferenceError: ResizeObserver is not
// defined` from inside <Scrollbar>, four times, before any interaction could
// run -- so this is a measured requirement, not a precaution.
//
// A no-op is the honest shape: nothing here can produce a real resize, because
// jsdom has no layout to resize. It therefore keeps the component mountable and
// asserts nothing about geometry. A test that needed to TRIGGER a resize would
// need a real fake (jsdom-testing-mocks' mockResizeObserver) — we have none.
if (typeof globalThis.ResizeObserver === "undefined") {
	globalThis.ResizeObserver = class {
		observe() {}
		unobserve() {}
		disconnect() {}
	} as unknown as typeof ResizeObserver;
}

// Mantine's Combobox scrolls the highlighted option into view when the keyboard
// or a click moves the selection. jsdom implements no scrolling, so asserting a
// Select on ItemDetailPage threw `TypeError: items[index]?.scrollIntoView is not
// a function` from use-combobox — measured, like the two above, not assumed.
//
// A no-op is again the honest shape: with no layout there is nothing to scroll,
// so this makes the interaction complete and asserts nothing about what ends up
// visible. A test that depended on visibility after scrolling would be testing
// the shim, not the app.
if (typeof Element.prototype.scrollIntoView !== "function") {
	Element.prototype.scrollIntoView = () => {};
}

interface AppRenderOptions extends Omit<RenderOptions, "wrapper"> {
	initialEntries?: string[];
	queryClient?: QueryClient;
	routePath?: "/items/$itemId" | "/items/$itemId/edit";
}

function createTestQueryClient() {
	return new QueryClient({
		defaultOptions: {
			queries: {
				retry: false,
				refetchOnWindowFocus: false,
				networkMode: "always",
			},
			mutations: {
				retry: false,
				networkMode: "always",
			},
		},
	});
}

export function renderWithAppProviders(
	ui: ReactElement,
	{
		initialEntries = ["/"],
		queryClient = createTestQueryClient(),
		routePath,
		...renderOptions
	}: AppRenderOptions = {},
) {
	// RouterProvider owns the route tree instead of accepting children, so the
	// component under test is either the root route or a declared child when it
	// needs real path params from the isolated memory router.
	const rootRoute = createRootRoute(
		routePath ? { component: Outlet } : { component: () => ui },
	);
	const routeTree = routePath
		? rootRoute.addChildren([
				createRoute({
					getParentRoute: () => rootRoute,
					path: routePath,
					component: () => ui,
				}),
			])
		: rootRoute;
	const router = createRouter({
		routeTree,
		history: createMemoryHistory({ initialEntries }),
	});

	const result = render(
		<MantineProvider>
			<Notifications />
			<QueryClientProvider client={queryClient}>
				<RouterProvider router={router} />
			</QueryClientProvider>
		</MantineProvider>,
		renderOptions,
	);

	return { ...result, queryClient, router };
}
