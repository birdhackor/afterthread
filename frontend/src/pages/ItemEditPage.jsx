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

	// Load the item to seed the form. staleTime: Infinity freezes this snapshot
	// for the edit session -- refetchOnWindowFocus never fires (the query is
	// never stale), so the dirty-field diff baseline (item.tags below) can't
	// shift under the user mid-edit, preserving the old load-once behavior. The
	// key is shared with the detail page's ['item', itemId] query, so arriving
	// from there populates the form instantly; a save invalidates the key, so
	// returning here later refetches fresh.
	const {
		data: item,
		error,
		isError,
		isFetching,
	} = useQuery({
		queryKey: ["item", itemId],
		queryFn: () => apiGet(`/api/items/${itemId}`),
		staleTime: Number.POSITIVE_INFINITY,
	});

	const defaults = useMemo(
		() => (item ? buildFormDefaults(item) : null),
		[item],
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
		if (!sameTags(values.tags, item.tags ?? [])) {
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

	if (item === undefined && isFetching) {
		return (
			<Center py="xl">
				<Loader />
			</Center>
		);
	}

	if (error?.status === 404 && item === undefined) {
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

	if (isError && item === undefined) {
		return (
			<Alert color="red" title="載入失敗">
				<Text size="sm">{error?.message ?? "無法載入項目"}</Text>
			</Alert>
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
