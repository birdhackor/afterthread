// @vitest-environment jsdom

import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { renderWithAppProviders } from "../test/render.js";
import { StatusBadge } from "./StatusBadge.jsx";

describe("StatusBadge component smoke", () => {
	it("renders the Mantine badge label", async () => {
		renderWithAppProviders(<StatusBadge status="active" />);

		expect(await screen.findByText("進行中")).toBeInTheDocument();
	});
});
