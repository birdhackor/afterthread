// Pure helpers for the 工具 page's read-only 設定 section (web-v6 §9): what a
// package's operator-owned .env and config.env say, shown without ever showing a
// secret. A sibling of toolSummary.js rather than part of it -- these files are
// package-layer state that outlives every version, not a version's summary --
// and kept out of the component for the same reason as its siblings: vitest runs
// in node without jsdom, so a pure function is the only testable surface.
//
// The backend (GET /api/tools/{name}/settings) already did the security work:
// .env arrives as KEY NAMES only, and a config.env value that contains a known
// secret arrives as the redaction marker. Nothing here can make that weaker; it
// only decides wording.

export function toolSettingsQueryKey(name) {
	return ["tool-settings", name];
}

// React keys for a list of names that may repeat: the names arrive already
// redacted, so two keys that both equal a secret come back as the same marker.
// `${name}-${occurrence}` is TagList's scheme -- stable per distinct value, and
// no raw array index (Biome's noArrayIndexKey).
export function occurrenceKeys(names) {
	const seen = new Map();
	return names.map((name) => {
		const occurrence = seen.get(name) ?? 0;
		seen.set(name, occurrence + 1);
		return `${name}-${occurrence}`;
	});
}

// Badges beside one config.env pair, most serious first. Each one is a fact the
// backend computed, not a guess made here:
// * contains_secret -- the value equals or contains (exactly) a value some
//   .env registers: very likely a secret in the wrong file, and config.env
//   values are NOT masked anywhere else;
// * secret_like_name -- the KEY reads like a credential (KEY/TOKEN/SECRET/
//   PASSWORD); a reminder only, since a value can never prove it is a setting;
// * overridden_by_env -- .env sets the same key, and its value is the one that
//   runs, so editing this line changes nothing.
export function configEntryFlags(entry) {
	const flags = [];
	if (entry.contains_secret) {
		flags.push({ color: "red", label: "含有 .env 的秘密" });
	}
	if (entry.secret_like_name) {
		flags.push({ color: "orange", label: "名稱像秘密" });
	}
	if (entry.overridden_by_env) {
		flags.push({ color: "gray", label: "被 .env 覆蓋" });
	}
	return flags;
}

// Notices above the two lists. An unreadable file is reported because an empty
// list would otherwise read as "no settings"; the pre-0.4.0 reminder is the one
// place a migrated package's .env gets a second look (web-v6 §1 A5), and only
// when there is something in it to look at.
export function settingsNotices(settings) {
	const notices = [];
	if (!settings.env_readable) {
		notices.push({
			color: "red",
			text: "無法讀取 .env（例如它是連結、不是一般檔案，或超過 64 KiB）：工具執行時會當作沒有 .env，修訂也會被拒絕。",
		});
	}
	if (!settings.config_readable) {
		notices.push({
			color: "red",
			text: "無法讀取 config.env（例如它是連結、不是一般檔案，或超過 64 KiB）：工具執行時會當作沒有 config.env，修訂也會被拒絕。",
		});
	}
	if (settings.built_before_v5 && settings.env_keys.length > 0) {
		notices.push({
			color: "orange",
			text: "這個工具建於 0.4.0 以前，當時的 AI 可能把設定（例如 TTL、網址）也寫進了 .env。.env 的每個值都會被當成秘密遮蔽：太短的值會讓修訂被拒，其他的則會在所有 AI 日誌裡被遮掉。不是秘密的，請把那一行移到同目錄的 config.env。",
		});
	}
	return notices;
}
