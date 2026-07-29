// @vitest-environment jsdom

import { fireEvent, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, type Mock, vi } from "vitest";
import { apiPost } from "../api/client.js";
import { SECTION_FIELD_KEYS } from "../constants/sections.js";
import { renderWithAppProviders } from "../test/render.js";
import { ItemNewPage } from "./ItemNewPage.jsx";

vi.mock("../api/client.js", () => ({
	apiPost: vi.fn(),
}));

type MockApiOptions = {
	body?: unknown;
};

const mockedApiPost = vi.mocked(apiPost) as unknown as Mock<
	(path: string, options: MockApiOptions) => Promise<unknown>
>;

describe("ItemNewPage create request", () => {
	beforeEach(() => {
		mockedApiPost.mockReset();
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
			expect(apiPost).toHaveBeenCalledWith("/api/items", {
				body: {
					title,
					status: "capture-quick",
					stage: "quick",
					tags: [],
					...emptySections,
				},
			});
		});
	}, 10_000);
});
