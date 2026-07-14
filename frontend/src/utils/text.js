// Unicode-code-point length, matching the backend's Pydantic length bounds
// (which count code points via Python's len(str)). JS's native `.length` --
// and native `maxLength` props -- count UTF-16 code units instead, so a
// non-BMP character (most emoji, some CJK extension characters) counts as 2
// there but 1 here. Using `.length`/`maxLength` for a cap that mirrors a
// backend bound therefore either rejects valid input early (client-side
// `.length` check) or silently cuts text off mid-codepoint before the user
// reaches the real limit (native `maxLength`). Every length-bound validation
// rule that mirrors a backend cap must use this helper instead; array/element
// counts (e.g. number of tags) are unaffected and keep using `.length`.
export function codePointLength(str) {
	return [...str].length;
}
