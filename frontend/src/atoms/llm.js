// Cross-page LLM configuration status. Fetched once on app start and shared by
// the shell banner and every AI action button. `configured` gates whether AI
// features are enabled; when false the shell shows a dismissible banner and
// AI buttons are disabled with an explanatory tooltip.

import { atom } from "jotai";
import { apiFetch, PROBE_TIMEOUT_MS } from "../api/client.js";

// Shape: { loaded, loading, configured, model, error }.
// `configured` starts true so the "not configured" banner never flashes before
// the first /api/llm/status response resolves.
export const llmStatusAtom = atom({
	loaded: false,
	loading: false,
	configured: true,
	model: null,
	error: null,
});

// Module-level generation counter guarding writes to llmStatusAtom, mirroring
// the requestId pattern ItemsListPage/HomePage use for their own fetches
// (a monotonic counter captured at request-start and re-checked before the
// response is applied). Two write paths OWN a state transition here -- a new
// loadLlmStatusAtom probe starting, and markLlmUnconfiguredAtom's
// authoritative 503 downgrade -- and both bump this counter. Every
// loadLlmStatusAtom call captures the post-bump value before awaiting its
// fetch, then re-checks it before writing the result: if the counter has
// moved on by then, a newer write already claimed the atom and this call's
// result (success or failure) is stale and must be dropped, INCLUDING the
// `loading` flag, which the newer write already owns. Without this, an older
// /api/llm/status response still in flight when a real AI call's 503
// authoritatively downgrades the atom can resolve afterwards and flip
// `configured` back to the optimistic true, re-enabling doomed AI buttons.
let generation = 0;

// Write-only action: load /api/llm/status into llmStatusAtom. Non-forced
// calls are idempotent -- they no-op while a request is in flight and after
// the first successful load. A `{ force: true }` call always starts a fresh
// probe, INCLUDING while an older request is still in flight -- it claims a
// newer generation, so the older in-flight response is discarded when it
// settles (see the guard inside for why silently dropping a force instead
// would strand the AI badge). On failure, a prior
// successful load's `configured`/`model` are preserved (a transport blip
// during a recheck must not silently overwrite an already-KNOWN status --
// e.g. flipping a known "not configured" back to the optimistic true would
// silently re-enable AI buttons the backend already told us to disable);
// only when status was NEVER successfully loaded does the optimistic
// `configured: true` fallback apply, so the "not configured" banner never
// flashes before the first response resolves.
export const loadLlmStatusAtom = atom(null, async (get, set, options = {}) => {
	const current = get(llmStatusAtom);
	// Non-forced dedupe only: "already loading" and "already loaded" both
	// yield to a force. A forced call must be able to supersede an IN-FLIGHT
	// request too, not just a settled one: the connectivity monitor fires
	// exactly one forced reload on the backend's false -> true recovery
	// transition, and that moment can easily find a doomed pre-outage status
	// request still pending -- dropping the force there would leave the AI
	// badge stale until a manual retry, because the recovery transition does
	// not come again. Letting it through is safe: the `++generation` below
	// happens before the older call's response settles, so that response
	// (success or failure) is discarded as stale and ownership of the atom
	// transfers to this newest call.
	if (!options.force && (current.loading || current.loaded)) {
		return;
	}

	// Claim the current generation for this call (see `generation` above)
	// before firing the request, exactly like `const id = ++requestId.current`
	// in the page fetch effects.
	const myGeneration = ++generation;
	set(llmStatusAtom, { ...current, loading: true, error: null });
	try {
		// Same PROBE_TIMEOUT_MS deadline as the health probe (see client.js):
		// force-piercing means a superseded status request is DROPPED (via the
		// generation check below) but never cancelled, so without a deadline a
		// status endpoint that accepts the connection and then hangs would leak
		// one pending request per recovery transition, without bound. The
		// timeout caps every request's lifetime at one bound, turning that
		// accumulation into a small, self-draining overlap. A timeout rejects
		// through the ordinary network-error path below -- for a trivial
		// no-DB/no-LLM endpoint, "can't answer within the bound" honestly reads
		// as a failed check.
		const data = await apiFetch("/api/llm/status", {
			method: "GET",
			signal: AbortSignal.timeout(PROBE_TIMEOUT_MS),
		});
		if (myGeneration !== generation) {
			// A newer load or markLlmUnconfiguredAtom's downgrade already claimed
			// a later generation while this request was in flight -- that write
			// owns the atom now, so drop this stale success instead of
			// overwriting it (e.g. clobbering an authoritative 503 downgrade).
			return;
		}
		set(llmStatusAtom, {
			loaded: true,
			loading: false,
			configured: Boolean(data?.configured),
			model: data?.model ?? null,
			error: null,
		});
	} catch (error) {
		if (myGeneration !== generation) {
			// Same staleness check as the success branch above, applied to the
			// failure path -- `current` here was captured before this call's
			// await, so writing it now would also resurrect whatever
			// configured/model pair was current back then.
			return;
		}
		const message = error?.message ?? "無法取得 AI 狀態";
		if (current.loaded) {
			// A previous load already resolved a real configured/model pair --
			// keep it (see rationale above) instead of clobbering it with the
			// never-loaded fallback below.
			set(llmStatusAtom, { ...current, loading: false, error: message });
		} else {
			set(llmStatusAtom, {
				loaded: true,
				loading: false,
				configured: true,
				model: null,
				error: message,
			});
		}
	}
});

// Write-only action: synchronously downgrade the shared status to "not
// configured". Call this the moment an actual AI call comes back with a 503
// llm_not_configured -- that response IS authoritative (the backend just
// told us, on this very request, that it can't serve AI calls right now), so
// this must not wait on loadLlmStatusAtom's async re-probe. That re-probe can
// itself be slow or keep failing (a transport blip preserves the prior
// `configured` per the catch branch above, which after this downgrade is
// already `false`), and until it resolves the buttons/banner must already
// read "not configured" -- otherwise a repeat guaranteed-failure keeps
// finding the AI buttons enabled. Callers still fire the force re-probe
// afterwards so a since-fixed backend can flip this back to `true`; this
// action only ever moves state to the disabled reading.
//
// Bumps `generation` first so any loadLlmStatusAtom probe already in flight
// (e.g. the initial app-start probe, still unresolved when this 503 lands)
// is stale by the time it resolves and discards its write instead of
// clobbering this downgrade -- see `generation` above.
export const markLlmUnconfiguredAtom = atom(null, (_get, set) => {
	generation++;
	set(llmStatusAtom, {
		loaded: true,
		loading: false,
		configured: false,
		model: null,
		error: null,
	});
});
