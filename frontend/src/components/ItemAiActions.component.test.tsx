// @vitest-environment jsdom

import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { getDefaultStore } from "jotai";
import { beforeEach, describe, expect, it, type Mock, vi } from "vitest";
import type { ApiSuccessResponse } from "../api/client.js";
import { apiPost } from "../api/client.js";
import { llmStatusAtom } from "../atoms/llm.js";
import { renderWithAppProviders } from "../test/render.js";
import { ItemAiActions } from "./ItemAiActions.jsx";

vi.mock("../api/client.js", () => ({
	apiPost: vi.fn(),
}));

type MockApiOptions = {
	path?: Record<string, string | number>;
	body?: unknown;
};

const mockedApiPost = vi.mocked(apiPost) as unknown as Mock<
	(path: string, options: MockApiOptions) => Promise<unknown>
>;

type ItemDetailResponse = ApiSuccessResponse<"/api/items/{item_id}", "get">;
type EnrichResponse = ApiSuccessResponse<"/api/items/{item_id}/enrich", "post">;
type AssistUpdateResponse = ApiSuccessResponse<
	"/api/items/{item_id}/assist-update",
	"post"
>;

const propItemId = 37;
const item = {
	alternatives: "",
	assumptions: "",
	confidence: "mixed",
	consequences: "",
	constraints: "",
	created: "2026-07-29T09:00:00Z",
	decisions: "",
	evidence: "",
	id: propItemId,
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
	snapshot: "AI request assertion fixture",
	source: "manual",
	stage: "quick",
	status: "capture-quick",
	tags: ["request-assertion"],
	title: `第 ${propItemId} 號 AI 項目`,
	unknown: "",
	updated: "2026-07-29T09:00:00Z",
	why_matters: "",
} satisfies ItemDetailResponse;

function renderActions() {
	return renderWithAppProviders(
		<ItemAiActions
			item={item}
			pending={false}
			onPendingChange={vi.fn()}
			queryItemId={`route-${propItemId}`}
		/>,
	);
}

describe("ItemAiActions money-spending requests", () => {
	beforeEach(() => {
		mockedApiPost.mockReset();
		getDefaultStore().set(llmStatusAtom, {
			loaded: true,
			loading: false,
			configured: true,
			model: null,
			error: null,
		});
	});

	it("posts enrichment context for the item supplied by props", async () => {
		const user = userEvent.setup();
		const context = `第 ${propItemId} 號項目的補充背景`;
		mockedApiPost.mockResolvedValue({
			gaps: [],
			item,
		} satisfies EnrichResponse);
		renderActions();

		await user.type(
			await screen.findByRole("textbox", { name: "補充背景" }),
			`  ${context}  `,
		);
		await user.click(screen.getByRole("button", { name: "AI 補齊" }));

		await waitFor(() => {
			expect(apiPost).toHaveBeenCalledWith("/api/items/{item_id}/enrich", {
				path: { item_id: String(propItemId) },
				body: { additional_context: context },
			});
		});
	});

	it("posts an assisted update for the item supplied by props", async () => {
		const user = userEvent.setup();
		const note = `第 ${propItemId} 號項目的進度說明`;
		mockedApiPost.mockResolvedValue({
			item,
		} satisfies AssistUpdateResponse);
		renderActions();

		await user.type(
			await screen.findByRole("textbox", { name: "進度說明" }),
			`  ${note}  `,
		);
		await user.click(screen.getByRole("button", { name: "AI 進度更新" }));

		await waitFor(() => {
			expect(apiPost).toHaveBeenCalledWith(
				"/api/items/{item_id}/assist-update",
				{
					path: { item_id: String(propItemId) },
					body: { note },
				},
			);
		});
	});
});
