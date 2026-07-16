import { describe, expect, it } from "vitest";
import {
	INSTALL_POLL_MS,
	installJobRefetchInterval,
	isHttpUrl,
	isInstallJobActive,
	isTerminalInstallState,
} from "./toolInstall.js";

// Minimal shape of the react-query Query object the interval fn receives.
const queryWithState = (state) => ({
	state: { data: state ? { state } : undefined },
});

// Same, but with the query's latest error carrying an HTTP status (no data yet).
const queryWithError = (status) => ({
	state: { data: undefined, error: { status } },
});

describe("isTerminalInstallState", () => {
	it("treats succeeded and failed as terminal", () => {
		expect(isTerminalInstallState("succeeded")).toBe(true);
		expect(isTerminalInstallState("failed")).toBe(true);
	});

	it("treats in-flight and unknown states as non-terminal", () => {
		expect(isTerminalInstallState("queued")).toBe(false);
		expect(isTerminalInstallState("running")).toBe(false);
		expect(isTerminalInstallState(undefined)).toBe(false);
		expect(isTerminalInstallState("some-future-state")).toBe(false);
	});
});

describe("installJobRefetchInterval", () => {
	it("keeps polling while the job is queued or running", () => {
		expect(installJobRefetchInterval(queryWithState("queued"))).toBe(
			INSTALL_POLL_MS,
		);
		expect(installJobRefetchInterval(queryWithState("running"))).toBe(
			INSTALL_POLL_MS,
		);
	});

	it("keeps polling before the first response lands (no data yet)", () => {
		expect(installJobRefetchInterval(queryWithState(null))).toBe(
			INSTALL_POLL_MS,
		);
		expect(installJobRefetchInterval(undefined)).toBe(INSTALL_POLL_MS);
	});

	it("stops polling once the job is terminal", () => {
		expect(installJobRefetchInterval(queryWithState("succeeded"))).toBe(false);
		expect(installJobRefetchInterval(queryWithState("failed"))).toBe(false);
	});

	it("stops polling when the latest error is a 404 (job gone after a restart)", () => {
		expect(installJobRefetchInterval(queryWithError(404))).toBe(false);
	});

	it("keeps polling on other errors (transient) and before any data", () => {
		expect(installJobRefetchInterval(queryWithError(500))).toBe(
			INSTALL_POLL_MS,
		);
		expect(installJobRefetchInterval(queryWithError(0))).toBe(INSTALL_POLL_MS);
		expect(installJobRefetchInterval(queryWithState(null))).toBe(
			INSTALL_POLL_MS,
		);
	});
});

describe("isInstallJobActive", () => {
	it("is not active without a job id", () => {
		expect(isInstallJobActive({ jobId: null })).toBe(false);
		expect(isInstallJobActive({ jobId: undefined })).toBe(false);
	});

	it("is active with a job id before the first poll (no state, no error)", () => {
		expect(isInstallJobActive({ jobId: "j1" })).toBe(true);
		expect(
			isInstallJobActive({
				jobId: "j1",
				state: undefined,
				errorStatus: undefined,
			}),
		).toBe(true);
	});

	it("stays active while the job is queued or running", () => {
		expect(isInstallJobActive({ jobId: "j1", state: "queued" })).toBe(true);
		expect(isInstallJobActive({ jobId: "j1", state: "running" })).toBe(true);
	});

	it("releases once the job reaches a terminal state", () => {
		expect(isInstallJobActive({ jobId: "j1", state: "succeeded" })).toBe(false);
		expect(isInstallJobActive({ jobId: "j1", state: "failed" })).toBe(false);
	});

	it("releases on a 404 poll error (the job is gone after a restart)", () => {
		expect(isInstallJobActive({ jobId: "j1", errorStatus: 404 })).toBe(false);
		// Even with a stale non-terminal state cached, a 404 still releases.
		expect(
			isInstallJobActive({ jobId: "j1", state: "running", errorStatus: 404 }),
		).toBe(false);
	});

	it("stays active on a non-404 poll error (transient -- keep the job tracked)", () => {
		// A blip must not drop a live job: a resubmit would then 409 and orphan it.
		expect(
			isInstallJobActive({ jobId: "j1", state: "running", errorStatus: 500 }),
		).toBe(true);
		expect(isInstallJobActive({ jobId: "j1", errorStatus: 500 })).toBe(true);
		expect(isInstallJobActive({ jobId: "j1", errorStatus: 0 })).toBe(true);
	});
});

describe("isHttpUrl", () => {
	it("accepts http and https URLs (case-insensitive scheme, padded input)", () => {
		expect(isHttpUrl("http://kb.example/openapi.json")).toBe(true);
		expect(isHttpUrl("https://kb.example/openapi.json")).toBe(true);
		expect(isHttpUrl("  HTTPS://kb.example/spec  ")).toBe(true);
	});

	it("rejects other schemes, bare schemes, and junk", () => {
		expect(isHttpUrl("ftp://kb.example/spec")).toBe(false);
		expect(isHttpUrl("kb.example/openapi.json")).toBe(false);
		expect(isHttpUrl("http://")).toBe(false);
		expect(isHttpUrl("")).toBe(false);
		expect(isHttpUrl(null)).toBe(false);
		expect(isHttpUrl(undefined)).toBe(false);
	});
});
