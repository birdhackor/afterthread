// @vitest-environment jsdom

import { fireEvent, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, type Mock, vi } from "vitest";
import type { ApiSuccessResponse } from "../api/client.js";
import { apiDelete, apiGet, apiPatch, apiPost } from "../api/client.js";
import { renderWithAppProviders } from "../test/render.js";
import { expectOnlyWriteCall } from "../test/writeCalls.js";
import { ItemEditPage } from "./ItemEditPage.jsx";

vi.mock("../api/client.js", () => ({
	apiDelete: vi.fn(),
	apiGet: vi.fn(),
	apiPatch: vi.fn(),
	apiPost: vi.fn(),
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
const writeApiMocks = [
	vi.mocked(apiDelete),
	mockedApiPatch,
	vi.mocked(apiPost),
];

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

const expectedItemReadCall = [
	"/api/items/{item_id}",
	{ path: { item_id: routeItemId } },
] as const;

function mockExpectedItemRead() {
	mockedApiGet.mockImplementation((...call) => {
		// A permissive fixture hides a wrong route target by pre-filling this form
		// with believable content that belongs to a different item.
		expect(call).toEqual(expectedItemReadCall);
		return Promise.resolve(item);
	});
}

function expectItemRead() {
	expect(mockedApiGet.mock.calls).toEqual([expectedItemReadCall]);
}

function renderEditPage() {
	return renderWithAppProviders(<ItemEditPage />, {
		initialEntries: [`/items/${routeItemId}/edit`],
		routePath: "/items/$itemId/edit",
	});
}

describe("ItemEditPage sole-copy rewrite request", () => {
	beforeEach(() => {
		mockedApiGet.mockReset();
		for (const apiMock of writeApiMocks) {
			apiMock.mockReset();
		}
		mockExpectedItemRead();
	});

	// Full-form Mantine mounts approach Vitest's 5 s default under parallel
	// jsdom load; waitFor keeps the request assertion's own deadline short.
	it("patches edited content on the item identified by the route", async () => {
		const editedTitle = `第 ${routeItemId} 號更新後的唯一記憶`;
		mockedApiPatch.mockImplementation(() => new Promise(() => {}));
		renderEditPage();

		await waitFor(expectItemRead);
		const title = await screen.findByRole("textbox", { name: "標題" });
		fireEvent.change(title, { target: { value: `  ${editedTitle}  ` } });
		fireEvent.click(screen.getByRole("button", { name: "儲存變更" }));

		await waitFor(() => {
			expectOnlyWriteCall(writeApiMocks, mockedApiPatch, [
				"/api/items/{item_id}",
				{
					path: { item_id: routeItemId },
					body: { title: editedTitle },
				},
			]);
		});
		expectItemRead();
	}, 10_000);
});
