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
import { Link, useNavigate, useParams } from "@tanstack/react-router";
import { useEffect, useMemo, useRef, useState } from "react";
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
	const [state, setState] = useState({
		phase: "loading",
		item: null,
		error: null,
	});

	// If the user leaves via the navbar while the PATCH in onSubmit below is
	// in flight, this page unmounts but the promise still resolves -- skip
	// the success-path backToDetail() (a navigate()) in that case so it can't
	// yank the user back to the item they just left. Separate from `active`
	// in the load effect just below: that one guards a single fetch and
	// resets on every itemId change, while this ref must stay false for the
	// rest of the component's life once the page has actually unmounted.
	const isMountedRef = useRef(true);
	useEffect(() => {
		isMountedRef.current = true;
		return () => {
			isMountedRef.current = false;
		};
	}, []);

	useEffect(() => {
		let active = true;
		setState({ phase: "loading", item: null, error: null });
		apiGet(`/api/items/${itemId}`)
			.then((data) => {
				if (active) {
					setState({ phase: "success", item: data, error: null });
				}
			})
			.catch((error) => {
				if (active) {
					setState({
						phase: error?.status === 404 ? "notfound" : "error",
						item: null,
						error,
					});
				}
			});
		return () => {
			active = false;
		};
	}, [itemId]);

	const defaults = useMemo(
		() => (state.item ? buildFormDefaults(state.item) : null),
		[state.item],
	);

	const backToDetail = () =>
		navigate({ to: "/items/$itemId", params: { itemId } });

	const onSubmit = async (values, { dirtyFields, setError }) => {
		const patch = {};
		for (const key of SCALAR_FIELD_KEYS) {
			if (dirtyFields[key]) {
				patch[key] = key === "title" ? values[key].trim() : values[key];
			}
		}
		if (!sameTags(values.tags, state.item.tags ?? [])) {
			patch.tags = values.tags;
		}

		if (Object.keys(patch).length === 0) {
			notifications.show({ color: "gray", message: "沒有變更" });
			backToDetail();
			return;
		}

		try {
			await apiPatch(`/api/items/${itemId}`, patch);
			notifications.show({
				color: "green",
				title: "已更新",
				message: "項目已更新",
			});
			if (isMountedRef.current) {
				backToDetail();
			}
		} catch (error) {
			applyServerFieldErrors(error, setError);
		}
	};

	if (state.phase === "loading") {
		return (
			<Center py="xl">
				<Loader />
			</Center>
		);
	}

	if (state.phase === "notfound") {
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

	if (state.phase === "error") {
		return (
			<Alert color="red" title="載入失敗">
				<Text size="sm">{state.error?.message ?? "無法載入項目"}</Text>
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
