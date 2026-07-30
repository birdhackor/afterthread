// @vitest-environment jsdom

import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { getDefaultStore } from "jotai";
import { beforeEach, describe, expect, it, type Mock, vi } from "vitest";
import type { ApiSuccessResponse } from "../api/client.js";
import { apiDelete, apiGet, apiPatch, apiPost } from "../api/client.js";
import { llmStatusAtom } from "../atoms/llm.js";
import { renderWithAppProviders } from "../test/render.js";
import { expectOnlyWriteCall } from "../test/writeCalls.js";
import { ItemDetailPage } from "./ItemDetailPage.jsx";

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

const mockedApiDelete = vi.mocked(apiDelete) as unknown as Mock<
	(path: string, options?: MockApiOptions) => Promise<unknown>
>;
const mockedApiGet = vi.mocked(apiGet) as unknown as Mock<
	(path: string, options?: MockApiOptions) => Promise<unknown>
>;
const mockedApiPatch = vi.mocked(apiPatch) as unknown as Mock<
	(path: string, options: MockApiOptions) => Promise<unknown>
>;
const mockedApiPost = vi.mocked(apiPost) as unknown as Mock<
	(path: string, options: MockApiOptions) => Promise<unknown>
>;
const writeApiMocks = [mockedApiDelete, mockedApiPatch, mockedApiPost];

type ItemDetailResponse = ApiSuccessResponse<"/api/items/{item_id}", "get">;
type ItemDeleteResponse = ApiSuccessResponse<"/api/items/{item_id}", "delete">;
type ProgressResponse = ApiSuccessResponse<
	"/api/items/{item_id}/progress",
	"post"
>;

const routeItemId = "37";
const item = {
	alternatives: "",
	assumptions: "",
	confidence: "mixed",
	consequences: "",
	constraints: "",
	created: "2026-07-28T09:00:00Z",
	decisions: "",
	evidence: "",
	id: Number(routeItemId),
	inferred: "",
	is_stale: false,
	known: "",
	next_actions: "",
	open_questions: "",
	progress: [],
	rationale: "",
	recovery_files: "",
	recovery_keywords: "",
	recovery_people: "",
	resume_trigger: "",
	risks: "",
	snapshot: "保留這份唯一記憶",
	source: "manual",
	stage: "quick",
	status: "capture-quick",
	tags: ["round-2"],
	title: `第 ${routeItemId} 號唯一記憶`,
	unknown: "",
	updated: "2026-07-28T09:00:00Z",
	why_matters: "",
} satisfies ItemDetailResponse;

const expectedItemReadCall = [
	"/api/items/{item_id}",
	{ path: { item_id: routeItemId } },
] as const;

function mockItemReadResponses(...responses: ItemDetailResponse[]) {
	mockedApiGet.mockImplementation((...call) => {
		// A permissive fixture hides a wrong route target by rendering plausible
		// content for it, even while the later write still targets this route.
		expect(call).toEqual(expectedItemReadCall);
		const response = responses[mockedApiGet.mock.calls.length - 1];
		if (response === undefined) {
			throw new Error("Unexpected additional item read");
		}
		return Promise.resolve(response);
	});
}

function expectItemReads(count = 1) {
	expect(mockedApiGet.mock.calls).toEqual(
		Array.from({ length: count }, () => expectedItemReadCall),
	);
}

function renderDetailPage() {
	return renderWithAppProviders(<ItemDetailPage />, {
		initialEntries: [`/items/${routeItemId}`],
		routePath: "/items/$itemId",
	});
}

describe("ItemDetailPage destructive and rewriting requests", () => {
	beforeEach(() => {
		for (const apiMock of [apiDelete, apiGet, apiPatch, apiPost]) {
			vi.mocked(apiMock).mockReset();
		}
		getDefaultStore().set(llmStatusAtom, {
			loaded: true,
			loading: false,
			configured: true,
			// The atom is authored in JS with `model: null`, so its inferred type is
			// the literal null -- and a model name is irrelevant here anyway: these
			// tests assert requests, not anything the model name renders.
			model: null,
			error: null,
		});
		mockItemReadResponses(item);
	});

	it("deletes the item identified by the route and names that item in the success notification", async () => {
		const user = userEvent.setup();
		mockedApiDelete.mockResolvedValue(null satisfies ItemDeleteResponse);
		renderDetailPage();

		await waitFor(() => expectItemReads());
		expect(
			await screen.findByRole("heading", { name: item.title }),
		).toBeInTheDocument();
		await user.click(screen.getByRole("button", { name: "刪除" }));
		const dialog = await screen.findByRole("dialog", { name: "刪除項目" });
		await user.click(within(dialog).getByRole("button", { name: "刪除" }));

		await waitFor(() => {
			expectOnlyWriteCall(writeApiMocks, mockedApiDelete, [
				"/api/items/{item_id}",
				{ path: { item_id: routeItemId } },
			]);
		});
		expect(
			await screen.findByText(`已刪除「${item.title}」`),
		).toBeInTheDocument();
		expectItemReads();
	});

	it("patches status on the item identified by the route", async () => {
		const user = userEvent.setup();
		mockItemReadResponses(item, { ...item, status: "active" });
		mockedApiPatch.mockResolvedValue({ ...item, status: "active" });
		renderDetailPage();

		await waitFor(() => expectItemReads());
		expect(
			await screen.findByRole("heading", { name: item.title }),
		).toBeInTheDocument();
		const status = screen.getByRole("combobox", { name: "狀態" });
		await user.click(status);
		await user.keyboard("{ArrowDown}{ArrowDown}{Enter}");

		await waitFor(() => {
			expectOnlyWriteCall(writeApiMocks, mockedApiPatch, [
				"/api/items/{item_id}",
				{
					path: { item_id: routeItemId },
					body: { status: "active" },
				},
			]);
		});
		expect(await screen.findByRole("combobox", { name: "狀態" })).toHaveValue(
			"進行中",
		);
		// The refetched value can commit before the mutation clears its final gate.
		await waitFor(() => {
			expect(screen.getByRole("combobox", { name: "狀態" })).toBeEnabled();
		});
		expectItemReads(2);
	});

	it("posts a progress rewrite to the route item with the submitted note", async () => {
		const user = userEvent.setup();
		const note = `第 ${routeItemId} 號項目的新進度`;
		const progress = {
			date: "2026-07-29T08:30:00Z",
			id: 501,
			item_id: item.id,
			note,
		} satisfies ProgressResponse;
		mockItemReadResponses(item, { ...item, progress: [progress] });
		mockedApiPost.mockResolvedValue(progress);
		renderDetailPage();

		await waitFor(() => expectItemReads());
		const input = await screen.findByRole("textbox", { name: "新增進度" });
		await user.type(input, `  ${note}  `);
		await user.click(screen.getByRole("button", { name: "新增進度" }));

		await waitFor(() => {
			expectOnlyWriteCall(writeApiMocks, mockedApiPost, [
				"/api/items/{item_id}/progress",
				{
					path: { item_id: routeItemId },
					body: { note },
				},
			]);
		});
		expect(await screen.findByText(note)).toBeInTheDocument();
		// The refetched note can commit before the mutation clears its final gate.
		await waitFor(() => {
			expect(screen.getByRole("button", { name: "新增進度" })).toBeEnabled();
		});
		expectItemReads(2);
	});
});
