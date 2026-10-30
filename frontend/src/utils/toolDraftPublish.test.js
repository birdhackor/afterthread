import { describe, expect, it } from "vitest";
import { draftContinuation, reviseStartedMessage } from "./toolDraft.js";
import {
	draftPublishStartedMessage,
	draftPublishTarget,
	draftPublishVariables,
} from "./toolDraftPublish.js";
import {
	buildDraftPublishRequest,
	toolIdentityConsumers,
} from "./toolSummary.js";

const DRAFT_ID = "0123456789abcdef0123456789abcdef";
const OTHER_DRAFT_ID = "fedcba9876543210fedcba9876543210";
const BASE_VID = "20260728T010203Z-abcdef";
const OTHER_VID = "20260730T040506Z-fedcba";

// A draft as the tools list returns it; each test states only what it is about.
const draft = (fields) => ({
	draft_id: DRAFT_ID,
	generation: 3,
	attempts: 2,
	state: "open",
	base_vid: BASE_VID,
	...fields,
});

// The row of the tools list the publish is sent from.
const row = (fields) => ({
	name: "kb",
	current_vid: BASE_VID,
	draft: draft({}),
	...fields,
});

describe("draftPublishTarget", () => {
	it("records the tool and the draft the confirmation opened on", () => {
		expect(draftPublishTarget("kb", draftContinuation(draft({})))).toEqual({
			name: "kb",
			draftId: DRAFT_ID,
			generation: 3,
			baseVid: BASE_VID,
		});
	});

	it("records nothing for a draft that cannot be published", () => {
		expect(draftPublishTarget("kb", null)).toBeNull();
		expect(draftPublishTarget("kb", undefined)).toBeNull();
		for (const state of ["stale", "published", "unknown", "unusable"]) {
			expect(
				draftPublishTarget("kb", draftContinuation(draft({ state }))),
				state,
			).toBeNull();
		}
		expect(
			draftPublishTarget("kb", draftContinuation(draft({ draft_id: null }))),
		).toBeNull();
	});
});

describe("draftPublishVariables: a confirmation publishes the draft it opened on", () => {
	// What the request says for a confirmation that opened on `opened` and is
	// confirmed while the page's row is `now`.
	const sent = (opened, now) =>
		buildDraftPublishRequest(
			draftPublishVariables(
				now,
				draftPublishTarget("kb", draftContinuation(opened)),
			),
		);
	const opened = draft({});

	it("publishes the draft it opened on when the list shows another one now", () => {
		// Discarded in another tab, and another made from a later version.
		const another = draft({
			draft_id: OTHER_DRAFT_ID,
			generation: 1,
			base_vid: OTHER_VID,
		});
		expect(
			sent(opened, row({ current_vid: OTHER_VID, draft: another })),
		).toEqual({
			path: "/api/tools/kb/draft/publish",
			body: {
				draft_id: DRAFT_ID,
				draft_generation: 3,
				expected_vid: BASE_VID,
			},
		});
	});

	it("publishes the draft it opened on when the list shows the same draft written to since", () => {
		expect(sent(opened, row({ draft: draft({ generation: 4 }) })).body).toEqual(
			{
				draft_id: DRAFT_ID,
				draft_generation: 3,
				expected_vid: BASE_VID,
			},
		);
	});

	it("publishes the draft it opened on when the list shows no draft now", () => {
		expect(sent(opened, row({ draft: null })).body).toEqual({
			draft_id: DRAFT_ID,
			draft_generation: 3,
			expected_vid: BASE_VID,
		});
	});

	it("expects the draft's base version, not the row's current one", () => {
		expect(
			sent(opened, row({ current_vid: OTHER_VID })).body.expected_vid,
		).toBe(BASE_VID);
	});

	it("keys the job card to the row the publish is sent from", () => {
		const now = row({ current_vid: OTHER_VID });
		const variables = draftPublishVariables(
			now,
			draftPublishTarget("kb", draftContinuation(opened)),
		);
		expect(variables.jobAttributionKey).toBe(
			toolIdentityConsumers("kb", OTHER_VID).jobAttributionKey,
		);
		expect(variables.name).toBe("kb");
	});
});

describe("draftPublishStartedMessage", () => {
	it("names the tool, and is not a revise's", () => {
		const message = draftPublishStartedMessage("kb");
		expect(message).toContain("「kb」");
		expect(message).not.toContain("AI");
		expect(message).not.toBe(
			reviseStartedMessage("kb", { continuation: null }),
		);
	});
});
