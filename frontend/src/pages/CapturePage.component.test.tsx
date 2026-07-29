// @vitest-environment jsdom

import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { getDefaultStore } from "jotai";
import { beforeEach, describe, it, type Mock, vi } from "vitest";
import type { ApiSuccessResponse } from "../api/client.js";
import { apiDelete, apiPatch, apiPost } from "../api/client.js";
import { llmStatusAtom } from "../atoms/llm.js";
import { renderWithAppProviders } from "../test/render.js";
import { expectOnlyWriteCall } from "../test/writeCalls.js";
import { CapturePage } from "./CapturePage.jsx";

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

type CaptureResponse = ApiSuccessResponse<"/api/capture", "post">;

const capturedItemId = 37;
const captureResponse = {
	item: {
		alternatives: "",
		assumptions: "",
		confidence: "mixed",
		consequences: "",
		constraints: "",
		created: "2026-07-29T10:00:00Z",
		decisions: "",
		evidence: "",
		id: capturedItemId,
		inferred: "",
		is_stale: false,
		known: "",
		next_actions: "",
		open_questions: "",
		rationale: "",
		recovery_files: "",
		recovery_keywords: "",
		recovery_people: "",
		resume_trigger: "",
		risks: "",
		snapshot: "Capture request assertion fixture",
		source: "llm-capture",
		stage: "quick",
		status: "capture-quick",
		tags: ["request-assertion"],
		title: `第 ${capturedItemId} 號捕捉結果`,
		unknown: "",
		updated: "2026-07-29T10:00:00Z",
		why_matters: "",
	},
	questions: [],
} satisfies CaptureResponse;

describe("CapturePage money-spending request", () => {
	beforeEach(() => {
		for (const apiMock of writeApiMocks) {
			apiMock.mockReset();
		}
		getDefaultStore().set(llmStatusAtom, {
			loaded: true,
			loading: false,
			configured: true,
			model: null,
			error: null,
		});
	});

	it("posts the trimmed discussion to the capture schema path", async () => {
		const user = userEvent.setup();
		const rawText = `第 ${capturedItemId} 次架構討論的原始內容`;
		mockedApiPost.mockResolvedValue(captureResponse);
		renderWithAppProviders(<CapturePage />, {
			initialEntries: ["/capture"],
		});

		await user.type(await screen.findByRole("textbox"), `  ${rawText}  `);
		await user.click(screen.getByRole("button", { name: "AI 快速捕捉" }));

		await waitFor(() => {
			expectOnlyWriteCall(writeApiMocks, mockedApiPost, [
				"/api/capture",
				{ body: { raw_text: rawText } },
			]);
		});
	});
});
