import { createStore, getDefaultStore } from "jotai";
import { beforeEach, describe, expect, it } from "vitest";
import {
	backendStatusAtom,
	reportBackendDownAtom,
	reportBackendUpAtom,
} from "./connectivity.js";

// Production code writes these atoms through jotai's default store (the app
// renders without a <Provider>, so api/client.js targets getDefaultStore()),
// so the transition tests below go through the same store to exercise the
// exact wiring the app uses.
const store = getDefaultStore();

// The default store is a process-wide singleton, so state written by one
// test would leak into the next without an explicit reset.
beforeEach(() => {
	store.set(backendStatusAtom, { reachable: null });
});

describe("backendStatusAtom", () => {
	it("starts undetermined (reachable: null)", () => {
		// A fresh store (not the shared default one) reads the atom's true
		// initial value, without depending on the beforeEach reset above
		// happening to match it.
		expect(createStore().get(backendStatusAtom)).toEqual({ reachable: null });
	});
});

describe("reportBackendUpAtom / reportBackendDownAtom", () => {
	it("flips null -> true on the first up report", () => {
		store.set(reportBackendUpAtom);
		expect(store.get(backendStatusAtom)).toEqual({ reachable: true });
	});

	it("flips null -> false on the first down report", () => {
		store.set(reportBackendDownAtom);
		expect(store.get(backendStatusAtom)).toEqual({ reachable: false });
	});

	it("transitions true -> false when the backend drops", () => {
		store.set(reportBackendUpAtom);
		store.set(reportBackendDownAtom);
		expect(store.get(backendStatusAtom)).toEqual({ reachable: false });
	});

	it("transitions false -> true when the backend recovers", () => {
		store.set(reportBackendDownAtom);
		store.set(reportBackendUpAtom);
		expect(store.get(backendStatusAtom)).toEqual({ reachable: true });
	});

	it("does not write a new state object for a redundant up report", () => {
		// Every apiFetch call fires a report, so the write atoms only write on
		// an actual change -- otherwise each request would hand subscribers a
		// fresh (deep-equal) object and re-render them for nothing. Object
		// identity (toBe) is the observable proof that no write happened.
		store.set(reportBackendUpAtom);
		const settled = store.get(backendStatusAtom);
		store.set(reportBackendUpAtom);
		expect(store.get(backendStatusAtom)).toBe(settled);
	});

	it("does not write a new state object for a redundant down report", () => {
		store.set(reportBackendDownAtom);
		const settled = store.get(backendStatusAtom);
		store.set(reportBackendDownAtom);
		expect(store.get(backendStatusAtom)).toBe(settled);
	});
});
