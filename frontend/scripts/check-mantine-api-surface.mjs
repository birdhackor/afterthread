// Offline guard for browser-API references in Mantine's installed ESM.
//
// WHAT THIS DOES AND DOES NOT GUARANTEE -- read before trusting it.
//
// It monitors a FIXED INVENTORY of the six API names below and reports which
// installed Mantine modules mention each. That is all. Specifically:
//
//   * A SEVENTH browser API -- one not in MONITORED_APIS -- is invisible to it.
//     Mantine could start calling `visualViewport` tomorrow and this stays
//     silent. The defence against that is not here: an unshimmed API throws a
//     loud TypeError in the component tests, which is self-detecting and needs
//     no maintenance. This check exists for the quieter case where a name we
//     ALREADY care about spreads to new modules.
//   * It records which files mention an API, not how many times. A second use
//     site inside a module already listed does not move the snapshot.
//   * It is a substring match, so an API named in a comment or a local variable
//     counts as a mention. That errs toward telling a human to look, which is
//     the safe direction for a prompt.
//
// And the standing limit: it detects CHANGE, not correctness. It cannot tell
// whether an existing test shim has become semantically wrong, and "referenced
// by Mantine" never implies "needs a shim". A diff is a prompt to inspect the
// affected component and a real failing test, never an instruction to add one.

import { readdir, readFile, stat, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
const MANTINE_SCOPE_DIR = path.resolve(SCRIPT_DIR, "../node_modules/@mantine");
const SNAPSHOT_PATH = path.join(
	SCRIPT_DIR,
	"mantine-api-surface.snapshot.json",
);
const MONITORED_APIS = [
	"matchMedia",
	"ResizeObserver",
	"IntersectionObserver",
	"document.fonts",
	"scrollIntoView",
	"getComputedStyle",
];

async function esmFiles(directory) {
	const entries = await readdir(directory, { withFileTypes: true });
	const files = [];

	for (const entry of entries) {
		const entryPath = path.join(directory, entry.name);
		if (entry.isDirectory()) {
			files.push(...(await esmFiles(entryPath)));
		} else if (entry.name.endsWith(".mjs")) {
			// Executable modules ONLY -- deliberately not the `.mjs.map` beside each
			// one. A source map carries the original text, so scanning both counted
			// every module twice (IntersectionObserver read as 6 modules when it is
			// 3) and, worse, would fire on changes that cannot alter behaviour: a
			// renamed local or an edited comment moves the map while the shipped
			// module is identical, and an API named only in a comment would register
			// as a reference at all. A check that cries wolf gets ignored, and an
			// ignored check is the same as no check.
			files.push(entryPath);
		}
	}

	return files;
}

async function scanInstalledMantine() {
	let packages;
	try {
		packages = await readdir(MANTINE_SCOPE_DIR);
	} catch (error) {
		if (error?.code === "ENOENT") {
			throw new Error(
				`Mantine is not installed at ${MANTINE_SCOPE_DIR}; run pnpm install first`,
			);
		}
		throw error;
	}

	const surface = Object.fromEntries(MONITORED_APIS.map((api) => [api, []]));

	for (const packageName of packages.sort()) {
		const esmDirectory = path.join(MANTINE_SCOPE_DIR, packageName, "esm");
		try {
			if (!(await stat(esmDirectory)).isDirectory()) {
				continue;
			}
		} catch (error) {
			if (error?.code === "ENOENT") {
				continue;
			}
			throw error;
		}

		for (const filePath of await esmFiles(esmDirectory)) {
			const source = await readFile(filePath, "utf8");
			const relativeFile = path
				.relative(esmDirectory, filePath)
				.split(path.sep)
				.join("/");
			const snapshotPath = `@mantine/${packageName}/esm/${relativeFile}`;

			for (const api of MONITORED_APIS) {
				if (source.includes(api)) {
					surface[api].push(snapshotPath);
				}
			}
		}
	}

	for (const files of Object.values(surface)) {
		files.sort();
	}
	return surface;
}

function difference(left, right) {
	const rightSet = new Set(right);
	return left.filter((item) => !rightSet.has(item));
}

function surfaceDiff(expected, actual) {
	const apiNames = [
		...MONITORED_APIS,
		...Object.keys(expected).filter((api) => !MONITORED_APIS.includes(api)),
	];
	const changes = [];

	for (const api of apiNames) {
		const expectedFiles = Array.isArray(expected[api]) ? expected[api] : [];
		const actualFiles = actual[api] ?? [];
		const added = difference(actualFiles, expectedFiles);
		const removed = difference(expectedFiles, actualFiles);
		if (added.length > 0 || removed.length > 0) {
			changes.push({ api, added, removed });
		}
	}

	return changes;
}

async function main() {
	const actual = await scanInstalledMantine();

	// `--write` exists so that ACCEPTING an upgrade is a deliberate one-liner
	// rather than hand-transcribing dozens of paths into JSON. Without it the
	// only way to clear a legitimate Mantine bump is manual editing, which is
	// slow enough that the honest outcome would be someone deleting the check.
	// It is not wired into any script or CI step: the diff must be READ by a
	// person first, and this only records what they decided.
	if (process.argv.includes("--write")) {
		await writeFile(
			SNAPSHOT_PATH,
			`${JSON.stringify(actual, null, "\t")}\n`,
			"utf8",
		);
		console.log(
			`Snapshot rewritten from the installed packages: ${SNAPSHOT_PATH}`,
		);
		return;
	}

	const expected = JSON.parse(await readFile(SNAPSHOT_PATH, "utf8"));
	const changes = surfaceDiff(expected, actual);

	if (changes.length > 0) {
		console.error("Mantine ESM browser API surface changed:");
		for (const { api, added, removed } of changes) {
			console.error(`\n${api}`);
			for (const file of added) {
				console.error(`  + ${file}`);
			}
			for (const file of removed) {
				console.error(`  - ${file}`);
			}
		}
		console.error(
			"\nInspect the affected components and real test behavior. If the change is expected, re-record it with `pnpm exec node scripts/check-mantine-api-surface.mjs --write`.",
		);
		process.exitCode = 1;
		return;
	}

	const referenceCount = Object.values(actual).reduce(
		(total, files) => total + files.length,
		0,
	);
	console.log(
		`Mantine ESM API surface matches snapshot (${MONITORED_APIS.length} APIs, ${referenceCount} file references).`,
	);
}

main().catch((error) => {
	console.error(error instanceof Error ? error.message : error);
	process.exitCode = 1;
});
