// @vitest-environment jsdom

import { fireEvent, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, type Mock, vi } from "vitest";
import {
	type ApiJsonRequestOptions,
	apiDelete,
	apiPatch,
	apiPost,
} from "../api/client.js";
import { SECTION_FIELD_KEYS } from "../constants/sections.js";
import { renderWithAppProviders } from "../test/render.js";
import { expectOnlyWriteCall } from "../test/writeCalls.js";
import { ItemNewPage } from "./ItemNewPage.jsx";

vi.mock("../api/client.js", () => ({
	apiDelete: vi.fn(),
	apiPatch: vi.fn(),
	apiPost: vi.fn(),
}));

type ItemCreateOptions = ApiJsonRequestOptions<"/api/items", "post">;
type MockApiOptions = Omit<ItemCreateOptions, "body"> & {
	// OpenAPI marks fields with backend defaults as required, while this form
	// intentionally lets the backend supply source/confidence.
	body: Partial<ItemCreateOptions["body"]>;
};

const mockedApiPost = vi.mocked(apiPost) as unknown as Mock<
	(path: string, options: MockApiOptions) => Promise<unknown>
>;
const writeApiMocks = [
	vi.mocked(apiDelete),
	vi.mocked(apiPatch),
	mockedApiPost,
];

describe("ItemNewPage create request", () => {
	beforeEach(() => {
		for (const apiMock of writeApiMocks) {
			apiMock.mockReset();
		}
	});

	it("keeps the section fields on the canonical API wire names", () => {
		// This oracle is intentionally literal and independent of SECTION_GROUPS.
		// Never tidy it into a loop over the shared production constant: a typo
		// there must make this test fail instead of changing both sides together.
		expect(SECTION_FIELD_KEYS).toEqual([
			"snapshot",
			"why_matters",
			"known",
			"inferred",
			"unknown",
			"decisions",
			"alternatives",
			"rationale",
			"consequences",
			"constraints",
			"assumptions",
			"risks",
			"evidence",
			"open_questions",
			"next_actions",
			"recovery_keywords",
			"recovery_people",
			"recovery_files",
			"resume_trigger",
		]);
	});

	// Full-form Mantine mounts approach Vitest's 5 s default under parallel
	// jsdom load; waitFor keeps the request assertion's own deadline short.
	it("posts the complete form payload to the item collection", async () => {
		const createdItemId = 37;
		const title = `第 ${createdItemId} 號手動項目`;
		const snapshot = "這是唯一一份快照內容";
		const expectedOptions = {
			body: {
				title,
				status: "capture-quick",
				stage: "quick",
				tags: [],
				snapshot,
				why_matters: "",
				known: "",
				inferred: "",
				unknown: "",
				decisions: "",
				alternatives: "",
				rationale: "",
				consequences: "",
				constraints: "",
				assumptions: "",
				risks: "",
				evidence: "",
				open_questions: "",
				next_actions: "",
				recovery_keywords: "",
				recovery_people: "",
				recovery_files: "",
				resume_trigger: "",
			},
		} satisfies MockApiOptions;
		mockedApiPost.mockImplementation(() => new Promise(() => {}));
		renderWithAppProviders(<ItemNewPage />, {
			initialEntries: ["/items/new"],
		});

		const titleInput = await screen.findByRole("textbox", { name: "標題" });
		fireEvent.change(titleInput, { target: { value: `  ${title}  ` } });
		fireEvent.change(screen.getByPlaceholderText("捕捉快照"), {
			target: { value: snapshot },
		});
		fireEvent.click(screen.getByRole("button", { name: "建立" }));

		await waitFor(() => {
			expectOnlyWriteCall(writeApiMocks, mockedApiPost, [
				"/api/items",
				expectedOptions,
			]);
		});
	});
});
