// biome-ignore-all lint/suspicious/noTemplateCurlyInString: the examples below are source text for the scanner to read, not templates.
import { readdirSync, readFileSync } from "node:fs";
import { join, relative, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

// The directory the guard reads: src/.
const SRC = fileURLToPath(new URL("..", import.meta.url));
// What it does not read: the module every such path is written in, and this
// file, whose examples are what the scanner must find.
const LEFT_OUT = ["api/toolPaths.js", "api/toolPathsGuard.test.js"];

// A template literal holding "/api/tools/" and a ${...} value on one line,
// whichever comes first.
const TEMPLATE_WITH_A_VALUE = [
	/`[^`\n]*\/api\/tools\/[^`\n]*\$\{/g,
	/`[^`\n]*\$\{[^`\n]*\/api\/tools\//g,
];
// A string or template literal holding "/api/tools/" with a + after or before it.
const LITERAL_BESIDE_A_PLUS = [
	/(["'`])[^"'`\n]*\/api\/tools\/[^"'`\n]*\1\s*\+/g,
	/\+\s*(["'`])[^"'`\n]*\/api\/tools\//g,
];

// The lines of `source`, in order, where an /api/tools/ path is built by
// splicing a value into a literal of one of the shapes above.
function handBuiltToolPaths(source) {
	const lines = new Set();
	for (const pattern of [...TEMPLATE_WITH_A_VALUE, ...LITERAL_BESIDE_A_PLUS]) {
		for (const match of source.matchAll(pattern)) {
			lines.add(source.slice(0, match.index).split("\n").length);
		}
	}
	return [...lines].sort((a, b) => a - b);
}

function sourceFiles(directory) {
	return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
		const path = join(directory, entry.name);
		if (entry.isDirectory()) {
			return sourceFiles(path);
		}
		return /\.[cm]?jsx?$/.test(entry.name) ? [path] : [];
	});
}

function fromSrc(path) {
	return relative(SRC, path).split(sep).join("/");
}

// Each shape the scanner must find: the ones this app's code was written in,
// and the ones a + builds.
const MUST_FIND = [
	[
		"a value inside a template literal",
		"apiGet(`/api/tools/${name}/settings`)",
	],
	[
		"a value at the end of a template literal",
		"apiDelete(`/api/tools/${name}`)",
	],
	[
		"a value read off an object",
		"apiGet(`/api/tools/jobs/${activeJob.jobId}`)",
	],
	["two values", "path: `/api/tools/${name}/versions/${currentVid}`,"],
	[
		"a query string from an expression",
		"path: `/api/tools/${name}/draft?${new URLSearchParams(query)}`",
	],
	[
		"a query string with a constant",
		"apiGet(`/api/tools/${name}/runs?limit=${TOOL_RUNS_LIMIT}`)",
	],
	[
		"a value in the query string alone",
		"path: `/api/tools/kb/draft?draft_id=${DRAFT_ID}&generation=3`,",
	],
	["a value before the prefix", "fetch(`${origin}/api/tools/kb`)"],
	["a + after a double-quoted literal", 'apiGet("/api/tools/" + name)'],
	[
		"a + on each side of a single-quoted literal",
		"apiGet('/api/tools/' + name + '/revise')",
	],
	[
		"a + after a template literal with no value",
		"apiGet(`/api/tools/` + name)",
	],
	[
		"a + ending the line, the value on the next",
		'const path =\n\t"/api/tools/" +\n\tname;',
	],
	[
		"a + ending the line, the literal on the next",
		'const path = origin +\n\t"/api/tools/kb";',
	],
];

// Shapes it must pass: the paths with no value, and the helper.
const MUST_PASS = [
	["the list route", 'get("/api/tools")'],
	["a route with no value", 'apiPost("/api/tools/install", payload)'],
	["the helper", 'apiGet(toolApiPath(name, "settings"))'],
	[
		"the helper with a query string",
		'apiGet(`${toolApiPath(name, "runs")}?limit=${TOOL_RUNS_LIMIT}`)',
	],
	["a comment naming a route", "// PATCH /api/tools/{name} toggles it"],
];

describe("handBuiltToolPaths", () => {
	it.each(MUST_FIND)("finds %s", (_shape, source) => {
		expect(handBuiltToolPaths(source)).not.toEqual([]);
	});

	it.each(MUST_PASS)("passes %s", (_shape, source) => {
		expect(handBuiltToolPaths(source)).toEqual([]);
	});
});

// 驗證：src/ 底下每個 .js／.jsx（api/toolPaths.js 與這個檔案除外）沒有同一行裡含 `/api/tools/` 又含 `${…}` 的 template literal，也沒有緊鄰 `+` 的、含 `/api/tools/` 的字串或 template literal。
// 不涵蓋：`/api/tools/` 與 `${` 分在兩行的 template literal、前綴拆成幾段寫或先放進變數、常數再組的路徑、`.concat`／陣列 `join`／`new URL` 等其他組法，也不判斷插進去的值需不需要編碼。
describe("src/ has no template literal holding /api/tools/ and ${…} on one line, and no literal holding /api/tools/ beside a +, outside api/toolPaths.js and this file", () => {
	it("finds none in any file it reads", () => {
		const everyFile = sourceFiles(SRC).map(fromSrc);
		expect(everyFile).toEqual(expect.arrayContaining(LEFT_OUT));
		const read = everyFile.filter((path) => !LEFT_OUT.includes(path));
		expect(read).toEqual(
			expect.arrayContaining([
				"components/ToolRunsSection.jsx",
				"components/ToolSettingsSection.jsx",
				"pages/ToolsPage.jsx",
				"utils/toolDraft.js",
				"utils/toolDraft.test.js",
				"utils/toolSummary.js",
			]),
		);
		const found = read.flatMap((path) =>
			handBuiltToolPaths(readFileSync(join(SRC, path), "utf8")).map(
				(line) => `${path}:${line}`,
			),
		);
		expect(found).toEqual([]);
	});
});
