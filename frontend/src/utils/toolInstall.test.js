import { describe, expect, it } from "vitest";
import {
	INSTALL_POLL_MS,
	installJobRefetchInterval,
	isHttpUrl,
	isTerminalInstallState,
} from "./toolInstall.js";

// Minimal shape of the react-query Query object the interval fn receives.
const queryWithState = (state) => ({
	state: { data: state ? { state } : undefined },
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
