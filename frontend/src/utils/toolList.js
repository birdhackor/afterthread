// Where the 工具 page's copy of GET /api/tools came from, in the order the page
// started its reads. The card of a revise, or of the publish of a draft, may say
// something about the tool's CURRENT draft, and offer to continue or publish it,
// only on a list read that STARTED after the card's job was seen to end and that
// SUCCEEDED (see jobDraftView in toolDraft.js).
// Neither "the list has data" nor "the re-read settled" says that:
//
// * a read already in flight when the job ended can land after it, with the draft
//   as it was before the round saved;
// * a re-read that fails leaves the previous list on screen, and react-query keeps
//   that data;
// * a re-read asked for while the very first load is still in flight joins that
//   load instead of starting a new one (query-core's Query.fetch continues the
//   running retryer when there is no data yet), so it too began before the end.
//
// So each read is numbered when it STARTS, the number travels inside the data it
// produces, and the moment the page sees a job end takes a number from the same
// counter. A list is fresh for that job when its number is the larger. A failed
// read produces no data, so the list on screen keeps the number of the read that
// did produce it.
//
// The counter lives in this module, not in a component: the cached list outlives
// a remount of the page, and a counter that started again from zero would let an
// old list outrank a new mark.

let lastListRead = 0;

function nextListRead() {
	lastListRead += 1;
	return lastListRead;
}

// A point in the order of list reads: every read that starts after this call
// carries a larger number than the one returned.
export function markListReads() {
	return nextListRead();
}

// The tools list, read with `get` (the API client's GET), carrying the number the
// read was given when it started (`readSeq`). Every reader of the ["tools"] cache
// entry goes through this, so whatever the entry holds says where it came from.
export async function readToolList(get) {
	const readSeq = nextListRead();
	const list = await get("/api/tools");
	return { ...list, readSeq };
}

// Whether `list` comes from a read that started after `mark`. Anything that does
// not carry a number, or a missing mark, is not.
export function listReadAfter(list, mark) {
	return (
		Number.isInteger(mark) &&
		Number.isInteger(list?.readSeq) &&
		list.readSeq > mark
	);
}

// Read the tools list again, starting a NEW read: one already in flight is
// cancelled first, so the re-read cannot be that earlier read joined. Errors are
// not raised; whether the read produced a list is told by the list itself.
export async function rereadToolList(queryClient) {
	await queryClient.cancelQueries({ queryKey: ["tools"], exact: true });
	await queryClient.invalidateQueries({ queryKey: ["tools"], exact: true });
}
