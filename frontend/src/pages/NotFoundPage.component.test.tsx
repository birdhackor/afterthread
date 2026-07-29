// @vitest-environment jsdom

import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { renderWithAppProviders } from "../test/render.js";
import { NotFoundPage } from "./NotFoundPage.jsx";

describe("NotFoundPage fallback", () => {
	it("explains the missing route and links back to the overview", async () => {
		renderWithAppProviders(<NotFoundPage />, {
			initialEntries: ["/missing"],
		});

		expect(
			await screen.findByRole("heading", { name: "找不到頁面" }),
		).toBeInTheDocument();
		expect(
			screen.getByText("您要找的頁面不存在，可能連結有誤或已被移除。"),
		).toBeInTheDocument();
		expect(screen.getByRole("link", { name: "返回總覽" })).toHaveAttribute(
			"href",
			"/",
		);
	});
});
