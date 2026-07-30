import { Stack, Title } from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { useMutation, useQueryClient } from "@tanstack/react-query";
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
	const queryClient = useQueryClient();
	const defaults = useMemo(() => buildFormDefaults(), []);

	// If the user leaves via the navbar while the create mutation below is in
	// flight, this page unmounts but the mutation still resolves -- skip the
	// success-path navigate in that case so it can't yank the user back to the
	// just-created item from wherever they navigated to instead. The toast still
	// shows (it's still true, and no longer where anyone's looking makes it
	// harmless).
	const isMountedRef = useRef(true);
	useEffect(() => {
		isMountedRef.current = true;
		return () => {
			isMountedRef.current = false;
		};
	}, []);

	// The new item shows up in the list and review buckets, so invalidate both;
	// navigation to the detail page then loads it via its own ['item', id] query.
	const createMutation = useMutation({
		mutationFn: (payload) => apiPost("/api/items", { body: payload }),
		onSuccess: (created) => {
			notifications.show({
				color: "green",
				title: "已建立",
				message: `已建立「${created.title}」`,
			});
			queryClient.invalidateQueries({ queryKey: ["items"] });
			queryClient.invalidateQueries({ queryKey: ["review"] });
			if (isMountedRef.current) {
				navigate({
					to: "/items/$itemId",
					params: { itemId: String(created.id) },
				});
			}
		},
	});

	// mutateAsync so RHF's isSubmitting (which ItemForm's Fieldset/submit button
	// read) tracks the request, and a 422's field errors can be mapped onto the
	// form here exactly as before.
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
			await createMutation.mutateAsync(payload);
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
