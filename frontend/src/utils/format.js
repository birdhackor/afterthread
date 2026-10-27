// Shared display formatters for timestamps and durations: the AI 日誌 rows and
// the 工具 page's 最近執行 both show when something ran and how long it took.

// Local date+time formatter: DateText renders only YYYY-MM-DD, but rows within
// one day need the time to be distinguishable. The value is an ISO UTC string;
// this renders it in the viewer's local zone. Returns "—" for a missing/invalid
// value so a malformed record never blanks the cell.
export function formatTimestamp(value) {
	if (!value) {
		return "—";
	}
	const date = new Date(value);
	if (Number.isNaN(date.getTime())) {
		return "—";
	}
	const pad = (part) => String(part).padStart(2, "0");
	const ymd = `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
	const hms = `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
	return `${ymd} ${hms}`;
}

// Milliseconds -> a compact human string; seconds once past 1s so a long
// generation reads as "12.3 s" rather than "12345 ms".
export function formatDuration(ms) {
	if (typeof ms !== "number") {
		return "—";
	}
	return ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${ms} ms`;
}
