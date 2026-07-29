import { MantineProvider } from "@mantine/core";
import { Notifications } from "@mantine/notifications";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
	createMemoryHistory,
	createRootRoute,
	createRouter,
	RouterProvider,
} from "@tanstack/react-router";
import "@testing-library/jest-dom/vitest";
import { cleanup, type RenderOptions, render } from "@testing-library/react";
import type { ReactElement } from "react";
import { afterEach } from "vitest";

afterEach(cleanup);

// These measured shims match the installed Mantine 9.4.1 ESM. ResizeObserver,
// IntersectionObserver, scrollIntoView, and getComputedStyle are deliberately
// omitted: no component test has crossed those boundaries. Adding speculative
// no-ops would let an inaccurate browser model silently age behind green tests.
// scripts/check-mantine-api-surface.mjs separately flags upstream reference
// changes; a reference there is only a prompt to inspect a real test failure.
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

interface AppRenderOptions extends Omit<RenderOptions, "wrapper"> {
	initialEntries?: string[];
	queryClient?: QueryClient;
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
		...renderOptions
	}: AppRenderOptions = {},
) {
	// RouterProvider owns the route tree instead of accepting children, so the
	// component under test is the root route for this isolated memory router.
	const rootRoute = createRootRoute({
		component: () => ui,
	});
	const router = createRouter({
		routeTree: rootRoute,
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
