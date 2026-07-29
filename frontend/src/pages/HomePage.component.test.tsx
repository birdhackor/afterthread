// @vitest-environment jsdom

import { act, cleanup, screen } from "@testing-library/react";
import { getDefaultStore, type PrimitiveAtom } from "jotai";
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
import { apiFetch, apiGet } from "../api/client.js";
import { backendStatusAtom } from "../atoms/connectivity.js";
import { llmStatusAtom } from "../atoms/llm.js";
import { renderWithAppProviders } from "../test/render.js";
import { HomePage } from "./HomePage.jsx";

vi.mock("../api/client.js", () => ({
	apiFetch: vi.fn(),
	apiGet: vi.fn(),
}));

type ReviewResponse = ApiSuccessResponse<"/api/review", "get">;
type HomeLlmStatus = {
	loaded: boolean;
	loading: boolean;
	configured: boolean;
	model: string | null;
	error: string | null;
};
type BackendStatus = { reachable: boolean | null };

const mockedApiGet = vi.mocked(apiGet) as unknown as Mock<
	(path: string, options?: unknown) => Promise<unknown>
>;
const store = getDefaultStore();
const homeLlmStatusAtom = llmStatusAtom as PrimitiveAtom<HomeLlmStatus>;
const homeBackendStatusAtom = backendStatusAtom as PrimitiveAtom<BackendStatus>;
const reviewResponse = {
	needs_enrichment: [],
	active: [],
	waiting: [],
	parked: [],
} satisfies ReviewResponse;
const expectedReviewRead = ["/api/review"] as const;

beforeEach(() => {
	mockedApiGet.mockReset();
	vi.mocked(apiFetch).mockReset();
	mockedApiGet.mockImplementation((...call) => {
		if (JSON.stringify(call) !== JSON.stringify(expectedReviewRead)) {
			throw new Error(
				`Unexpected GET ${JSON.stringify(call)}; expected ${JSON.stringify(expectedReviewRead)}`,
			);
		}
		return Promise.resolve(reviewResponse);
	});
	store.set(homeBackendStatusAtom, { reachable: false });
	store.set(homeLlmStatusAtom, {
		loaded: true,
		loading: false,
		configured: false,
		model: null,
		error: null,
	});
});

afterEach(() => {
	cleanup();
	// React Query turns a thrown queryFn error into query state. Re-scan the raw
	// call log so an unexpected read cannot be swallowed into a green UI test.
	expect(mockedApiGet.mock.calls).toEqual([expectedReviewRead]);
	expect(apiFetch).not.toHaveBeenCalled();
	store.set(homeBackendStatusAtom, { reachable: null });
	store.set(homeLlmStatusAtom, {
		loaded: false,
		loading: false,
		configured: true,
		model: null,
		error: null,
	});
});

describe("HomePage operator guidance", () => {
	it("uses 重試 for failures and 重新檢查 only for a plain unconfigured result", async () => {
		renderWithAppProviders(<HomePage />);

		expect(
			await screen.findByRole("button", { name: "重試" }),
		).toBeInTheDocument();
		expect(
			screen.queryByRole("button", { name: "重新檢查" }),
		).not.toBeInTheDocument();

		act(() => {
			store.set(homeBackendStatusAtom, { reachable: true });
			store.set(homeLlmStatusAtom, {
				loaded: true,
				loading: false,
				configured: false,
				model: null,
				error: "狀態探測失敗",
			});
		});
		expect(screen.getByRole("button", { name: "重試" })).toBeInTheDocument();

		act(() => {
			store.set(homeLlmStatusAtom, {
				loaded: true,
				loading: false,
				configured: false,
				model: null,
				error: null,
			});
		});
		expect(
			screen.getByRole("button", { name: "重新檢查" }),
		).toBeInTheDocument();
		expect(
			screen.queryByRole("button", { name: "重試" }),
		).not.toBeInTheDocument();
	});
});
