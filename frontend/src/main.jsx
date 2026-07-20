import { MantineProvider } from "@mantine/core";
import "@mantine/core/styles.css";
import { Notifications } from "@mantine/notifications";
import "@mantine/notifications/styles.css";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "@tanstack/react-router";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./index.css";
import { router } from "./router.jsx";

// App-wide TanStack Query client. Every page's data fetching (useQuery) and
// every mutation (useMutation + invalidation) run through this single client,
// which replaces the hand-rolled requestId/stale-drop guards the pages used to
// keep -- query keys own staleness now. The three defaults below are chosen to
// coexist with the passive/active connectivity layer (atoms/connectivity.js,
// api/health.js, atoms/llm.js), which stays hand-rolled by design.
const queryClient = new QueryClient({
	defaultOptions: {
		queries: {
			// retry:false preserves the app's one-shot request semantics AND keeps
			// the passive connectivity layer honest: an automatic retry would paper
			// over the very outage the 後端/AI badges exist to surface (api/client.js
			// reports each request's outcome to backendStatusAtom, so a silent
			// behind-the-scenes retry loop would mask a down backend instead of
			// letting the badge go red).
			retry: false,
			// refetchOnWindowFocus:true matches the connectivity monitor's focus
			// semantics (useConnectivityMonitor re-probes /api/health on focus): when
			// the user returns to the tab they get fresh page data on the same
			// trigger that refreshes the badges, so the two never disagree about how
			// current the view is.
			refetchOnWindowFocus: true,
			// staleTime 5s dedups React StrictMode's double-mount (the two mounts
			// share one fresh result instead of firing two requests) and rapid
			// navigation (revisiting a page within 5s reuses the cache) without
			// meaningfully delaying freshness -- anything older than 5s still
			// refetches on mount/focus.
			staleTime: 5_000,
			// networkMode 'always', not the default 'online': this app talks to a
			// LOCALHOST backend, so navigator.onLine (WiFi off, airplane mode) says
			// nothing about whether the loopback server is reachable. The default
			// would PAUSE fetches while "offline" -- fetchStatus 'paused' with
			// isFetching false and no error -- a limbo state the pages' loading/
			// error branches don't (and shouldn't) model, and one that also
			// silences the passive connectivity layer: a paused request never
			// fails, so the 後端 badge would sit on stale green instead of probing
			// red. 'always' fires every request for real; if the backend truly
			// is unreachable it fails fast into the normal network-error path,
			// which is exactly what the badges and error branches are built for.
			networkMode: "always",
		},
		mutations: {
			// retry:false for mutations too: these are user-triggered writes (CRUD,
			// progress, the three AI actions); a silent retry could double-apply a
			// side effect or mask a 409/503 the UI is built to react to.
			retry: false,
			// Same localhost rationale as queries: a paused mutation would hold
			// the page's mutation gate closed indefinitely with no error surfaced.
			networkMode: "always",
		},
	},
});

createRoot(document.getElementById("root")).render(
	<StrictMode>
		<MantineProvider>
			<Notifications />
			<QueryClientProvider client={queryClient}>
				<RouterProvider router={router} />
			</QueryClientProvider>
		</MantineProvider>
	</StrictMode>,
);
