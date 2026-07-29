// @vitest-environment jsdom

import { cleanup, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { getDefaultStore } from "jotai";
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
import { DEFAULT_LIMIT, pageAtom, resetFiltersAtom } from "../atoms/filters.js";
import { renderWithAppProviders } from "../test/render.js";
import { ItemsListPage } from "./ItemsListPage.jsx";

vi.mock("../api/client.js", () => ({
	apiGet: vi.fn(),
}));

type ItemListResponse = ApiSuccessResponse<"/api/items", "get">;
type Item = ItemListResponse["items"][number];
type ReadStep = {
	call: readonly unknown[];
	response: ItemListResponse;
};

const mockedApiGet = vi.mocked(apiGet) as unknown as Mock<
	(path: string, options?: unknown) => Promise<unknown>
>;
const store = getDefaultStore();
let expectedReadCalls: readonly (readonly unknown[])[] = [];

function item(id: number, title: string): Item {
	return {
		alternatives: "",
		assumptions: "",
		confidence: "mixed",
		consequences: "",
		constraints: "",
		created: "2026-07-29T08:00:00Z",
		decisions: "",
		evidence: "",
		id,
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
		snapshot: `${title} 的快照`,
		source: "manual",
		stage: "full",
		status: "active",
		tags: ["架構"],
		title,
		unknown: "",
		updated: "2026-07-29T09:00:00Z",
		why_matters: "",
	};
}

function itemRead(
	overrides: Partial<{
		status: string;
		stage: string;
		tag: string;
		q: string;
		limit: number;
		offset: number;
	}> = {},
) {
	return [
		"/api/items",
		{
			query: {
				status: "",
				stage: "",
				tag: "",
				q: "",
				limit: DEFAULT_LIMIT,
				offset: 0,
				...overrides,
			},
		},
	] as const;
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

function findDisplayedText(text: string) {
	// Mantine + React Query transitions can exceed Testing Library's 1 s
	// default while all jsdom files run in parallel; the request remains strict.
	return screen.findByText(text, {}, { timeout: 3_000 });
}

beforeEach(() => {
	expectedReadCalls = [];
	mockedApiGet.mockReset();
	store.set(resetFiltersAtom);
});

afterEach(() => {
	cleanup();
	// A strict implementation throw becomes React Query error state. The raw
	// sequence assertion is what still exposes a wrong tuple or additive read.
	expect(mockedApiGet.mock.calls).toEqual(expectedReadCalls);
	store.set(resetFiltersAtom);
});

describe("ItemsListPage display correctness", () => {
	// Two Mantine Select interactions plus two real 300 ms debounce windows can
	// approach Vitest's 5 s default when jsdom files run in parallel.
	it("sends every selected filter and the debounced final keystroke", async () => {
		const user = userEvent.setup();
		const tag = "架構";
		const query = "最後輸入Z";
		mockReadSequence(
			{
				call: itemRead(),
				response: { items: [item(1, "全部項目")], total: 1 },
			},
			{
				call: itemRead({ status: "active" }),
				response: { items: [item(2, "狀態篩選結果")], total: 1 },
			},
			{
				call: itemRead({ status: "active", stage: "full" }),
				response: { items: [item(3, "階段篩選結果")], total: 1 },
			},
			{
				call: itemRead({ status: "active", stage: "full", tag }),
				response: { items: [item(4, "標籤篩選結果")], total: 1 },
			},
			{
				call: itemRead({ status: "active", stage: "full", tag, q: query }),
				response: { items: [item(5, "完整篩選結果")], total: 1 },
			},
		);
		renderWithAppProviders(<ItemsListPage />, {
			initialEntries: ["/items"],
		});

		expect(await findDisplayedText("全部項目")).toBeInTheDocument();
		await user.click(screen.getByRole("combobox", { name: "狀態" }));
		await user.keyboard("{ArrowDown}{ArrowDown}{ArrowDown}{Enter}");
		expect(await findDisplayedText("狀態篩選結果")).toBeInTheDocument();

		await user.click(screen.getByRole("combobox", { name: "階段" }));
		await user.keyboard("{ArrowDown}{ArrowDown}{Enter}");
		expect(await findDisplayedText("階段篩選結果")).toBeInTheDocument();

		await user.type(screen.getByRole("textbox", { name: "標籤" }), tag);
		expect(await findDisplayedText("標籤篩選結果")).toBeInTheDocument();

		await user.type(screen.getByRole("textbox", { name: "搜尋" }), query);
		expect(await findDisplayedText("完整篩選結果")).toBeInTheDocument();
		expect(mockedApiGet).toHaveBeenCalledTimes(5);
	}, 10_000);

	it("uses the selected page offset and renders the response's exact total", async () => {
		const user = userEvent.setup();
		mockReadSequence(
			{
				call: itemRead(),
				response: { items: [item(1, "第一頁資料")], total: 41 },
			},
			{
				call: itemRead({ offset: DEFAULT_LIMIT }),
				response: { items: [item(21, "第二頁資料")], total: 41 },
			},
		);
		renderWithAppProviders(<ItemsListPage />, {
			initialEntries: ["/items"],
		});

		expect(await findDisplayedText("第一頁資料")).toBeInTheDocument();
		expect(screen.getByText("共 41 筆")).toBeInTheDocument();
		expect(screen.queryByText("尚無記憶項目")).not.toBeInTheDocument();

		await user.click(screen.getByRole("button", { name: "2" }));

		expect(await findDisplayedText("第二頁資料")).toBeInTheDocument();
		expect(screen.queryByText("第一頁資料")).not.toBeInTheDocument();
		expect(screen.getByText("共 41 筆")).toBeInTheDocument();
		expect(screen.getByRole("button", { name: "2" })).toHaveAttribute(
			"data-active",
			"true",
		);
	});

	it("distinguishes unfiltered emptiness from a filtered no-match result", async () => {
		const user = userEvent.setup();
		const query = "不存在Z";
		mockReadSequence(
			{ call: itemRead(), response: { items: [], total: 0 } },
			{
				call: itemRead({ q: query }),
				response: { items: [], total: 0 },
			},
			{ call: itemRead(), response: { items: [], total: 0 } },
		);
		renderWithAppProviders(<ItemsListPage />, {
			initialEntries: ["/items"],
		});

		expect(await findDisplayedText("尚無記憶項目")).toBeInTheDocument();
		expect(screen.queryByText("找不到符合條件的項目")).not.toBeInTheDocument();

		const search = screen.getByRole("textbox", { name: "搜尋" });
		await user.type(search, query);
		expect(await findDisplayedText("找不到符合條件的項目")).toBeInTheDocument();
		expect(screen.queryByText("尚無記憶項目")).not.toBeInTheDocument();

		await user.clear(search);
		expect(await findDisplayedText("尚無記憶項目")).toBeInTheDocument();
		expect(mockedApiGet).toHaveBeenCalledTimes(3);
	});

	it("clamps an emptied last page and displays the new last page", async () => {
		store.set(pageAtom, 3);
		mockReadSequence(
			{
				call: itemRead({ offset: DEFAULT_LIMIT * 2 }),
				response: { items: [], total: 21 },
			},
			{
				call: itemRead({ offset: DEFAULT_LIMIT }),
				response: { items: [item(21, "校正後最後一頁")], total: 21 },
			},
		);
		renderWithAppProviders(<ItemsListPage />, {
			initialEntries: ["/items"],
		});

		expect(await findDisplayedText("校正後最後一頁")).toBeInTheDocument();
		expect(screen.getByText("共 21 筆")).toBeInTheDocument();
		expect(screen.getByRole("button", { name: "2" })).toHaveAttribute(
			"data-active",
			"true",
		);
		await waitFor(() => {
			expect(mockedApiGet).toHaveBeenCalledTimes(2);
		});
	});
});
