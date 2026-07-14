import { Stack, Title } from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { useNavigate } from "@tanstack/react-router";
import { useEffect, useMemo, useRef } from "react";
import { apiPost } from "../api/client.js";
import {
	applyServerFieldErrors,
	buildFormDefaults,
	ItemForm,
} from "../components/ItemForm.jsx";
import { SECTION_FIELD_KEYS } from "../constants/sections.js";
import { usePageTitle } from "../hooks/usePageTitle.js";

// Manual create page: POST the full form, then jump to the new item's detail.
export function ItemNewPage() {
	usePageTitle("新增項目");
	const navigate = useNavigate();
	const defaults = useMemo(() => buildFormDefaults(), []);

	// If the user leaves via the navbar while the POST below is in flight,
	// this page unmounts but the promise still resolves -- skip the
	// success-path navigate in that case so it can't yank the user back to
	// the just-created item from wherever they navigated to instead. The
	// toast still shows (it's still true, and no longer where anyone's
	// looking makes it harmless).
	const isMountedRef = useRef(true);
	useEffect(() => {
		isMountedRef.current = true;
		return () => {
			isMountedRef.current = false;
		};
	}, []);

	const onSubmit = async (values, { setError }) => {
		const payload = {
			title: values.title.trim(),
			status: values.status,
			stage: values.stage,
			tags: values.tags,
		};
		for (const key of SECTION_FIELD_KEYS) {
			payload[key] = values[key];
		}
		try {
			const created = await apiPost("/api/items", payload);
			notifications.show({
				color: "green",
				title: "已建立",
				message: `已建立「${created.title}」`,
			});
			if (isMountedRef.current) {
				navigate({
					to: "/items/$itemId",
					params: { itemId: String(created.id) },
				});
			}
		} catch (error) {
			applyServerFieldErrors(error, setError);
		}
	};

	return (
		<Stack gap="md">
			<Title order={2}>新增項目</Title>
			<ItemForm
				defaultValues={defaults}
				submitLabel="建立"
				onSubmit={onSubmit}
				onCancel={() => navigate({ to: "/items" })}
			/>
		</Stack>
	);
}
