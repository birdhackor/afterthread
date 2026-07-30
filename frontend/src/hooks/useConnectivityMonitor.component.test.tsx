// @vitest-environment jsdom

import { act, cleanup, render } from "@testing-library/react";
import { atom, createStore, type PrimitiveAtom, Provider } from "jotai";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { backendStatusAtom } from "../atoms/connectivity.js";
import { useConnectivityMonitor } from "./useConnectivityMonitor.js";

const statusAtom = backendStatusAtom as PrimitiveAtom<{
	reachable: boolean | null;
}>;

const mocks = vi.hoisted(() => ({
	loadLlmStatus: vi.fn(),
	probeBackendHealth: vi.fn(),
}));

vi.mock("../api/health.js", () => ({
	probeBackendHealth: mocks.probeBackendHealth,
}));

vi.mock("../atoms/llm.js", () => ({
	loadLlmStatusAtom: atom(null, (_get, _set, options) => {
		mocks.loadLlmStatus(options);
	}),
}));

function MonitorHarness() {
	useConnectivityMonitor();
	return null;
}

function renderMonitor(initialReachable: boolean | null = null) {
	const store = createStore();
	store.set(statusAtom, { reachable: initialReachable });
	render(
		<Provider store={store}>
			<MonitorHarness />
		</Provider>,
	);
	return store;
}

beforeEach(() => {
	mocks.loadLlmStatus.mockReset();
	mocks.probeBackendHealth.mockReset();
});

afterEach(() => {
	cleanup();
	vi.restoreAllMocks();
});

describe("useConnectivityMonitor", () => {
	it("suppresses interval and visibility probes while hidden, then probes when visible", () => {
		let intervalCallback: (() => void) | undefined;
		vi.spyOn(window, "setInterval").mockImplementation((callback) => {
			intervalCallback = callback as () => void;
			return 1;
		});
		let visibility: DocumentVisibilityState = "hidden";
		vi.spyOn(document, "visibilityState", "get").mockImplementation(
			() => visibility,
		);

		renderMonitor();
		// Ignore the unconditional mount probe; this test controls only the two
		// triggers whose hidden-tab opt-out protects.
		mocks.probeBackendHealth.mockClear();

		act(() => {
			intervalCallback?.();
			document.dispatchEvent(new Event("visibilitychange"));
		});
		expect(mocks.probeBackendHealth).not.toHaveBeenCalled();

		visibility = "visible";
		act(() => {
			intervalCallback?.();
			document.dispatchEvent(new Event("visibilitychange"));
		});
		expect(mocks.probeBackendHealth).toHaveBeenCalledTimes(2);
	});

	it("forces an LLM reload only on a false-to-true recovery transition", () => {
		const store = renderMonitor(null);

		act(() => {
			store.set(statusAtom, { reachable: true });
		});
		expect(mocks.loadLlmStatus).not.toHaveBeenCalled();

		act(() => {
			store.set(statusAtom, { reachable: false });
		});
		act(() => {
			store.set(statusAtom, { reachable: true });
		});
		expect(mocks.loadLlmStatus).toHaveBeenCalledOnce();
		expect(mocks.loadLlmStatus).toHaveBeenCalledWith({ force: true });

		act(() => {
			store.set(statusAtom, { reachable: true });
		});
		expect(mocks.loadLlmStatus).toHaveBeenCalledOnce();
	});
});
