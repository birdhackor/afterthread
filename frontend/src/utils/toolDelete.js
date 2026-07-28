// Translate the whole-tool DELETE wire result into one operator-facing notice.
// The retained branch deliberately includes the backend's exact parked path:
// removing a row from the registry is not the same as removing config or key
// files from disk, and the operator needs that path to finish cleanup safely.
export function toolDeleteNotification(name, result) {
	if (result?.outcome === "removed") {
		return {
			color: "green",
			message: `已刪除「${name}」的工具目錄`,
		};
	}
	if (
		result?.outcome === "retained" &&
		typeof result.retained_path === "string" &&
		result.retained_path !== ""
	) {
		return {
			color: "orange",
			title: "工具檔案仍保留",
			message: `已從工具清單移除「${name}」，但工具目錄（含設定與金鑰檔）仍保留在：${result.retained_path}。請確認相關工具程序已停止後，再手動刪除該目錄。`,
		};
	}
	return {
		color: "red",
		title: "無法確認刪除結果",
		message: `「${name}」已從工具清單移除，但後端沒有回報檔案是否仍保留；請檢查工具目錄。`,
	};
}

// Discard has the same removal/retention contract as whole-tool deletion, but
// current already points back to P in both branches. Say that first, then make
// the exact retained V path actionable instead of presenting generic success.
export function toolDiscardNotification(name, result) {
	if (result?.outcome === "removed") {
		return {
			color: "green",
			message: `已丟掉「${name}」的目前版本並退回前一版；原版本檔案已移除`,
		};
	}
	if (
		result?.outcome === "retained" &&
		typeof result.retained_path === "string" &&
		result.retained_path !== ""
	) {
		return {
			color: "orange",
			title: "已退回前一版，但原版本檔案仍保留",
			message: `「${name}」已退回前一版；原版本檔案仍保留在：${result.retained_path}。請確認相關工具程序已停止後，再手動刪除該目錄。`,
		};
	}
	return {
		color: "red",
		title: "無法確認原版本檔案狀態",
		message: `「${name}」已退回前一版，但後端沒有回報原版本檔案是否仍保留；請檢查工具目錄。`,
	};
}
