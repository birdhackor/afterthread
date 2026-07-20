// Shared definition of the memory-item section groups, mirroring the
// methodology used across the app. Both the detail view and the create/edit
// form iterate over this single source so the section vocabulary, grouping and
// display order never drift apart. Each group has a zh-TW `title`; multi-field
// groups additionally give every field its own zh-TW sub-`label`.

export const SECTION_GROUPS = [
	{
		id: "snapshot",
		title: "捕捉快照",
		fields: [{ key: "snapshot", label: "快照" }],
	},
	{
		id: "why",
		title: "為何重要",
		fields: [{ key: "why_matters", label: "為何重要" }],
	},
	{
		id: "understanding",
		title: "目前理解",
		fields: [
			{ key: "known", label: "已知" },
			{ key: "inferred", label: "推測" },
			{ key: "unknown", label: "未知" },
		],
	},
	{
		id: "decisions",
		title: "決策與理由",
		fields: [
			{ key: "decisions", label: "決策" },
			{ key: "alternatives", label: "替代方案" },
			{ key: "rationale", label: "理由" },
			{ key: "consequences", label: "後果" },
		],
	},
	{
		id: "constraints",
		title: "限制與假設",
		fields: [
			{ key: "constraints", label: "限制" },
			{ key: "assumptions", label: "假設" },
		],
	},
	{
		id: "risks",
		title: "風險",
		fields: [{ key: "risks", label: "風險" }],
	},
	{
		id: "evidence",
		title: "證據連結",
		fields: [{ key: "evidence", label: "證據連結" }],
	},
	{
		id: "open_questions",
		title: "未解問題",
		fields: [{ key: "open_questions", label: "未解問題" }],
	},
	{
		id: "next_actions",
		title: "下一步",
		fields: [{ key: "next_actions", label: "下一步" }],
	},
	{
		id: "recovery",
		title: "恢復線索",
		fields: [
			{ key: "recovery_keywords", label: "關鍵字" },
			{ key: "recovery_people", label: "相關人員" },
			{ key: "recovery_files", label: "相關檔案" },
			{ key: "resume_trigger", label: "重啟觸發" },
		],
	},
];

// Flat list of every section field key in display order. Used by the form to
// build default values and to diff dirty fields against the loaded item.
export const SECTION_FIELD_KEYS = SECTION_GROUPS.flatMap((group) =>
	group.fields.map((field) => field.key),
);

// Upper bound the backend enforces on every section text field.
export const SECTION_MAX_LENGTH = 20000;
