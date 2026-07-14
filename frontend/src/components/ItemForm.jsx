import {
	Button,
	Fieldset,
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
import { codePointLength } from "../utils/text.js";
import { CharCounter } from "./CharCounter.jsx";

const TITLE_MAX = 300;
const TAG_MAX = 100;
const MAX_TAGS = 20;

// Scalar fields whose dirtiness RHF tracks reliably. Tags (an array) is diffed
// separately (see sameTags below) by both the edit page's PATCH decision and
// this form's dirty-only validation bypass, so the two can never disagree
// about what counts as "untouched".
export const SCALAR_FIELD_KEYS = [
	"title",
	"status",
	"stage",
	...SECTION_FIELD_KEYS,
];

// True when two tag arrays hold the same values in the same order. Shared by
// ItemEditPage (decides whether tags belongs in the PATCH) and this form's
// tags validate rule (decides whether an edit-mode tags value is "untouched"
// and should therefore skip the count/length rules) -- one definition keeps
// "will be PATCHed" and "validation applies" in sync.
export function sameTags(a, b) {
	if (a.length !== b.length) {
		return false;
	}
	return a.every((value, index) => value === b[index]);
}

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
// `isEdit` (false for the create page) gates EVERY bounded field's cap/format
// rule -- title (maxLength + required-trim), tags (count/length) and every
// section textarea -- so each one applies only once that specific field is
// dirty (scalars via RHF `dirtyFields`, tags via the value-equality
// `sameTags` check above). A pre-existing item can already violate today's
// bounds -- a title saved before the 300-char limit existed, a tags array
// from before the 20-tag cap, or a history section (decisions/alternatives/
// rationale/consequences) the backend merged past 20000 via
// merge_with_supersede, which stores up to 60000 -- so validating an
// untouched field would block submitting an unrelated change (like just the
// status) even though that untouched field is never sent (edit PATCHes only
// dirty fields, see ItemEditPage). Each cap/required/trim check still applies
// the moment the user actually edits that specific field. Create mode is
// unaffected (isEdit defaults to false, and ItemNewPage never passes it).
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

	// Same bypass as the section fields below, hoisted once since (unlike
	// tags) title dirtiness doesn't depend on the value passed into validate.
	const titleUntouchedInEdit = isEdit && !dirtyFields.title;

	return (
		<form onSubmit={submit}>
			<Stack gap="lg">
				{/* A native <fieldset disabled> locks every input while a submit
				is in flight, so edits typed during a slow POST/PATCH aren't
				silently lost once the response lands and navigates away.
				Reaches every Controller-driven input in one place (RHF's
				`field` object carries no `disabled` prop to spread) and,
				unlike passing `disabled` to each control individually, also
				disables TagsInput's per-pill remove buttons -- Mantine
				doesn't wire those to the input's own `disabled` prop, but
				the browser disables them anyway as fieldset descendants.
				variant="unstyled" keeps it visually a no-op. */}
				<Fieldset variant="unstyled" disabled={isSubmitting}>
					<Stack gap="lg">
						<Stack gap="md">
							<Controller
								name="title"
								control={control}
								rules={{
									validate: (value) => {
										if (titleUntouchedInEdit) {
											return true;
										}
										if (value.trim() === "") {
											return "請輸入標題";
										}
										if (codePointLength(value) > TITLE_MAX) {
											return `標題不可超過 ${TITLE_MAX} 字`;
										}
										return true;
									},
								}}
								render={({ field, fieldState }) => (
									<div>
										<TextInput
											{...field}
											label="標題"
											withAsterisk
											placeholder="這個項目在追蹤什麼？"
											error={fieldState.error?.message}
										/>
										<CharCounter
											value={field.value}
											max={TITLE_MAX}
											suppressOverLimit={titleUntouchedInEdit}
										/>
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
									// Unlike title, "untouched" can't be hoisted from dirtyFields
									// (tags dirtiness isn't reliably tracked by RHF -- see
									// SCALAR_FIELD_KEYS above) so it's recomputed here from the
									// live value against the original, exactly mirroring
									// ItemEditPage's own sameTags-based PATCH decision.
									validate: (tags) => {
										if (isEdit && sameTags(tags, defaultValues.tags)) {
											return true;
										}
										if (tags.length > MAX_TAGS) {
											return `標籤最多 ${MAX_TAGS} 個`;
										}
										if (tags.some((tag) => tag.trim() === "")) {
											return "標籤不可為空白";
										}
										if (tags.some((tag) => codePointLength(tag) > TAG_MAX)) {
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
														codePointLength(value) <= SECTION_MAX_LENGTH ||
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
					</Stack>
				</Fieldset>

				<Group justify="flex-end">
					<Button
						type="button"
						variant="default"
						onClick={onCancel}
						disabled={isSubmitting}
					>
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
