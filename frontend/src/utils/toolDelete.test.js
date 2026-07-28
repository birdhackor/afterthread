import { describe, expect, it } from "vitest";
import {
	toolDeleteNotification,
	toolDiscardNotification,
} from "./toolDelete.js";

describe("toolDeleteNotification", () => {
	it("reports physical removal without a retained-path warning", () => {
		expect(
			toolDeleteNotification("kbsearch", {
				outcome: "removed",
				retained_path: null,
			}),
		).toEqual({
			color: "green",
			message: "已刪除「kbsearch」的工具目錄",
		});
	});

	it("reports retained files and the exact operator cleanup path", () => {
		const retainedPath =
			"/srv/afterthread/tools/.kbsearch.stale-0123456789abcdef0123456789abcdef";

		expect(
			toolDeleteNotification("kbsearch", {
				outcome: "retained",
				retained_path: retainedPath,
			}),
		).toEqual({
			color: "orange",
			title: "工具檔案仍保留",
			message: `已從工具清單移除「kbsearch」，但工具目錄（含設定與金鑰檔）仍保留在：${retainedPath}。請確認相關工具程序已停止後，再手動刪除該目錄。`,
		});
	});
});

describe("toolDiscardNotification", () => {
	it("reports when the former version files were removed", () => {
		expect(
			toolDiscardNotification("kbsearch", {
				outcome: "removed",
				retained_path: null,
			}),
		).toEqual({
			color: "green",
			message: "已丟掉「kbsearch」的目前版本並退回前一版；原版本檔案已移除",
		});
	});

	it("reports retained version files and their exact cleanup path", () => {
		const retainedPath =
			"/srv/afterthread/tools/kbsearch/versions/20260728T010203Z-abcdef.discarded";

		expect(
			toolDiscardNotification("kbsearch", {
				outcome: "retained",
				retained_path: retainedPath,
			}),
		).toEqual({
			color: "orange",
			title: "已退回前一版，但原版本檔案仍保留",
			message: `「kbsearch」已退回前一版；原版本檔案仍保留在：${retainedPath}。請確認相關工具程序已停止後，再手動刪除該目錄。`,
		});
	});
});
