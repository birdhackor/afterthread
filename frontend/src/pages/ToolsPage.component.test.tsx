// @vitest-environment jsdom

import {
	act,
	fireEvent,
	screen,
	waitFor,
	waitForElementToBeRemoved,
	within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, type Mock, vi } from "vitest";
import type { ApiSuccessResponse } from "../api/client.js";
import { apiDelete, apiGet, apiPatch, apiPost } from "../api/client.js";
import { renderWithAppProviders } from "../test/render.js";
import { expectOnlyWriteCall } from "../test/writeCalls.js";
import { ToolsPage } from "./ToolsPage.jsx";

vi.mock("../api/client.js", () => ({
	apiDelete: vi.fn(),
	apiGet: vi.fn(),
	apiPatch: vi.fn(),
	apiPost: vi.fn(),
}));

type MockApiOptions = {
	path?: Record<string, string | number>;
	query?: Record<string, unknown>;
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

type ToolListResponse = ApiSuccessResponse<"/api/tools", "get">;
type ToolSummaryResponse = ApiSuccessResponse<
	"/api/tools/{name}/summary",
	"get"
>;
type ToolDiscardResponse = ApiSuccessResponse<
	"/api/tools/{name}/versions/{vid}",
	"delete"
>;
type ToolDeleteResponse = ApiSuccessResponse<"/api/tools/{name}", "delete">;
type ToolToggleResponse = ApiSuccessResponse<"/api/tools/{name}", "patch">;
type ToolRegenerateResponse = ApiSuccessResponse<
	"/api/tools/{name}/summary/regenerate",
	"post"
>;
type ToolReviseAccepted = ApiSuccessResponse<
	"/api/tools/{name}/revise",
	"post"
>;
type ToolInstallAccepted = ApiSuccessResponse<"/api/tools/install", "post">;
type ToolJobResponse = ApiSuccessResponse<"/api/tools/jobs/{job_id}", "get">;

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

function toolListWith(
	overrides: Partial<ToolListResponse["tools"][number]> = {},
): ToolListResponse {
	return {
		tools: [{ ...toolListResponse.tools[0], ...overrides }],
	};
}

function summaryFor(
	tool: ToolListResponse["tools"][number],
): ToolSummaryResponse {
	if (tool.current_vid === null) {
		throw new Error(`Tool ${tool.name} has no resolved version`);
	}
	return {
		current_vid: tool.current_vid,
		summary: `${tool.name} 的工具總結`,
		updated_at: null,
		llm_log_id: null,
	};
}

function regenerateResponseFor(
	tool: ToolListResponse["tools"][number],
): ToolRegenerateResponse {
	return summaryFor(tool);
}

function apiError(code: string) {
	return Object.assign(new Error(`測試衝突：${code}`), {
		status: 409,
		code,
	});
}

function deferred<T>() {
	let resolve!: (value: T) => void;
	let reject!: (reason?: unknown) => void;
	const promise = new Promise<T>((settle, fail) => {
		resolve = settle;
		reject = fail;
	});
	return { promise, resolve, reject };
}

function toolListRequestCount() {
	return mockedApiGet.mock.calls.filter(([path]) => path === "/api/tools")
		.length;
}

function mockToolReads(list: ToolListResponse) {
	mockedApiGet.mockImplementation((path, options) => {
		if (path === "/api/tools") {
			return Promise.resolve(list);
		}
		const tool = list.tools.find(
			(candidate) =>
				path === "/api/tools/{name}/summary" &&
				options?.path?.name === candidate.name,
		);
		if (tool) {
			return Promise.resolve(summaryFor(tool));
		}
		throw new Error(`Unexpected GET ${path}`);
	});
}

async function waitForTool(name = "weather-search") {
	expect(await screen.findByText(name)).toBeInTheDocument();
	await waitFor(() => {
		expect(screen.getByRole("button", { name: "重新整理" })).toBeEnabled();
	});
}

async function openDiscardConfirmation(
	user: ReturnType<typeof userEvent.setup>,
) {
	await user.click(screen.getByRole("button", { name: "丟掉這一版" }));
	expect(
		await screen.findByRole("dialog", { name: "退回前一版" }),
	).toBeInTheDocument();
}

describe("ToolsPage component", () => {
	beforeEach(() => {
		for (const apiMock of [apiDelete, apiGet, apiPatch, apiPost]) {
			vi.mocked(apiMock).mockReset();
		}
	});

	it("transitions from loading to the mocked tools response", async () => {
		const request = deferred<ToolListResponse>();
		mockedApiGet.mockImplementation(() => request.promise);

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

	it("toggles the tool identity supplied by the tools response", async () => {
		const user = userEvent.setup();
		const list = toolListWith({ name: "request-assertion-tool-37" });
		const tool = list.tools[0];
		mockToolReads(list);
		mockedApiPatch.mockResolvedValue({
			...tool,
			enabled: false,
		} satisfies ToolToggleResponse);

		renderWithAppProviders(<ToolsPage />);
		await waitForTool(tool.name);
		await user.click(screen.getByRole("switch", { name: "啟用" }));

		await waitFor(() => {
			expectOnlyWriteCall(writeApiMocks, mockedApiPatch, [
				"/api/tools/{name}",
				{
					path: { name: tool.name },
					body: { enabled: false },
				},
			]);
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

		mockedApiGet.mockImplementation((path, options) => {
			if (path === "/api/tools") {
				return Promise.resolve(currentList);
			}
			if (
				path === "/api/tools/{name}/summary" &&
				options?.path?.name === "weather-search"
			) {
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

	it("refetches the tools list after a discard version mismatch", async () => {
		const user = userEvent.setup();
		const list = toolListWith({ lineage: "usable" });
		mockToolReads(list);
		mockedApiDelete.mockRejectedValue(apiError("version_mismatch"));

		const { queryClient } = renderWithAppProviders(<ToolsPage />);
		await waitForTool();
		const invalidate = vi.spyOn(queryClient, "invalidateQueries");
		await openDiscardConfirmation(user);
		await user.click(screen.getByRole("button", { name: "丟掉並退回" }));

		await waitFor(() => {
			expect(toolListRequestCount()).toBe(2);
		});
		expect(invalidate).toHaveBeenCalledWith({
			queryKey: ["tools"],
			exact: true,
		});
	});

	it("closes discard confirmation when the displayed version changes", async () => {
		const user = userEvent.setup();
		const versionV = toolListWith({
			current_vid: "v-weather-revision",
			lineage: "usable",
		});
		const versionP = toolListWith({
			current_vid: "v-weather-parent",
			lineage: "usable",
		});
		mockedApiGet
			.mockImplementationOnce(() => Promise.resolve(versionV))
			.mockImplementation(() => Promise.resolve(versionP));

		const { queryClient } = renderWithAppProviders(<ToolsPage />);
		await waitForTool();
		await openDiscardConfirmation(user);
		const removal = waitForElementToBeRemoved(
			() => screen.queryByRole("dialog", { name: "退回前一版" }),
			{ timeout: 1000 },
		);

		await act(async () => {
			await queryClient.invalidateQueries({
				queryKey: ["tools"],
				exact: true,
			});
		});

		await removal;
		expect(toolListRequestCount()).toBe(2);
		expect(apiDelete).not.toHaveBeenCalled();
	});

	it.each([
		"job_busy",
		"ai_job_in_progress",
	])("keeps the discard confirmation open for retryable %s conflicts", async (code) => {
		const user = userEvent.setup();
		const list = toolListWith({ lineage: "usable" });
		mockToolReads(list);
		mockedApiDelete.mockRejectedValue(apiError(code));

		renderWithAppProviders(<ToolsPage />);
		await waitForTool();
		await openDiscardConfirmation(user);
		await user.click(screen.getByRole("button", { name: "丟掉並退回" }));

		await waitFor(() => {
			expect(screen.getByRole("button", { name: "丟掉並退回" })).toBeEnabled();
		});
		await expect(
			waitForElementToBeRemoved(
				() => screen.queryByRole("dialog", { name: "退回前一版" }),
				{ timeout: 1000 },
			),
		).rejects.toThrow();
		expect(toolListRequestCount()).toBe(1);
	});

	it("marks a late lineage conflict broken without refetching", async () => {
		const user = userEvent.setup();
		const list = toolListWith({ lineage: "usable" });
		mockToolReads(list);
		mockedApiDelete.mockRejectedValue(apiError("lineage_unavailable"));

		const { queryClient } = renderWithAppProviders(<ToolsPage />);
		await waitForTool();
		const invalidate = vi.spyOn(queryClient, "invalidateQueries");
		await openDiscardConfirmation(user);
		await user.click(screen.getByRole("button", { name: "丟掉並退回" }));

		expect(await screen.findByText("無法退回前一版")).toBeInTheDocument();
		await waitFor(() => {
			expect(
				screen.queryByRole("dialog", { name: "退回前一版" }),
			).not.toBeInTheDocument();
		});
		expect(
			screen.queryByRole("button", { name: "丟掉這一版" }),
		).not.toBeInTheDocument();
		expect(invalidate).not.toHaveBeenCalled();
		expect(toolListRequestCount()).toBe(1);
	});

	it("offers sole lineage as whole-tool deletion", async () => {
		const user = userEvent.setup();
		mockToolReads(toolListWith({ lineage: "sole" }));

		renderWithAppProviders(<ToolsPage />);
		await waitForTool();

		expect(
			screen.getByText("目前只有這一版；丟掉後會刪除整個工具。"),
		).toBeInTheDocument();
		await user.click(screen.getByRole("button", { name: "丟掉這一版" }));
		expect(
			await screen.findByRole("dialog", { name: "刪除工具" }),
		).toBeInTheDocument();
		expect(
			screen.queryByRole("dialog", { name: "退回前一版" }),
		).not.toBeInTheDocument();
	});

	it("offers usable lineage as version discard", async () => {
		const user = userEvent.setup();
		mockToolReads(toolListWith({ lineage: "usable" }));

		renderWithAppProviders(<ToolsPage />);
		await waitForTool();

		expect(
			screen.getByText("可丟掉目前版本，回到前一版。"),
		).toBeInTheDocument();
		await openDiscardConfirmation(user);
		expect(
			screen.queryByRole("dialog", { name: "刪除工具" }),
		).not.toBeInTheDocument();
	});

	it("offers broken lineage only whole-tool deletion", async () => {
		const user = userEvent.setup();
		mockToolReads(toolListWith({ lineage: "broken" }));

		renderWithAppProviders(<ToolsPage />);
		await waitForTool();

		expect(screen.getByText("無法退回前一版")).toBeInTheDocument();
		expect(
			screen.queryByRole("button", { name: "丟掉這一版" }),
		).not.toBeInTheDocument();
		await user.click(screen.getByRole("button", { name: "刪除" }));
		expect(
			await screen.findByRole("dialog", { name: "刪除工具" }),
		).toBeInTheDocument();
	});

	it("sends the exact version-discard request and renders a physical removal", async () => {
		const user = userEvent.setup();
		const list = toolListWith({ lineage: "usable" });
		mockToolReads(list);
		mockedApiDelete.mockResolvedValue({
			outcome: "removed",
			retained_path: null,
			retention_reason: null,
		} satisfies ToolDiscardResponse);

		renderWithAppProviders(<ToolsPage />);
		await waitForTool();
		await openDiscardConfirmation(user);
		await user.click(screen.getByRole("button", { name: "丟掉並退回" }));

		expectOnlyWriteCall(writeApiMocks, mockedApiDelete, [
			"/api/tools/{name}/versions/{vid}",
			{
				path: {
					name: list.tools[0].name,
					vid: list.tools[0].current_vid,
				},
			},
		]);
		expect(
			await screen.findByText(
				"已丟掉「weather-search」的目前版本並退回前一版；原版本檔案已移除",
			),
		).toBeInTheDocument();
		expect(screen.queryByText(/請勿手動刪除/)).not.toBeInTheDocument();
	});

	it("renders durability-unconfirmed retention as unsafe for manual cleanup", async () => {
		const user = userEvent.setup();
		const list = toolListWith({ lineage: "usable" });
		const retainedPath =
			"/srv/afterthread/tools/weather-search/versions/v-weather-1";
		mockToolReads(list);
		mockedApiDelete.mockResolvedValue({
			outcome: "retained",
			retained_path: retainedPath,
			retention_reason: "durability_unconfirmed",
		} satisfies ToolDiscardResponse);

		renderWithAppProviders(<ToolsPage />);
		await waitForTool();
		await openDiscardConfirmation(user);
		await user.click(screen.getByRole("button", { name: "丟掉並退回" }));

		expect(
			await screen.findByText("已退回前一版，但持久化尚未確認"),
		).toBeInTheDocument();
		expect(
			screen.getByText((content) =>
				content.includes(`請勿手動刪除 ${retainedPath}`),
			),
		).toBeInTheDocument();
		expect(screen.queryByText(/再手動刪除該目錄/)).not.toBeInTheDocument();
	});

	it("blocks every version write while the tools list is refetching", async () => {
		const user = userEvent.setup();
		const list = toolListWith({ lineage: "usable" });
		const listRefresh = deferred<ToolListResponse>();
		let listReads = 0;
		mockedApiGet.mockImplementation((path, options) => {
			if (path === "/api/tools") {
				listReads += 1;
				return listReads === 1 ? Promise.resolve(list) : listRefresh.promise;
			}
			if (
				path === "/api/tools/{name}/summary" &&
				options?.path?.name === "weather-search"
			) {
				return Promise.resolve(summaryFor(list.tools[0]));
			}
			throw new Error(`Unexpected GET ${path}`);
		});

		renderWithAppProviders(<ToolsPage />);
		await waitForTool();
		await user.click(screen.getByRole("button", { name: "AI 總結" }));
		expect(
			await screen.findByText("weather-search 的工具總結"),
		).toBeInTheDocument();
		await user.type(
			screen.getByRole("textbox", { name: "修訂意見" }),
			"清單重讀期間不可送出",
		);
		await openDiscardConfirmation(user);
		await user.click(screen.getByRole("button", { name: "重新整理" }));

		await waitFor(() => {
			expect(toolListRequestCount()).toBe(2);
		});
		expect(screen.getByRole("button", { name: "重新產生" })).toBeDisabled();
		expect(screen.getByRole("textbox", { name: "修訂意見" })).toBeDisabled();
		expect(screen.getByRole("button", { name: "送出修訂" })).toBeDisabled();
		expect(screen.getByRole("button", { name: "丟掉並退回" })).toBeDisabled();
		await user.click(screen.getByRole("button", { name: "重新產生" }));
		await user.click(screen.getByRole("button", { name: "丟掉並退回" }));
		const reviseForm = screen
			.getByRole("textbox", { name: "修訂意見" })
			.closest("form");
		expect(reviseForm).not.toBeNull();
		await act(async () => {
			fireEvent.submit(reviseForm as HTMLFormElement);
		});
		expect(apiPost).not.toHaveBeenCalled();
		expect(apiDelete).not.toHaveBeenCalled();

		await act(async () => {
			listRefresh.resolve(list);
			await listRefresh.promise;
		});
	});

	it("blocks every version write after a background tools-list failure", async () => {
		const user = userEvent.setup();
		const list = toolListWith({ lineage: "usable" });
		let listReads = 0;
		mockedApiGet.mockImplementation((path, options) => {
			if (path === "/api/tools") {
				listReads += 1;
				return listReads === 1
					? Promise.resolve(list)
					: Promise.reject(new Error("工具清單重讀失敗"));
			}
			if (
				path === "/api/tools/{name}/summary" &&
				options?.path?.name === "weather-search"
			) {
				return Promise.resolve(summaryFor(list.tools[0]));
			}
			throw new Error(`Unexpected GET ${path}`);
		});

		renderWithAppProviders(<ToolsPage />);
		await waitForTool();
		await user.click(screen.getByRole("button", { name: "AI 總結" }));
		expect(
			await screen.findByText("weather-search 的工具總結"),
		).toBeInTheDocument();
		await openDiscardConfirmation(user);
		await user.click(screen.getByRole("button", { name: "重新整理" }));

		expect(await screen.findByText("無法更新工具清單")).toBeInTheDocument();
		expect(screen.getByRole("button", { name: "重新產生" })).toBeDisabled();
		expect(screen.getByRole("textbox", { name: "修訂意見" })).toBeDisabled();
		expect(screen.getByRole("button", { name: "丟掉並退回" })).toBeDisabled();
		await user.click(screen.getByRole("button", { name: "重新產生" }));
		await user.click(screen.getByRole("button", { name: "丟掉並退回" }));
		expect(apiPost).not.toHaveBeenCalled();
		expect(apiDelete).not.toHaveBeenCalled();
	});

	it("blocks revision authoring while its displayed summary is refreshing or stale", async () => {
		const user = userEvent.setup();
		const list = toolListWith({ lineage: "usable" });
		const summaryRefresh = deferred<ToolSummaryResponse>();
		let summaryReads = 0;
		mockedApiGet.mockImplementation((path, options) => {
			if (path === "/api/tools") {
				return Promise.resolve(list);
			}
			if (
				path === "/api/tools/{name}/summary" &&
				options?.path?.name === "weather-search"
			) {
				summaryReads += 1;
				return summaryReads === 1
					? Promise.resolve(summaryFor(list.tools[0]))
					: summaryRefresh.promise;
			}
			throw new Error(`Unexpected GET ${path}`);
		});

		const { queryClient } = renderWithAppProviders(<ToolsPage />);
		await waitForTool();
		await user.click(screen.getByRole("button", { name: "AI 總結" }));
		expect(
			await screen.findByText("weather-search 的工具總結"),
		).toBeInTheDocument();
		await user.type(
			screen.getByRole("textbox", { name: "修訂意見" }),
			"總結重讀期間不可送出",
		);

		const invalidation = queryClient.invalidateQueries({
			queryKey: ["tool-summary", "weather-search"],
		});
		await waitFor(() => {
			expect(summaryReads).toBe(2);
		});
		expect(screen.getByRole("textbox", { name: "修訂意見" })).toBeDisabled();
		expect(screen.getByRole("button", { name: "送出修訂" })).toBeDisabled();
		expect(screen.getByRole("button", { name: "重新產生" })).toBeEnabled();
		const reviseForm = screen
			.getByRole("textbox", { name: "修訂意見" })
			.closest("form");
		expect(reviseForm).not.toBeNull();
		await act(async () => {
			fireEvent.submit(reviseForm as HTMLFormElement);
		});
		expect(apiPost).not.toHaveBeenCalled();

		await act(async () => {
			summaryRefresh.reject(new Error("總結重讀失敗"));
			await invalidation;
		});
		expect(await screen.findByText("無法更新總結")).toBeInTheDocument();
		expect(screen.getByRole("textbox", { name: "修訂意見" })).toBeDisabled();
		expect(screen.getByRole("button", { name: "送出修訂" })).toBeDisabled();
		expect(screen.getByRole("button", { name: "重新產生" })).toBeEnabled();
		expect(apiPost).not.toHaveBeenCalled();
	});

	it("sends the exact regenerate request and gates other tools while pending", async () => {
		const user = userEvent.setup();
		const list = {
			tools: [
				{ ...toolListResponse.tools[0], lineage: "usable" },
				{
					...toolListResponse.tools[0],
					name: "calendar-search",
					current_vid: "v-calendar-1",
					lineage: "usable",
				},
			],
		} satisfies ToolListResponse;
		const regenerate = deferred<ToolRegenerateResponse>();
		mockToolReads(list);
		mockedApiPost.mockImplementation((path, options) => {
			if (
				path === "/api/tools/{name}/summary/regenerate" &&
				options.path?.name === "weather-search"
			) {
				return regenerate.promise;
			}
			throw new Error(`Unexpected POST ${path}`);
		});

		renderWithAppProviders(<ToolsPage />);
		await waitForTool();
		for (const button of screen.getAllByRole("button", { name: "AI 總結" })) {
			await user.click(button);
		}
		expect(
			await screen.findByText("calendar-search 的工具總結"),
		).toBeInTheDocument();
		await user.click(screen.getByRole("tab", { name: "安裝新工具" }));
		await user.type(
			screen.getByRole("textbox", { name: "OpenAPI JSON 網址" }),
			"https://example.test/openapi.json",
		);
		await user.type(
			screen.getByRole("textbox", { name: "給 AI 的指示" }),
			"忙碌時不可送出的安裝",
		);
		await user.click(screen.getByRole("tab", { name: "已安裝工具" }));
		await user.click(screen.getAllByRole("button", { name: "重新產生" })[0]);
		await waitFor(() => {
			expectOnlyWriteCall(writeApiMocks, mockedApiPost, [
				"/api/tools/{name}/summary/regenerate",
				{
					path: { name: list.tools[0].name },
					body: { expected_vid: list.tools[0].current_vid },
				},
			]);
		});

		expect(
			screen.getAllByRole("button", { name: "重新產生" })[1],
		).toBeDisabled();
		expect(
			screen.getAllByRole("textbox", { name: "修訂意見" })[1],
		).toBeDisabled();
		expect(
			screen.getAllByRole("button", { name: "丟掉這一版" })[1],
		).toBeDisabled();
		await user.click(screen.getByRole("tab", { name: "安裝新工具" }));
		expect(
			screen.getByRole("textbox", { name: "OpenAPI JSON 網址" }),
		).toBeDisabled();
		expect(screen.getByRole("button", { name: "開始安裝" })).toBeDisabled();
		const installForm = screen
			.getByRole("textbox", { name: "OpenAPI JSON 網址" })
			.closest("form");
		expect(installForm).not.toBeNull();
		await act(async () => {
			fireEvent.submit(installForm as HTMLFormElement);
		});
		expect(apiPost).toHaveBeenCalledTimes(1);

		await act(async () => {
			regenerate.resolve(regenerateResponseFor(list.tools[0]));
			await regenerate.promise;
		});
	});

	it("sends the exact install request and gates every version write while pending", async () => {
		const user = userEvent.setup();
		const list = toolListWith({ lineage: "usable" });
		const install = deferred<ToolInstallAccepted>();
		mockToolReads(list);
		mockedApiPost.mockImplementation((path) => {
			if (path === "/api/tools/install") {
				return install.promise;
			}
			throw new Error(`Unexpected POST ${path}`);
		});

		renderWithAppProviders(<ToolsPage />);
		await waitForTool();
		await user.click(screen.getByRole("button", { name: "AI 總結" }));
		expect(
			await screen.findByText("weather-search 的工具總結"),
		).toBeInTheDocument();
		await user.click(screen.getByRole("tab", { name: "安裝新工具" }));
		await user.type(
			screen.getByRole("textbox", { name: "OpenAPI JSON 網址" }),
			"https://example.test/openapi.json",
		);
		await user.type(
			screen.getByRole("textbox", { name: "給 AI 的指示" }),
			"建立測試工具",
		);
		await user.click(screen.getByRole("button", { name: "開始安裝" }));
		await waitFor(() => {
			expectOnlyWriteCall(writeApiMocks, mockedApiPost, [
				"/api/tools/install",
				{
					body: {
						openapi_url: "https://example.test/openapi.json",
						instructions: "建立測試工具",
					},
				},
			]);
		});
		expect(
			screen.getByRole("textbox", { name: "OpenAPI JSON 網址" }),
		).toBeDisabled();
		expect(
			screen.getByRole("textbox", { name: "給 AI 的指示" }),
		).toBeDisabled();
		expect(screen.getByRole("button", { name: "開始安裝" })).toBeDisabled();
		const installForm = screen
			.getByRole("textbox", { name: "OpenAPI JSON 網址" })
			.closest("form");
		expect(installForm).not.toBeNull();
		await act(async () => {
			fireEvent.submit(installForm as HTMLFormElement);
		});
		expect(apiPost).toHaveBeenCalledTimes(1);
		await user.click(screen.getByRole("tab", { name: "已安裝工具" }));

		expect(screen.getByRole("button", { name: "重新產生" })).toBeDisabled();
		expect(screen.getByRole("textbox", { name: "修訂意見" })).toBeDisabled();
		expect(screen.getByRole("button", { name: "丟掉這一版" })).toBeDisabled();
		expect(apiDelete).not.toHaveBeenCalled();
	});

	it("keeps the install form closed while its job is active and being revalidated", async () => {
		const user = userEvent.setup();
		const list = toolListWith({ lineage: "usable" });
		const runningJob = {
			job_id: "job-install-1",
			state: "running",
			created_at: "2026-07-29T01:00:00Z",
			finished_at: null,
			error: null,
			tool_name: "weather-search",
			summary: null,
			llm_log_id: null,
			llm_log_process: null,
			env_keys: [],
		} satisfies ToolJobResponse;
		const terminalJob = {
			...runningJob,
			state: "succeeded",
			finished_at: "2026-07-29T01:01:00Z",
			summary: "安裝完成摘要",
		} satisfies ToolJobResponse;
		let currentJob: ToolJobResponse = runningJob;
		mockedApiGet.mockImplementation((path, options) => {
			if (path === "/api/tools") {
				return Promise.resolve(list);
			}
			if (
				path === "/api/tools/{name}/summary" &&
				options?.path?.name === "weather-search"
			) {
				return Promise.resolve(summaryFor(list.tools[0]));
			}
			if (
				path === "/api/tools/jobs/{job_id}" &&
				options?.path?.job_id === "job-install-1"
			) {
				return Promise.resolve(currentJob);
			}
			throw new Error(`Unexpected GET ${path}`);
		});
		mockedApiPost.mockResolvedValue({
			job_id: "job-install-1",
		} satisfies ToolInstallAccepted);

		const { queryClient } = renderWithAppProviders(<ToolsPage />);
		await waitForTool();
		await user.click(screen.getByRole("button", { name: "AI 總結" }));
		expect(
			await screen.findByText("weather-search 的工具總結"),
		).toBeInTheDocument();
		await user.click(screen.getByRole("tab", { name: "安裝新工具" }));
		await user.type(
			screen.getByRole("textbox", { name: "OpenAPI JSON 網址" }),
			"https://example.test/openapi.json",
		);
		await user.type(
			screen.getByRole("textbox", { name: "給 AI 的指示" }),
			"建立測試工具",
		);
		await user.click(screen.getByRole("button", { name: "開始安裝" }));

		expect(
			await screen.findByText("AI 正在安裝工具，可能需要數分鐘……"),
		).toBeInTheDocument();
		const installUrl = screen.getByRole("textbox", {
			name: "OpenAPI JSON 網址",
		});
		const installForm = installUrl.closest("form");
		expect(installUrl).toBeDisabled();
		expect(screen.getByRole("button", { name: "開始安裝" })).toBeDisabled();
		expect(installForm).not.toBeNull();
		await act(async () => {
			fireEvent.submit(installForm as HTMLFormElement);
		});
		expect(apiPost).toHaveBeenCalledTimes(1);
		await user.click(screen.getByRole("tab", { name: "已安裝工具" }));
		expect(screen.getByRole("button", { name: "重新產生" })).toBeDisabled();
		expect(screen.getByRole("textbox", { name: "修訂意見" })).toBeDisabled();
		expect(screen.getByRole("button", { name: "丟掉這一版" })).toBeDisabled();
		await user.click(screen.getByRole("tab", { name: "安裝新工具" }));

		const revalidation = deferred<void>();
		const originalInvalidate = queryClient.invalidateQueries.bind(queryClient);
		vi.spyOn(queryClient, "invalidateQueries").mockImplementation(
			(filters, options) => {
				const key = filters?.queryKey;
				if (
					Array.isArray(key) &&
					(key[0] === "tools" || key[0] === "tool-summary")
				) {
					return revalidation.promise;
				}
				return originalInvalidate(filters, options);
			},
		);
		currentJob = terminalJob;
		await act(async () => {
			await queryClient.refetchQueries({
				queryKey: ["tool-install", "job-install-1"],
				exact: true,
			});
		});

		expect(await screen.findByText("安裝完成")).toBeInTheDocument();
		expect(installUrl).toBeDisabled();
		expect(screen.getByRole("button", { name: "開始安裝" })).toBeEnabled();
		await user.click(screen.getByRole("tab", { name: "已安裝工具" }));
		expect(screen.getByRole("button", { name: "重新產生" })).toBeDisabled();
		expect(screen.getByRole("textbox", { name: "修訂意見" })).toBeDisabled();
		expect(screen.getByRole("button", { name: "丟掉這一版" })).toBeDisabled();
		await user.click(screen.getByRole("tab", { name: "安裝新工具" }));
		await act(async () => {
			fireEvent.submit(installForm as HTMLFormElement);
		});
		expect(apiPost).toHaveBeenCalledTimes(1);

		await act(async () => {
			revalidation.resolve();
			await revalidation.promise;
		});
		await waitFor(() => {
			expect(installUrl).toBeEnabled();
			expect(screen.getByRole("button", { name: "開始安裝" })).toBeEnabled();
		});
	});

	it("sends the exact revise request and keeps the write gate closed through its job", async () => {
		const user = userEvent.setup();
		const list = {
			tools: [
				{ ...toolListResponse.tools[0], lineage: "usable" },
				{
					...toolListResponse.tools[0],
					name: "calendar-search",
					current_vid: "v-calendar-1",
					lineage: "usable",
				},
			],
		} satisfies ToolListResponse;
		const revise = deferred<ToolReviseAccepted>();
		const runningJob = {
			job_id: "job-revise-1",
			state: "running",
			created_at: "2026-07-29T01:00:00Z",
			finished_at: null,
			error: null,
			tool_name: "weather-search",
			summary: null,
			llm_log_id: null,
			llm_log_process: null,
			env_keys: [],
		} satisfies ToolJobResponse;
		mockedApiGet.mockImplementation((path, options) => {
			if (path === "/api/tools") {
				return Promise.resolve(list);
			}
			const tool = list.tools.find(
				(candidate) =>
					path === "/api/tools/{name}/summary" &&
					options?.path?.name === candidate.name,
			);
			if (tool) {
				return Promise.resolve(summaryFor(tool));
			}
			if (
				path === "/api/tools/jobs/{job_id}" &&
				options?.path?.job_id === "job-revise-1"
			) {
				return Promise.resolve(runningJob);
			}
			throw new Error(`Unexpected GET ${path}`);
		});
		mockedApiPost.mockImplementation((path, options) => {
			if (
				path === "/api/tools/{name}/revise" &&
				options.path?.name === "weather-search"
			) {
				return revise.promise;
			}
			throw new Error(`Unexpected POST ${path}`);
		});

		renderWithAppProviders(<ToolsPage />);
		await waitForTool();
		for (const button of screen.getAllByRole("button", { name: "AI 總結" })) {
			await user.click(button);
		}
		await user.type(
			screen.getAllByRole("textbox", { name: "修訂意見" })[0],
			"調整第一個工具",
		);
		await user.click(screen.getAllByRole("button", { name: "送出修訂" })[0]);
		await waitFor(() => {
			expectOnlyWriteCall(writeApiMocks, mockedApiPost, [
				"/api/tools/{name}/revise",
				{
					path: { name: list.tools[0].name },
					body: {
						feedback: "調整第一個工具",
						expected_vid: list.tools[0].current_vid,
					},
				},
			]);
		});
		expect(
			screen.getAllByRole("button", { name: "重新產生" })[1],
		).toBeDisabled();

		await act(async () => {
			revise.resolve({ job_id: "job-revise-1" });
			await revise.promise;
		});
		expect(
			await screen.findByText("AI 正在修訂工具，可能需要數分鐘……"),
		).toBeInTheDocument();
		expect(
			screen.getAllByRole("button", { name: "重新產生" })[1],
		).toBeDisabled();
		expect(
			screen.getAllByRole("textbox", { name: "修訂意見" })[1],
		).toBeDisabled();
		expect(
			screen.getAllByRole("button", { name: "丟掉這一版" })[1],
		).toBeDisabled();
	});

	it("keeps version writes blocked while a finished revise is being revalidated", async () => {
		const user = userEvent.setup();
		const list = toolListWith({ lineage: "usable" });
		const terminalJob = {
			job_id: "job-revise-finished",
			state: "succeeded",
			created_at: "2026-07-29T01:00:00Z",
			finished_at: "2026-07-29T01:01:00Z",
			error: null,
			tool_name: "weather-search",
			summary: "修訂完成摘要",
			llm_log_id: null,
			llm_log_process: null,
			env_keys: [],
		} satisfies ToolJobResponse;
		mockedApiGet.mockImplementation((path, options) => {
			if (path === "/api/tools") {
				return Promise.resolve(list);
			}
			if (
				path === "/api/tools/{name}/summary" &&
				options?.path?.name === "weather-search"
			) {
				return Promise.resolve(summaryFor(list.tools[0]));
			}
			if (
				path === "/api/tools/jobs/{job_id}" &&
				options?.path?.job_id === "job-revise-finished"
			) {
				return Promise.resolve(terminalJob);
			}
			throw new Error(`Unexpected GET ${path}`);
		});
		mockedApiPost.mockResolvedValue({
			job_id: "job-revise-finished",
		} satisfies ToolReviseAccepted);

		const { queryClient } = renderWithAppProviders(<ToolsPage />);
		await waitForTool();
		await user.click(screen.getByRole("button", { name: "AI 總結" }));
		expect(
			await screen.findByText("weather-search 的工具總結"),
		).toBeInTheDocument();
		const revalidation = deferred<void>();
		vi.spyOn(queryClient, "invalidateQueries").mockImplementation(
			() => revalidation.promise,
		);
		await user.type(
			screen.getByRole("textbox", { name: "修訂意見" }),
			"送出後測試終局閘",
		);
		await user.click(screen.getByRole("button", { name: "送出修訂" }));

		expect(await screen.findByText("修訂完成")).toBeInTheDocument();
		expect(screen.getByRole("button", { name: "重新產生" })).toBeDisabled();
		expect(screen.getByRole("textbox", { name: "修訂意見" })).toBeDisabled();
		expect(screen.getByRole("button", { name: "丟掉這一版" })).toBeDisabled();

		await act(async () => {
			revalidation.resolve();
			await revalidation.promise;
		});
	});

	it("blocks an invalid toggle and sends the exact whole-tool delete request", async () => {
		const user = userEvent.setup();
		const list = toolListWith({
			valid: false,
			error: "tool.json 無效",
			lineage: "broken",
		});
		mockToolReads(list);
		mockedApiDelete.mockResolvedValue({
			outcome: "removed",
			retained_path: null,
			retention_reason: null,
		} satisfies ToolDeleteResponse);

		renderWithAppProviders(<ToolsPage />);
		await waitForTool();

		expect(screen.getByRole("switch", { name: "啟用" })).toBeDisabled();
		await user.click(screen.getByRole("switch", { name: "啟用" }));
		expect(apiPatch).not.toHaveBeenCalled();
		expect(screen.getByRole("button", { name: "刪除" })).toBeEnabled();
		await user.click(screen.getByRole("button", { name: "刪除" }));
		const dialog = await screen.findByRole("dialog", { name: "刪除工具" });
		await user.click(within(dialog).getByRole("button", { name: "刪除" }));
		expectOnlyWriteCall(writeApiMocks, mockedApiDelete, [
			"/api/tools/{name}",
			{ path: { name: list.tools[0].name } },
		]);
	});
});
