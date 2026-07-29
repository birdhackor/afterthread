// @vitest-environment jsdom

import { act, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
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
type ToolSummaryResponse = ApiSuccessResponse<
	"/api/tools/{name}/summary",
	"get"
>;

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

describe("ToolsPage component", () => {
	beforeEach(() => {
		vi.mocked(apiGet).mockReset();
	});

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

	it("drops revision feedback when the current version changes without visible row changes", async () => {
		const user = userEvent.setup();
		const versionV = {
			tools: [
				{
					...toolListResponse.tools[0],
					current_vid: "v-weather-revision",
					lineage: "usable",
				},
			],
		} satisfies ToolListResponse;
		const versionP = {
			tools: [
				{
					...versionV.tools[0],
					current_vid: "v-weather-parent",
				},
			],
		} satisfies ToolListResponse;
		const summaryV = {
			current_vid: versionV.tools[0].current_vid,
			summary: "版本 V 的工具總結",
			updated_at: null,
			llm_log_id: null,
		} satisfies ToolSummaryResponse;
		const summaryP = {
			...summaryV,
			current_vid: versionP.tools[0].current_vid,
			summary: "版本 P 的工具總結",
		} satisfies ToolSummaryResponse;
		let currentList = versionV;
		let currentSummary = summaryV;

		vi.mocked(apiGet).mockImplementation((path) => {
			if (path === "/api/tools") {
				return Promise.resolve(currentList);
			}
			if (path === "/api/tools/weather-search/summary") {
				return Promise.resolve(currentSummary);
			}
			throw new Error(`Unexpected GET ${path}`);
		});

		renderWithAppProviders(<ToolsPage />);

		expect(await screen.findByText("weather-search")).toBeInTheDocument();
		await waitFor(() => {
			expect(screen.getByRole("button", { name: "重新整理" })).toBeEnabled();
		});
		await user.click(screen.getByRole("button", { name: "AI 總結" }));
		expect(await screen.findByText(summaryV.summary)).toBeInTheDocument();

		const feedback = screen.getByRole("textbox", { name: "修訂意見" });
		await user.type(feedback, "只適用於版本 V 的修訂方向");
		expect(feedback).toHaveValue("只適用於版本 V 的修訂方向");

		currentList = versionP;
		currentSummary = summaryP;
		await user.click(screen.getByRole("button", { name: "重新整理" }));
		await waitFor(() => {
			const listRequests = vi
				.mocked(apiGet)
				.mock.calls.filter(([path]) => path === "/api/tools");
			expect(listRequests).toHaveLength(2);
			expect(screen.getByRole("button", { name: "重新整理" })).toBeEnabled();
		});

		// A correctly keyed row remounts collapsed. The deliberately broken key
		// used by the mutation proof leaves it expanded, so only open it when the
		// remount actually happened; both paths then expose the same final field.
		const collapsedSummaryButton = screen.queryByRole("button", {
			name: "AI 總結",
		});
		if (collapsedSummaryButton) {
			await user.click(collapsedSummaryButton);
		}

		expect(await screen.findByText(summaryP.summary)).toBeInTheDocument();
		expect(screen.getByRole("textbox", { name: "修訂意見" })).toHaveValue("");
	});
});
