// @vitest-environment jsdom

import { act, cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {
	afterEach,
	beforeEach,
	describe,
	expect,
	it,
	type Mock,
	vi,
} from "vitest";
import type { ApiSuccessResponse } from "../api/client.js";
import { apiGet } from "../api/client.js";
import { renderWithAppProviders } from "../test/render.js";
import { LlmLogsPage } from "./LlmLogsPage.jsx";

vi.mock("../api/client.js", () => ({
	apiGet: vi.fn(),
}));

type LogListResponse = ApiSuccessResponse<"/api/llm/logs", "get">;
type LogSummary = LogListResponse["logs"][number];
type LogDetailResponse = ApiSuccessResponse<"/api/llm/logs/{log_id}", "get">;
type ReadStep = {
	call: readonly unknown[];
	response: LogListResponse | LogDetailResponse;
};

const mockedApiGet = vi.mocked(apiGet) as unknown as Mock<
	(path: string, options?: unknown) => Promise<unknown>
>;
const listRead = ["/api/llm/logs", { query: { limit: 50 } }] as const;
let expectedReadCalls: readonly (readonly unknown[])[] = [];

function logSummary(id: number, workflow: string, outcome: string): LogSummary {
	return {
		attempts: 1,
		duration_ms: 275,
		error: null,
		finished_at: `2026-07-29T08:00:${String(id).padStart(2, "0")}Z`,
		id,
		model: "fixture-model",
		outcome,
		started_at: `2026-07-29T08:00:${String(id).padStart(2, "0")}Z`,
		usage: {
			prompt_tokens: 10,
			completion_tokens: 5,
			total_tokens: 15,
		},
		workflow,
	};
}

function detailRead(id: number) {
	return ["/api/llm/logs/{log_id}", { path: { log_id: id } }] as const;
}

function mockReadSequence(...steps: ReadStep[]) {
	expectedReadCalls = steps.map((step) => step.call);
	mockedApiGet.mockImplementation((...call) => {
		const index = mockedApiGet.mock.calls.length - 1;
		const step = steps[index];
		if (
			step === undefined ||
			JSON.stringify(call) !== JSON.stringify(step.call)
		) {
			throw new Error(
				`Unexpected GET #${index + 1} ${JSON.stringify(call)}; expected ${JSON.stringify(step?.call ?? "no additional read")}`,
			);
		}
		return Promise.resolve(step.response);
	});
}

beforeEach(() => {
	expectedReadCalls = [];
	mockedApiGet.mockReset();
});

afterEach(() => {
	cleanup();
	// Query errors are renderable state on this page. Re-reading mock.calls is
	// required to make an unexpected/swallowed detail request fail the case.
	expect(mockedApiGet.mock.calls).toEqual(expectedReadCalls);
});

describe("LlmLogsPage display correctness", () => {
	it("renders every known outcome with its zh-TW label and Mantine colour", async () => {
		const cases = [
			{
				summary: logSummary(1, "capture", "ok"),
				workflow: "快速捕捉",
				label: "成功",
				color: "green",
			},
			{
				summary: logSummary(2, "enrich", "timeout"),
				workflow: "AI 補齊",
				label: "逾時",
				color: "red",
			},
			{
				summary: logSummary(3, "assist_update", "upstream_error"),
				workflow: "AI 進度更新",
				label: "上游失敗",
				color: "red",
			},
			{
				summary: logSummary(4, "tool_install", "invalid_output"),
				workflow: "工具建置",
				label: "輸出無效",
				color: "orange",
			},
			{
				summary: logSummary(5, "tool_summary", "not_configured"),
				workflow: "工具總結",
				label: "未設定",
				color: "gray",
			},
		];
		mockReadSequence({
			call: listRead,
			response: {
				logs: cases.map((testCase) => testCase.summary),
				process_token: "process-outcomes",
			},
		});
		renderWithAppProviders(<LlmLogsPage />, {
			initialEntries: ["/llm-logs"],
		});

		expect(await screen.findByText("快速捕捉")).toBeInTheDocument();
		for (const testCase of cases) {
			const control = screen.getByRole("button", {
				name: new RegExp(testCase.workflow),
			});
			const badge = within(control)
				.getByText(testCase.label)
				.closest(".mantine-Badge-root");
			expect(badge).toHaveStyle(
				`--badge-bg: var(--mantine-color-${testCase.color}-light)`,
			);
			expect(badge).toHaveStyle(
				`--badge-color: var(--mantine-color-${testCase.color}-light-color)`,
			);
		}
	});

	it("opens the selected record and keeps errors, status, and tools on their own attempts", async () => {
		const user = userEvent.setup();
		const other = logSummary(17, "capture", "ok");
		const selected = {
			...logSummary(29, "tool_summary", "invalid_output"),
			attempts: 2,
		};
		const detail = {
			...selected,
			attempts: [
				{
					error: "第一輪限流",
					request_chars: 28,
					request_messages: [{ role: "user", content: "紀錄 29 第一輪要求" }],
					response_chars: null,
					response_content: null,
					tools_advertised: ["weather_search", "calendar_lookup"],
					truncated: false,
					usage: null,
				},
				{
					error: null,
					request_chars: 31,
					request_messages: [{ role: "user", content: "紀錄 29 第二輪要求" }],
					response_chars: 18,
					response_content: "紀錄 29 第二輪回應",
					tools_advertised: ["finalize_tool"],
					truncated: true,
					usage: {
						prompt_tokens: 21,
						completion_tokens: 8,
						total_tokens: 29,
					},
				},
			],
		} satisfies LogDetailResponse;
		mockReadSequence(
			{
				call: listRead,
				response: {
					logs: [other, selected],
					process_token: "process-detail",
				},
			},
			{ call: detailRead(selected.id), response: detail },
		);
		renderWithAppProviders(<LlmLogsPage />, {
			initialEntries: ["/llm-logs"],
		});

		await user.click(await screen.findByRole("button", { name: /工具總結/ }));

		const attempt1 = (await screen.findByText("嘗試 1")).closest(
			".mantine-Card-root",
		);
		const attempt2 = screen.getByText("嘗試 2").closest(".mantine-Card-root");
		expect(attempt1).not.toBeNull();
		expect(attempt2).not.toBeNull();
		expect(
			within(attempt1 as HTMLElement).getByText("第一輪限流"),
		).toBeVisible();
		expect(
			within(attempt1 as HTMLElement).queryByText("已截斷"),
		).not.toBeInTheDocument();
		expect(
			within(attempt1 as HTMLElement).getByText("weather_search"),
		).toBeVisible();
		expect(
			within(attempt1 as HTMLElement).getByText("calendar_lookup"),
		).toBeVisible();
		expect(
			within(attempt1 as HTMLElement).queryByText("finalize_tool"),
		).not.toBeInTheDocument();
		expect(within(attempt2 as HTMLElement).getByText("已截斷")).toBeVisible();
		expect(
			within(attempt2 as HTMLElement).queryByText("第一輪限流"),
		).not.toBeInTheDocument();
		expect(
			within(attempt2 as HTMLElement).getByText("finalize_tool"),
		).toBeVisible();
		expect(
			within(attempt2 as HTMLElement).getByText("紀錄 29 第二輪回應"),
		).toBeVisible();
		expect(screen.queryByText("紀錄 17 第一輪要求")).not.toBeInTheDocument();
		expect(mockedApiGet).toHaveBeenCalledTimes(2);
	});

	it("shows the retained-log warning only while a background refetch is failing", async () => {
		const row = logSummary(41, "capture", "ok");
		const message = "AI 日誌背景更新失敗";
		expectedReadCalls = [listRead, listRead, listRead];
		mockedApiGet
			.mockResolvedValueOnce({
				logs: [row],
				process_token: "process-stale-list",
			} satisfies LogListResponse)
			.mockRejectedValueOnce(new Error(message))
			.mockResolvedValueOnce({
				logs: [row],
				process_token: "process-stale-list",
			} satisfies LogListResponse);
		const { queryClient } = renderWithAppProviders(<LlmLogsPage />, {
			initialEntries: ["/llm-logs"],
		});

		expect(await screen.findByText("快速捕捉")).toBeInTheDocument();
		expect(screen.queryByText("無法更新 AI 日誌")).not.toBeInTheDocument();
		await act(async () => {
			await queryClient.refetchQueries({
				queryKey: ["llm-logs"],
				exact: true,
			});
		});

		expect(await screen.findByText("無法更新 AI 日誌")).toBeInTheDocument();
		expect(screen.getByText(new RegExp(message))).toHaveTextContent(
			"以下內容是先前讀到的結果，可能已過期",
		);
		expect(screen.getByText("快速捕捉")).toBeInTheDocument();

		await act(async () => {
			await queryClient.refetchQueries({
				queryKey: ["llm-logs"],
				exact: true,
			});
		});
		await waitFor(() => {
			expect(screen.queryByText("無法更新 AI 日誌")).not.toBeInTheDocument();
		});
	});
});
