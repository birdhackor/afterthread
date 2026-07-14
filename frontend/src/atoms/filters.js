// Cross-page filter + pagination state for the memory item list. Kept as small
// primitive atoms so the filter bar can bind each control directly; the list
// page owns the debounce + page-reset timing (UI concerns) so these atoms stay
// dumb and reusable.

import { atom } from "jotai";

// Items per page for the list view (backend accepts limit 1..200).
export const DEFAULT_LIMIT = 20;

// "" means "no filter" (all values). buildQuery() drops empty strings, so an
// empty atom is simply omitted from the request.
export const statusFilterAtom = atom("");
export const stageFilterAtom = atom("");
export const tagFilterAtom = atom("");
export const qFilterAtom = atom("");

// 1-based page index; offset is derived as (page - 1) * DEFAULT_LIMIT.
export const pageAtom = atom(1);

// Reset every filter and pagination back to their defaults (清除 button).
export const resetFiltersAtom = atom(null, (_get, set) => {
	set(statusFilterAtom, "");
	set(stageFilterAtom, "");
	set(tagFilterAtom, "");
	set(qFilterAtom, "");
	set(pageAtom, 1);
});
