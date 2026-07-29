import { describe, expect, it } from "vitest";
import {
	isHttpUrl,
	isSecretName,
	isTerminalToolJobState,
	isToolJobActive,
	secretNameError,
	secretValueError,
	TOOL_JOB_POLL_MS,
	toolJobQueryEnabled,
	toolJobRefetchInterval,
} from "./toolInstall.js";

// Minimal shape of the react-query Query object the interval fn receives.
const queryWithState = (state) => ({
	state: { data: state ? { state } : undefined },
});

// Same, but with the query's latest error carrying an HTTP status (no data yet).
const queryWithError = (status) => ({
	state: { data: undefined, error: { status } },
});

// These pins cover BOTH job kinds the renamed helpers now serve (D40: install
// and revise share one job table, route, and state machine) -- the fixtures
// above are deliberately job-kind-agnostic (just {state}/{errorStatus}), so
// there is nothing install- or revise-specific to vary between them.
describe("isTerminalToolJobState", () => {
	it("treats succeeded and failed as terminal", () => {
		expect(isTerminalToolJobState("succeeded")).toBe(true);
		expect(isTerminalToolJobState("failed")).toBe(true);
	});

	it("treats in-flight and unknown states as non-terminal", () => {
		expect(isTerminalToolJobState("queued")).toBe(false);
		expect(isTerminalToolJobState("running")).toBe(false);
		expect(isTerminalToolJobState(undefined)).toBe(false);
		expect(isTerminalToolJobState("some-future-state")).toBe(false);
	});
});

describe("toolJobRefetchInterval", () => {
	it("keeps polling while the job is queued or running", () => {
		expect(toolJobRefetchInterval(queryWithState("queued"))).toBe(
			TOOL_JOB_POLL_MS,
		);
		expect(toolJobRefetchInterval(queryWithState("running"))).toBe(
			TOOL_JOB_POLL_MS,
		);
	});

	it("keeps polling before the first response lands (no data yet)", () => {
		expect(toolJobRefetchInterval(queryWithState(null))).toBe(TOOL_JOB_POLL_MS);
		expect(toolJobRefetchInterval(undefined)).toBe(TOOL_JOB_POLL_MS);
	});

	it("stops polling once the job is terminal", () => {
		expect(toolJobRefetchInterval(queryWithState("succeeded"))).toBe(false);
		expect(toolJobRefetchInterval(queryWithState("failed"))).toBe(false);
	});

	it("stops polling when the latest error is a 404 (job gone after a restart)", () => {
		expect(toolJobRefetchInterval(queryWithError(404))).toBe(false);
	});

	it("keeps polling on other errors (transient) and before any data", () => {
		expect(toolJobRefetchInterval(queryWithError(500))).toBe(TOOL_JOB_POLL_MS);
		expect(toolJobRefetchInterval(queryWithError(0))).toBe(TOOL_JOB_POLL_MS);
		expect(toolJobRefetchInterval(queryWithState(null))).toBe(TOOL_JOB_POLL_MS);
	});
});

describe("isToolJobActive", () => {
	it("is not active without a job id", () => {
		expect(isToolJobActive({ jobId: null })).toBe(false);
		expect(isToolJobActive({ jobId: undefined })).toBe(false);
	});

	it("is active with a job id before the first poll (no state, no error)", () => {
		expect(isToolJobActive({ jobId: "j1" })).toBe(true);
		expect(
			isToolJobActive({
				jobId: "j1",
				state: undefined,
				errorStatus: undefined,
			}),
		).toBe(true);
	});

	it("stays active while the job is queued or running", () => {
		expect(isToolJobActive({ jobId: "j1", state: "queued" })).toBe(true);
		expect(isToolJobActive({ jobId: "j1", state: "running" })).toBe(true);
	});

	it("releases once the job reaches a terminal state", () => {
		expect(isToolJobActive({ jobId: "j1", state: "succeeded" })).toBe(false);
		expect(isToolJobActive({ jobId: "j1", state: "failed" })).toBe(false);
	});

	it("releases on a 404 poll error (the job is gone after a restart)", () => {
		expect(isToolJobActive({ jobId: "j1", errorStatus: 404 })).toBe(false);
		// Even with a stale non-terminal state cached, a 404 still releases.
		expect(
			isToolJobActive({ jobId: "j1", state: "running", errorStatus: 404 }),
		).toBe(false);
	});

	it("stays active on a non-404 poll error (transient -- keep the job tracked)", () => {
		// A blip must not drop a live job: a resubmit would then 409 and orphan it.
		expect(
			isToolJobActive({ jobId: "j1", state: "running", errorStatus: 500 }),
		).toBe(true);
		expect(isToolJobActive({ jobId: "j1", errorStatus: 500 })).toBe(true);
		expect(isToolJobActive({ jobId: "j1", errorStatus: 0 })).toBe(true);
	});
});

describe("toolJobQueryEnabled", () => {
	it("is disabled with no job id, whatever the query says", () => {
		// Guards the query key `["tool-job", undefined]` case: with no job to poll
		// there is no URL to build, so nothing may fire.
		expect(toolJobQueryEnabled(null)(queryWithState("running"))).toBe(false);
		expect(toolJobQueryEnabled(undefined)(queryWithState(null))).toBe(false);
	});

	it("is enabled while the job is live (including before the first poll)", () => {
		expect(toolJobQueryEnabled("j1")(queryWithState(null))).toBe(true);
		expect(toolJobQueryEnabled("j1")(undefined)).toBe(true);
		expect(toolJobQueryEnabled("j1")(queryWithState("queued"))).toBe(true);
		expect(toolJobQueryEnabled("j1")(queryWithState("running"))).toBe(true);
	});

	it("falls to disabled once the job is terminal or its id 404s", () => {
		// THE fix for the refocus-refetch leak: after this point the app's
		// refetchOnWindowFocus default must have nothing left to act on.
		expect(toolJobQueryEnabled("j1")(queryWithState("succeeded"))).toBe(false);
		expect(toolJobQueryEnabled("j1")(queryWithState("failed"))).toBe(false);
		expect(toolJobQueryEnabled("j1")(queryWithError(404))).toBe(false);
	});

	it("stays enabled on a transient (non-404) poll error", () => {
		expect(toolJobQueryEnabled("j1")(queryWithError(500))).toBe(true);
		expect(toolJobQueryEnabled("j1")(queryWithError(0))).toBe(true);
	});

	it("agrees with the poll-stop rule and the form lock on every input", () => {
		// The point of the shared rule: `enabled`, the poll cadence and the
		// caller-side lock are ONE predicate, so a job can never be (say) locked
		// but unpollable. Pinned as an equivalence over the whole input space
		// rather than three separately-maintained expectations.
		const queries = [
			queryWithState(null),
			queryWithState("queued"),
			queryWithState("running"),
			queryWithState("some-future-state"),
			queryWithState("succeeded"),
			queryWithState("failed"),
			queryWithError(404),
			queryWithError(500),
			queryWithError(0),
			undefined,
		];
		for (const query of queries) {
			const enabled = toolJobQueryEnabled("j1")(query);
			expect(enabled).toBe(toolJobRefetchInterval(query) !== false);
			expect(enabled).toBe(
				isToolJobActive({
					jobId: "j1",
					state: query?.state?.data?.state,
					errorStatus: query?.state?.error?.status,
				}),
			);
		}
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
