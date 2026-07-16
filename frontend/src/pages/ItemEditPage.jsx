import {
	Alert,
	Button,
	Center,
	Loader,
	Stack,
	Text,
	Title,
} from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "@tanstack/react-router";
import { useEffect, useMemo, useRef } from "react";
import { apiGet, apiPatch } from "../api/client.js";
import {
	applyServerFieldErrors,
	buildFormDefaults,
	ItemForm,
	SCALAR_FIELD_KEYS,
	sameTags,
} from "../components/ItemForm.jsx";
import { usePageTitle } from "../hooks/usePageTitle.js";

// Manual edit page: load the item, then PATCH only the fields the user changed
// (RHF dirtyFields for scalars, an explicit array diff for tags) so unchanged
// values are never resent and no field is ever nulled.
export function ItemEditPage() {
	usePageTitle("編輯項目");
	const { itemId } = useParams({ strict: false });
	const navigate = useNavigate();
	const queryClient = useQueryClient();

	// If the user leaves via the navbar while the update mutation below is in
	// flight, this page unmounts but the mutation still resolves -- skip the
	// success-path backToDetail() in that case so it can't yank the user back to
	// the item they just left. Set true on mount, false on cleanup, so it resets
	// correctly under StrictMode's double-mount.
	const isMountedRef = useRef(true);
	useEffect(() => {
		isMountedRef.current = true;
		return () => {
			isMountedRef.current = false;
		};
	}, []);

	// Load the item to seed the form. One contract, three parts:
	// - refetchOnMount: "always" -- entering the edit page ALWAYS fetches a
	//   fresh snapshot, even though ['item', itemId] is shared with the detail
	//   page and may already be cached (e.g. arriving here right after
	//   viewing it) -- an edit session must never silently adopt a cache
	//   entry that was seeded before this visit, no matter how "fresh" it
	//   still looks under the default staleTime.
	// - staleTime is NOT set (inherits the QueryClient default) -- Infinity
	//   would suppress the ABOVE guarantee for every OTHER consumer of this
	//   same key too (the detail page), since it disables react-query's own
	//   staleness math wholesale, not just this query's refetch-on-mount
	//   decision.
	// - refetchOnWindowFocus: false AND refetchOnReconnect: false (overriding
	//   the app-wide defaults) -- once the form is up, NO background refetch
	//   may fire mid-edit: not on focus, not on network reconnect. A mid-edit
	//   refetch updates the live cache under the standing form, and any code
	//   that then compares form values against the LIVE item (the tags diff
	//   used to) would misread "server moved" as "user changed it" and PATCH
	//   stale values back over newer server state.
	// fresh-on-entry, frozen-while-editing.
	const {
		data: item,
		error,
		isError,
		isFetching,
	} = useQuery({
		queryKey: ["item", itemId],
		queryFn: () => apiGet(`/api/items/${itemId}`),
		refetchOnMount: "always",
		refetchOnWindowFocus: false,
		refetchOnReconnect: false,
	});

	// refetchOnMount:"always" can still return an existing ['item', itemId]
	// cache entry SYNCHRONOUSLY (as `item`) while that guaranteed fetch is
	// still in flight (`isFetching`) -- e.g. arriving here right after
	// viewing the detail page -- so `item` can be briefly defined-but-stale
	// right after mount. react-hook-form's defaultValues (inside ItemForm) is
	// captured ONCE at mount and never resynced from a later prop change, so
	// mounting ItemForm against that transient stale value would freeze
	// exactly the snapshot this fix removes. formReadyRef latches the itemId
	// only once a result has actually settled (isFetching false) for it, so
	// ItemForm always mounts from the first result that arrives AFTER mount
	// -- and, once latched, never unlatches for that itemId, so a later
	// background refetch (e.g. after a network reconnect; only window-focus
	// refetching is disabled above) renders OVER the standing form instead of
	// unmounting it back to a Loader mid-edit and losing in-progress input.
	// Comparing against the CURRENT itemId (rather than a plain boolean) also
	// resets this correctly if the route param ever changes without a full
	// remount. This must run during render, not in an effect: a ref write
	// doesn't itself trigger a re-render, so an effect-based version would
	// only unlock formReady on the render AFTER this one -- one render too
	// late to gate THIS render's Loader-vs-form branch below.
	//
	// Two refinements over a bare "settled" latch (phase-3 review round 2):
	// - `!isError`: a settled ERROR must not latch. With a warm cache, the
	//   mount refetch 404ing (item deleted elsewhere) or failing leaves the
	//   stale `item` in place and isFetching false -- latching there would
	//   mount an editable form over a row that is gone (or unverified). The
	//   error branches below render instead.
	// - `formBaseRef` captures, ONCE per itemId at the latch moment, the exact
	//   item the form is initialized from. Everything about the edit session
	//   diffs against THIS snapshot -- never the live query data -- so even if
	//   some future code path refreshes the cache mid-edit, "did the user
	//   change tags?" keeps comparing the user's values to the values the
	//   user was shown, not to whatever the server has moved to since.
	const formReadyRef = useRef(null);
	const formBaseRef = useRef(null);
	if (
		!isFetching &&
		!isError &&
		item !== undefined &&
		formReadyRef.current !== itemId
	) {
		formReadyRef.current = itemId;
		formBaseRef.current = item;
	}
	const formReady = formReadyRef.current === itemId;
	const formBase = formReady ? formBaseRef.current : null;

	const defaults = useMemo(
		() => (formBase ? buildFormDefaults(formBase) : null),
		[formBase],
	);

	const backToDetail = () =>
		navigate({ to: "/items/$itemId", params: { itemId } });

	const updateMutation = useMutation({
		mutationFn: (patch) => apiPatch(`/api/items/${itemId}`, patch),
		onSuccess: () => {
			notifications.show({
				color: "green",
				title: "已更新",
				message: "項目已更新",
			});
			queryClient.invalidateQueries({ queryKey: ["item", itemId] });
			queryClient.invalidateQueries({ queryKey: ["items"] });
			queryClient.invalidateQueries({ queryKey: ["review"] });
			if (isMountedRef.current) {
				backToDetail();
			}
		},
	});

	const onSubmit = async (values, { dirtyFields, setError }) => {
		const patch = {};
		for (const key of SCALAR_FIELD_KEYS) {
			if (dirtyFields[key]) {
				patch[key] = key === "title" ? values[key].trim() : values[key];
			}
		}
		// Diff against the form's OWN base snapshot (formBase), never the live
		// query data: the user's edit is relative to what the user was shown.
		if (!sameTags(values.tags, formBase?.tags ?? [])) {
			patch.tags = values.tags;
		}

		if (Object.keys(patch).length === 0) {
			notifications.show({ color: "gray", message: "沒有變更" });
			backToDetail();
			return;
		}

		// mutateAsync so RHF's isSubmitting tracks the request and a 422's field
		// errors can be mapped onto the form exactly as before.
		try {
			await updateMutation.mutateAsync(patch);
		} catch (submitError) {
			applyServerFieldErrors(submitError, setError);
		}
	};

	// Not just `item === undefined && isFetching`: a warm cache can hand back
	// `item` immediately while the mount-triggered "always" refetch above is
	// still settling (see formReadyRef above), and that value must not reach
	// ItemForm. Keep showing the Loader until formReady latches.
	if (!formReady && isFetching) {
		return (
			<Center py="xl">
				<Loader />
			</Center>
		);
	}

	// 404 is authoritative even over a still-cached stale item (same rule as
	// the detail page): the server just said the row is gone, and mounting an
	// editable form over it only sets up a doomed PATCH.
	if (error?.status === 404) {
		return (
			<Stack gap="md" align="flex-start">
				<Title order={2}>找不到項目</Title>
				<Text c="dimmed">此項目可能已被刪除，或連結有誤。</Text>
				<Button component={Link} to="/items" variant="light">
					返回記憶清單
				</Button>
			</Stack>
		);
	}

	// Gated on !formReady (not item === undefined): with a warm cache a failed
	// mount refetch leaves stale `item` defined, but the latch above refuses to
	// set formReady on an errored settle -- so this branch (not a stale form)
	// is what renders. Once the form IS up, a later error can no longer unmount
	// it out from under in-progress edits (background refetches are disabled
	// above anyway).
	if (isError && !formReady) {
		return (
			<Alert color="red" title="載入失敗">
				<Text size="sm">{error?.message ?? "無法載入項目"}</Text>
			</Alert>
		);
	}

	// Belt-and-braces: no error, not fetching, but nothing latched either
	// (e.g. an empty cache in a transient pre-fetch render) -- keep the Loader
	// rather than mounting ItemForm with null defaults.
	if (!formBase) {
		return (
			<Center py="xl">
				<Loader />
			</Center>
		);
	}

	return (
		<Stack gap="md">
			<Title order={2}>編輯項目</Title>
			<ItemForm
				key={itemId}
				defaultValues={defaults}
				submitLabel="儲存變更"
				onSubmit={onSubmit}
				onCancel={backToDetail}
				isEdit
			/>
		</Stack>
	);
}
