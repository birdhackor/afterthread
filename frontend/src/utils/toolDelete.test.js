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
				retention_reason: "cleanup_failed",
			}),
		).toEqual({
			color: "orange",
			title: "工具檔案仍保留",
			message: `已從工具清單移除「kbsearch」，但工具目錄（含設定與金鑰檔）因清理失敗仍保留在：${retainedPath}。請檢查權限與檔案系統狀態後，再手動刪除該目錄。`,
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
				retention_reason: "cleanup_failed",
			}),
		).toEqual({
			color: "orange",
			title: "已退回前一版，但原版本檔案仍保留",
			message: `「kbsearch」已退回前一版；原版本檔案因清理失敗仍保留在：${retainedPath}。請檢查權限與檔案系統狀態後，再手動刪除該目錄。`,
		});
	});

	it("forbids manual cleanup while current-pointer durability is unconfirmed", () => {
		const retainedPath =
			"/srv/afterthread/tools/kbsearch/versions/20260728T010203Z-abcdef";

		const notice = toolDiscardNotification("kbsearch", {
			outcome: "retained",
			retained_path: retainedPath,
			retention_reason: "durability_unconfirmed",
		});

		expect(notice).toEqual({
			color: "orange",
			title: "已退回前一版，但持久化尚未確認",
			message: `「kbsearch」目前已退回前一版，但無法確認版本指標已持久化。請勿手動刪除 ${retainedPath}；請先處理檔案系統或儲存裝置問題，重新啟動後確認工具仍指向前一版，再決定是否清理。`,
		});
		expect(notice.message).toContain("請勿手動刪除");
	});
});
