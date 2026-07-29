// @vitest-environment jsdom

import { fireEvent, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, it, type Mock, vi } from "vitest";
import { apiDelete, apiPatch, apiPost } from "../api/client.js";
import { SECTION_FIELD_KEYS } from "../constants/sections.js";
import { renderWithAppProviders } from "../test/render.js";
import { expectOnlyWriteCall } from "../test/writeCalls.js";
import { ItemNewPage } from "./ItemNewPage.jsx";

vi.mock("../api/client.js", () => ({
	apiDelete: vi.fn(),
	apiPatch: vi.fn(),
	apiPost: vi.fn(),
}));

type MockApiOptions = {
	body?: unknown;
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

	// Full-form Mantine mounts approach Vitest's 5 s default under parallel
	// jsdom load; waitFor keeps the request assertion's own deadline short.
	it("posts the complete form payload to the item collection", async () => {
		const createdItemId = 37;
		const title = `第 ${createdItemId} 號手動項目`;
		const emptySections = Object.fromEntries(
			SECTION_FIELD_KEYS.map((key) => [key, ""]),
		);
		mockedApiPost.mockImplementation(() => new Promise(() => {}));
		renderWithAppProviders(<ItemNewPage />, {
			initialEntries: ["/items/new"],
		});

		const titleInput = await screen.findByRole("textbox", { name: "標題" });
		fireEvent.change(titleInput, { target: { value: `  ${title}  ` } });
		fireEvent.click(screen.getByRole("button", { name: "建立" }));

		await waitFor(() => {
			expectOnlyWriteCall(writeApiMocks, mockedApiPost, [
				"/api/items",
				{
					body: {
						title,
						status: "capture-quick",
						stage: "quick",
						tags: [],
						...emptySections,
					},
				},
			]);
		});
	}, 10_000);
});
