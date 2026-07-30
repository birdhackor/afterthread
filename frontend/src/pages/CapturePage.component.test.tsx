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
		// This request-only case keeps success-side rendering and invalidation out
		// of scope so they cannot commit after the test has already finished.
		mockedApiPost.mockImplementation(
			() => new Promise<CaptureResponse>(() => {}),
		);
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
