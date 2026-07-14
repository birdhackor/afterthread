// Cross-page LLM configuration status. Fetched once on app start and shared by
// the shell banner and every AI action button. `configured` gates whether AI
// features are enabled; when false the shell shows a dismissible banner and
// AI buttons are disabled with an explanatory tooltip.

import { atom } from "jotai";
import { apiGet } from "../api/client.js";

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

// Write-only action: load /api/llm/status into llmStatusAtom. Idempotent -- it
// no-ops while a request is in flight and after the first successful load,
// unless called with { force: true } to refresh. On failure, a prior
// successful load's `configured`/`model` are preserved (a transport blip
// during a recheck must not silently overwrite an already-KNOWN status --
// e.g. flipping a known "not configured" back to the optimistic true would
// silently re-enable AI buttons the backend already told us to disable);
// only when status was NEVER successfully loaded does the optimistic
// `configured: true` fallback apply, so the "not configured" banner never
// flashes before the first response resolves.
export const loadLlmStatusAtom = atom(null, async (get, set, options = {}) => {
	const current = get(llmStatusAtom);
	if (current.loading) {
		return;
	}
	if (current.loaded && !options.force) {
		return;
	}

	set(llmStatusAtom, { ...current, loading: true, error: null });
	try {
		const data = await apiGet("/api/llm/status");
		set(llmStatusAtom, {
			loaded: true,
			loading: false,
			configured: Boolean(data?.configured),
			model: data?.model ?? null,
			error: null,
		});
	} catch (error) {
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
