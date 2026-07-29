// @vitest-environment jsdom

import { fireEvent, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, type Mock, vi } from "vitest";
import type { ApiSuccessResponse } from "../api/client.js";
import { apiGet, apiPatch } from "../api/client.js";
import { renderWithAppProviders } from "../test/render.js";
import { ItemEditPage } from "./ItemEditPage.jsx";

vi.mock("../api/client.js", () => ({
	apiGet: vi.fn(),
	apiPatch: vi.fn(),
}));

type MockApiOptions = {
	path?: Record<string, string | number>;
	body?: unknown;
};

const mockedApiGet = vi.mocked(apiGet) as unknown as Mock<
	(path: string, options?: MockApiOptions) => Promise<unknown>
>;
const mockedApiPatch = vi.mocked(apiPatch) as unknown as Mock<
	(path: string, options: MockApiOptions) => Promise<unknown>
>;

type ItemDetailResponse = ApiSuccessResponse<"/api/items/{item_id}", "get">;

const routeItemId = "37";
const item = {
	alternatives: "",
	assumptions: "",
	confidence: "mixed",
	consequences: "",
	constraints: "",
	created: "2026-07-29T09:00:00Z",
	decisions: "",
	evidence: "",
	id: Number(routeItemId),
	inferred: "",
	is_stale: false,
	known: "",
	next_actions: "",
	open_questions: "",
	progress: [],
	rationale: "這是唯一保存的理由",
	recovery_files: "",
	recovery_keywords: "",
	recovery_people: "",
	resume_trigger: "",
	risks: "",
	snapshot: "這是唯一保存的快照",
	source: "manual",
	stage: "quick",
	status: "capture-quick",
	tags: ["request-assertion"],
	title: `第 ${routeItemId} 號唯一記憶`,
	unknown: "",
	updated: "2026-07-29T09:00:00Z",
	why_matters: "",
} satisfies ItemDetailResponse;

function renderEditPage() {
	return renderWithAppProviders(<ItemEditPage />, {
		initialEntries: [`/items/${routeItemId}/edit`],
		routePath: "/items/$itemId/edit",
	});
}

describe("ItemEditPage sole-copy rewrite request", () => {
	beforeEach(() => {
		mockedApiGet.mockReset();
		mockedApiPatch.mockReset();
		mockedApiGet.mockResolvedValue(item);
	});

	// Full-form Mantine mounts approach Vitest's 5 s default under parallel
	// jsdom load; waitFor keeps the request assertion's own deadline short.
	it("patches edited content on the item identified by the route", async () => {
		const editedTitle = `第 ${routeItemId} 號更新後的唯一記憶`;
		mockedApiPatch.mockImplementation(() => new Promise(() => {}));
		renderEditPage();

		const title = await screen.findByRole("textbox", { name: "標題" });
		fireEvent.change(title, { target: { value: `  ${editedTitle}  ` } });
		fireEvent.click(screen.getByRole("button", { name: "儲存變更" }));

		await waitFor(() => {
			expect(apiPatch).toHaveBeenCalledWith("/api/items/{item_id}", {
				path: { item_id: routeItemId },
				body: { title: editedTitle },
			});
		});
	}, 10_000);
});
