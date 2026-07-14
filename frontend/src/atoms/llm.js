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
// unless called with { force: true } to refresh. On failure `configured` is
// left true (a transport blip must not falsely claim the LLM is unconfigured);
// only an explicit `configured: false` from the server shows the banner.
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
		set(llmStatusAtom, {
			loaded: true,
			loading: false,
			configured: true,
			model: null,
			error: error?.message ?? "無法取得 AI 狀態",
		});
	}
});
