import { describe, expect, it } from "vitest";
import {
	INSTALL_POLL_MS,
	installJobRefetchInterval,
	isHttpUrl,
	isInstallJobActive,
	isSecretName,
	isTerminalInstallState,
	secretNameError,
	secretValueError,
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

describe("isSecretName", () => {
	it("accepts valid env-var names (uppercase-first, padded input)", () => {
		expect(isSecretName("KB_API_KEY")).toBe(true);
		expect(isSecretName("A")).toBe(true);
		expect(isSecretName("X1_2_3")).toBe(true);
		expect(isSecretName("  KB_API_KEY  ")).toBe(true); // trimmed
		expect(isSecretName("A".repeat(64))).toBe(true); // max length
	});

	it("rejects lowercase-start, digit-start, spaces, too-long, and junk", () => {
		expect(isSecretName("kb_key")).toBe(false);
		expect(isSecretName("1KEY")).toBe(false);
		expect(isSecretName("_KEY")).toBe(false);
		expect(isSecretName("KB KEY")).toBe(false);
		expect(isSecretName("A".repeat(65))).toBe(false); // over 64
		expect(isSecretName("")).toBe(false);
		expect(isSecretName(null)).toBe(false);
		expect(isSecretName(undefined)).toBe(false);
	});
});

describe("secretNameError / secretValueError (both-or-neither pair)", () => {
	it("accepts an empty pair (the secret is optional)", () => {
		expect(secretNameError("", "")).toBeNull();
		expect(secretValueError("", "")).toBeNull();
		// Whitespace-only counts as empty on both sides.
		expect(secretNameError("  ", "  ")).toBeNull();
		expect(secretValueError("  ", "  ")).toBeNull();
	});

	it("accepts a complete, valid pair", () => {
		expect(secretNameError("KB_API_KEY", "the-value")).toBeNull();
		expect(secretValueError("KB_API_KEY", "the-value")).toBeNull();
	});

	it("flags the missing half of a half-supplied pair", () => {
		// Name given, value missing -> the VALUE field errors, the name does not.
		expect(secretNameError("KB_API_KEY", "")).toBeNull();
		expect(secretValueError("KB_API_KEY", "")).toBe(
			"請輸入秘密值，或清空秘密名稱",
		);
		// Value given, name missing -> the NAME field errors, the value does not.
		expect(secretValueError("", "the-value")).toBeNull();
		expect(secretNameError("", "the-value")).toBe(
			"請輸入秘密名稱，或清空秘密值",
		);
	});

	it("flags an invalid name even when a value is present", () => {
		expect(secretNameError("kb_key", "v")).toContain("大寫字母");
		expect(secretNameError("1KEY", "v")).toContain("大寫字母");
	});

	it("flags a value under the 6-char redactor floor (F3)", () => {
		// A complete pair with a short value: the value field errors.
		expect(secretValueError("KB_API_KEY", "abc")).toBe("秘密值長度至少 6 字元");
		// Exactly 6 is the floor (>= 6), so it is accepted.
		expect(secretValueError("KB_API_KEY", "abcdef")).toBeNull();
		// Trimmed before measuring, so surrounding spaces do not count.
		expect(secretValueError("KB_API_KEY", "  ab  ")).toBe(
			"秘密值長度至少 6 字元",
		);
		// A missing name is the half-pair error (on the name field); the value
		// field stays clean even for a short value, mirroring the backend's
		// pair-incomplete-before-length precedence.
		expect(secretValueError("", "abc")).toBeNull();
	});
});
