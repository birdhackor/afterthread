import {
	Button,
	Group,
	Select,
	Stack,
	TagsInput,
	Textarea,
	TextInput,
	Title,
} from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { Controller, useForm } from "react-hook-form";
import { STAGE_OPTIONS, STATUS_OPTIONS } from "../constants/labels.js";
import {
	SECTION_FIELD_KEYS,
	SECTION_GROUPS,
	SECTION_MAX_LENGTH,
} from "../constants/sections.js";
import { CharCounter } from "./CharCounter.jsx";

const TITLE_MAX = 300;
const TAG_MAX = 100;
const MAX_TAGS = 20;

// Scalar fields whose dirtiness RHF tracks reliably. Tags (an array) is diffed
// separately by the edit page.
export const SCALAR_FIELD_KEYS = [
	"title",
	"status",
	"stage",
	...SECTION_FIELD_KEYS,
];

// Build a complete default-values object (every field present) from a partial
// item, so the form is fully controlled and dirty-tracking has a stable
// baseline. Called with no argument for the create form.
export function buildFormDefaults(item = {}) {
	const sections = {};
	for (const key of SECTION_FIELD_KEYS) {
		sections[key] = item[key] ?? "";
	}
	return {
		title: item.title ?? "",
		status: item.status ?? "capture-quick",
		stage: item.stage ?? "quick",
		tags: item.tags ?? [],
		...sections,
	};
}

// Map a normalized ApiError onto the form. 422 field errors (keyed by the
// backend field path, e.g. "title" or "tags.0") are attached to the matching
// RHF field; everything else falls back to a red notification.
export function applyServerFieldErrors(error, setError) {
	if (error?.status === 422 && error.fieldErrors) {
		let applied = false;
		for (const [field, message] of Object.entries(error.fieldErrors)) {
			const name = field.split(".")[0];
			setError(name, { type: "server", message });
			applied = true;
		}
		if (applied) {
			notifications.show({
				color: "red",
				title: "資料有誤",
				message: "請修正標示的欄位後再試",
			});
			return;
		}
	}
	notifications.show({
		color: "red",
		title: "儲存失敗",
		message: error?.message ?? "無法儲存項目",
	});
}

// Shared create/edit form. `onSubmit(values, { dirtyFields, setError, reset })`
// lets the create page POST the whole payload while the edit page PATCHes only
// the dirty fields. Client-side rules mirror the backend bounds; server 422s
// are surfaced via applyServerFieldErrors.
//
// `isEdit` (false for the create page) gates the section-field length rule:
// a pre-existing item's section can already hold more than SECTION_MAX_LENGTH
// chars server-side -- e.g. a history section (decisions/alternatives/
// rationale/consequences) the backend merged past 20000 via
// merge_with_supersede, which stores up to 60000 -- so validating an
// untouched field's length would block submitting an unrelated change (like
// the title) even though that field is never sent (edit PATCHes only dirty
// fields, see ItemEditPage). The cap still applies once the user actually
// edits that field.
export function ItemForm({
	defaultValues,
	submitLabel,
	onSubmit,
	onCancel,
	isEdit = false,
}) {
	const {
		control,
		handleSubmit,
		setError,
		reset,
		formState: { isSubmitting, dirtyFields },
	} = useForm({ defaultValues });

	const submit = handleSubmit((values) =>
		onSubmit(values, { dirtyFields, setError, reset }),
	);

	return (
		<form onSubmit={submit}>
			<Stack gap="lg">
				<Stack gap="md">
					<Controller
						name="title"
						control={control}
						rules={{
							required: "請輸入標題",
							maxLength: {
								value: TITLE_MAX,
								message: `標題不可超過 ${TITLE_MAX} 字`,
							},
							validate: (value) => value.trim() !== "" || "請輸入標題",
						}}
						render={({ field, fieldState }) => (
							<div>
								<TextInput
									{...field}
									label="標題"
									withAsterisk
									maxLength={TITLE_MAX}
									placeholder="這個項目在追蹤什麼？"
									error={fieldState.error?.message}
								/>
								<CharCounter value={field.value} max={TITLE_MAX} />
							</div>
						)}
					/>

					<Group grow align="flex-start">
						<Controller
							name="status"
							control={control}
							render={({ field }) => (
								<Select
									{...field}
									label="狀態"
									data={STATUS_OPTIONS}
									allowDeselect={false}
								/>
							)}
						/>
						<Controller
							name="stage"
							control={control}
							render={({ field }) => (
								<Select
									{...field}
									label="階段"
									data={STAGE_OPTIONS}
									allowDeselect={false}
								/>
							)}
						/>
					</Group>

					<Controller
						name="tags"
						control={control}
						rules={{
							validate: (tags) => {
								if (tags.length > MAX_TAGS) {
									return `標籤最多 ${MAX_TAGS} 個`;
								}
								if (tags.some((tag) => tag.trim() === "")) {
									return "標籤不可為空白";
								}
								if (tags.some((tag) => tag.length > TAG_MAX)) {
									return `每個標籤不可超過 ${TAG_MAX} 字`;
								}
								return true;
							},
						}}
						render={({ field, fieldState }) => (
							<TagsInput
								label="標籤"
								value={field.value}
								onChange={field.onChange}
								onBlur={field.onBlur}
								maxTags={MAX_TAGS}
								placeholder="輸入後按 Enter 新增"
								error={fieldState.error?.message}
							/>
						)}
					/>
				</Stack>

				{SECTION_GROUPS.map((group) => {
					const single = group.fields.length === 1;
					return (
						<Stack key={group.id} gap="sm">
							<Title order={4}>{group.title}</Title>
							{group.fields.map((sectionField) => {
								// Never block submit over a field the user didn't touch --
								// it isn't part of the dirty-only PATCH either way (see the
								// module doc comment above).
								const untouchedInEdit =
									isEdit && !dirtyFields[sectionField.key];
								return (
									<Controller
										key={sectionField.key}
										name={sectionField.key}
										control={control}
										rules={{
											validate: (value) =>
												untouchedInEdit ||
												value.length <= SECTION_MAX_LENGTH ||
												`不可超過 ${SECTION_MAX_LENGTH} 字`,
										}}
										render={({ field, fieldState }) => (
											<div>
												<Textarea
													{...field}
													label={single ? undefined : sectionField.label}
													placeholder={
														single ? group.title : sectionField.label
													}
													autosize
													minRows={3}
													maxLength={SECTION_MAX_LENGTH}
													error={fieldState.error?.message}
												/>
												<CharCounter
													value={field.value}
													max={SECTION_MAX_LENGTH}
													suppressOverLimit={untouchedInEdit}
												/>
											</div>
										)}
									/>
								);
							})}
						</Stack>
					);
				})}

				<Group justify="flex-end">
					<Button type="button" variant="default" onClick={onCancel}>
						取消
					</Button>
					<Button type="submit" loading={isSubmitting}>
						{submitLabel}
					</Button>
				</Group>
			</Stack>
		</form>
	);
}
