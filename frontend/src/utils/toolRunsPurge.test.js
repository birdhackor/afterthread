import { describe, expect, it } from "vitest";
import {
	purgePreviewText,
	purgeRequestBody,
	purgeResultMessage,
	purgeScopeLines,
	statsSummaryText,
	TOOL_PURGE_PRESETS,
} from "./toolRunsPurge.js";

const NOW = new Date("2026-10-27T00:00:00Z");

describe("purgeRequestBody", () => {
	it("sends only the conditions a scope sets", () => {
		expect(purgeRequestBody({}, NOW)).toEqual({});
		expect(purgeRequestBody({ tool: "kb", outcome: "all" }, NOW)).toEqual({
			tool: "kb",
		});
		expect(
			purgeRequestBody({ outcome: "not_ok", orphansOnly: true }, NOW),
		).toEqual({
			outcome: "not_ok",
			orphans_only: true,
		});
	});

	it("turns an age into one absolute instant, so preview and confirm agree", () => {
		expect(purgeRequestBody({ olderThanDays: 30 }, NOW)).toEqual({
			before: "2026-09-27T00:00:00.000Z",
		});
	});

	it("maps every per-tool preset onto a body for that tool", () => {
		const bodies = TOOL_PURGE_PRESETS.map((preset) =>
			purgeRequestBody({ tool: "kb", ...preset.scope }, NOW),
		);
		expect(bodies.every((body) => body.tool === "kb")).toBe(true);
		expect(bodies[1].outcome).toBe("ok");
		expect(bodies[2].before).toBeDefined();
	});
});

describe("purge wording", () => {
	it("lists what a scope covers", () => {
		expect(purgeScopeLines({ tool: "kb" })).toEqual([
			{ label: "工具", value: "「kb」" },
			{ label: "結果", value: "全部" },
			{ label: "時間", value: "不限" },
		]);
		const lines = purgeScopeLines({
			outcome: "not_ok",
			olderThanDays: 30,
			orphansOnly: true,
		});
		expect(lines.map((line) => line.value)).toEqual([
			"所有工具",
			"只清失敗與被拒的",
			"30 天前的",
			"已刪除的工具留下的紀錄",
		]);
	});

	it("says how many the preview found, or that nothing matched", () => {
		expect(purgePreviewText(3)).toBe("將刪除 3 筆紀錄，刪除後無法復原。");
		expect(purgePreviewText(0)).toBe("沒有符合條件的紀錄。");
		expect(purgeResultMessage(2)).toBe("已刪除 2 筆執行紀錄");
	});
});

describe("statsSummaryText", () => {
	it("is a plain note when nothing is stored", () => {
		expect(statsSummaryText({ total: 0, not_ok: 0, orphans: 0 })).toBe(
			"還沒有工具執行紀錄",
		);
	});

	it("mentions orphans only when there are some", () => {
		expect(statsSummaryText({ total: 12, not_ok: 3, orphans: 0 })).toBe(
			"工具執行紀錄 12 筆（失敗與被拒 3 筆）",
		);
		expect(statsSummaryText({ total: 12, not_ok: 3, orphans: 2 })).toContain(
			"已刪除的工具留下 2 筆",
		);
	});
});
