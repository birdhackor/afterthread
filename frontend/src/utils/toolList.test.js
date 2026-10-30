import { QueryClient, QueryObserver } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";
import {
	listReadAfter,
	markListReads,
	readToolList,
	rereadToolList,
} from "./toolList.js";

// A GET whose answers the test gives, one call at a time, in any order.
function scriptedGet() {
	const calls = [];
	const get = (path) =>
		new Promise((resolve, reject) => {
			calls.push({ path, resolve, reject });
		});
	return { get, calls };
}

// The ["tools"] entry as the 工具 page holds it: one observer keeps it active, so a
// re-read refetches it, and its first load starts as the observer subscribes.
function watchedList(get) {
	const client = new QueryClient({
		defaultOptions: {
			queries: { retry: false, staleTime: 5_000, networkMode: "always" },
		},
	});
	const observer = new QueryObserver(client, {
		queryKey: ["tools"],
		queryFn: () => readToolList(get),
	});
	const unsubscribe = observer.subscribe(() => {});
	return { client, unsubscribe };
}

describe("readToolList / listReadAfter", () => {
	it("reads the tools list and counts a read that started after the mark", async () => {
		const { get, calls } = scriptedGet();
		const mark = markListReads();
		const reading = readToolList(get);
		expect(calls.map((call) => call.path)).toEqual(["/api/tools"]);
		calls[0].resolve({ tools: [{ name: "kb" }] });
		const list = await reading;
		expect(list.tools).toEqual([{ name: "kb" }]);
		expect(listReadAfter(list, mark)).toBe(true);
	});

	it("does not count a read that started before the mark, though it lands after it", async () => {
		const { get, calls } = scriptedGet();
		const reading = readToolList(get);
		const mark = markListReads();
		calls[0].resolve({ tools: [] });
		expect(listReadAfter(await reading, mark)).toBe(false);
	});

	it("does not count a list that carries no read number, or a missing mark", async () => {
		expect(listReadAfter({ tools: [] }, markListReads())).toBe(false);
		const { get, calls } = scriptedGet();
		const reading = readToolList(get);
		calls[0].resolve({ tools: [] });
		const list = await reading;
		expect(listReadAfter(list, null)).toBe(false);
		expect(listReadAfter(list, undefined)).toBe(false);
		expect(listReadAfter(undefined, markListReads())).toBe(false);
	});

	it("does not count a list because a later read started, when that read failed", async () => {
		const { get, calls } = scriptedGet();
		const first = readToolList(get);
		calls[0].resolve({ tools: [] });
		const list = await first;
		const mark = markListReads();
		const second = readToolList(get).catch(() => null);
		calls[1].reject(new Error("boom"));
		expect(await second).toBeNull();
		// The list on screen is still the one the first read produced.
		expect(listReadAfter(list, mark)).toBe(false);
	});
});

describe("rereadToolList on the page's cached list", () => {
	it("starts a new read after the mark while the first load is still in flight", async () => {
		const { get, calls } = scriptedGet();
		const { client, unsubscribe } = watchedList(get);
		expect(calls).toHaveLength(1);
		const mark = markListReads();
		const rereading = rereadToolList(client);
		await vi.waitFor(() => expect(calls).toHaveLength(2), { timeout: 500 });
		// The first load lands late; it began before the mark and must not count.
		calls[0].resolve({ tools: [{ name: "before" }] });
		calls[1].resolve({ tools: [{ name: "after" }] });
		await rereading;
		const list = client.getQueryData(["tools"]);
		expect(list.tools).toEqual([{ name: "after" }]);
		expect(listReadAfter(list, mark)).toBe(true);
		unsubscribe();
	});

	it("leaves the earlier list in place, not counted, when the re-read fails", async () => {
		const { get, calls } = scriptedGet();
		const { client, unsubscribe } = watchedList(get);
		calls[0].resolve({ tools: [{ name: "before" }] });
		await vi.waitFor(() =>
			expect(client.getQueryData(["tools"])).toBeDefined(),
		);
		const mark = markListReads();
		const rereading = rereadToolList(client);
		await vi.waitFor(() => expect(calls).toHaveLength(2), { timeout: 500 });
		calls[1].reject(new Error("boom"));
		await expect(rereading).resolves.toBeUndefined();
		const list = client.getQueryData(["tools"]);
		expect(list.tools).toEqual([{ name: "before" }]);
		expect(listReadAfter(list, mark)).toBe(false);
		unsubscribe();
	});
});
