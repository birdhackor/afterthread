// @vitest-environment jsdom

import { act, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ApiSuccessResponse } from "../api/client.js";
import { apiGet } from "../api/client.js";
import { renderWithAppProviders } from "../test/render.js";
import { ToolsPage } from "./ToolsPage.jsx";

vi.mock("../api/client.js", () => ({
	apiDelete: vi.fn(),
	apiGet: vi.fn(),
	apiPatch: vi.fn(),
	apiPost: vi.fn(),
}));

type ToolListResponse = ApiSuccessResponse<"/api/tools", "get">;

const toolListResponse = {
	tools: [
		{
			name: "weather-search",
			description: "依城市查詢即時天氣",
			enabled: true,
			valid: true,
			error: null,
			current_vid: "v-weather-1",
			lineage: "sole",
		},
	],
} satisfies ToolListResponse;

function deferred<T>() {
	let resolve!: (value: T) => void;
	const promise = new Promise<T>((settle) => {
		resolve = settle;
	});
	return { promise, resolve };
}

describe("ToolsPage component smoke", () => {
	it("transitions from loading to the mocked tools response", async () => {
		const request = deferred<ToolListResponse>();
		vi.mocked(apiGet).mockImplementation(() => request.promise);

		renderWithAppProviders(<ToolsPage />);

		await waitFor(() => {
			expect(apiGet).toHaveBeenCalledWith("/api/tools");
			expect(screen.getByRole("button", { name: "重新整理" })).toBeDisabled();
		});

		await act(async () => {
			request.resolve(toolListResponse);
			await request.promise;
		});

		expect(await screen.findByText("weather-search")).toBeInTheDocument();
		expect(screen.getByText("依城市查詢即時天氣")).toBeInTheDocument();
		await waitFor(() => {
			expect(screen.getByRole("button", { name: "重新整理" })).toBeEnabled();
		});
	});
});
