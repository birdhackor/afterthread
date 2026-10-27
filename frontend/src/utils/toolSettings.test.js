import { describe, expect, it } from "vitest";
import {
	configEntryFlags,
	settingsNotices,
	toolSettingsQueryKey,
} from "./toolSettings.js";

const entry = (overrides = {}) => ({
	key: "CACHE_TTL",
	value: "3600",
	overridden_by_env: false,
	secret_like_name: false,
	contains_secret: false,
	...overrides,
});

const settings = (overrides = {}) => ({
	env_keys: [],
	env_readable: true,
	config: [],
	config_readable: true,
	built_before_v5: false,
	...overrides,
});

describe("toolSettingsQueryKey", () => {
	it("is addressed by the package name alone", () => {
		expect(toolSettingsQueryKey("kb")).toEqual(["tool-settings", "kb"]);
	});
});

describe("configEntryFlags", () => {
	it("shows nothing for a plain setting", () => {
		expect(configEntryFlags(entry())).toEqual([]);
	});

	it("puts the most serious flag first", () => {
		const labels = configEntryFlags(
			entry({
				overridden_by_env: true,
				secret_like_name: true,
				contains_secret: true,
			}),
		).map((flag) => flag.label);
		expect(labels).toEqual(["含有 .env 的秘密", "名稱像秘密", "被 .env 覆蓋"]);
	});
});

describe("settingsNotices", () => {
	it("is empty for readable files and a package built since 0.4.0", () => {
		expect(settingsNotices(settings({ env_keys: ["API_KEY"] }))).toEqual([]);
	});

	it("reports each unreadable file, so an empty list is not read as no settings", () => {
		const texts = settingsNotices(
			settings({ env_readable: false, config_readable: false }),
		).map((notice) => notice.text);
		expect(texts).toHaveLength(2);
		expect(texts[0]).toContain(".env");
		expect(texts[1]).toContain("config.env");
	});

	it("reminds a pre-0.4.0 package to move settings out of .env, only when it has one", () => {
		expect(settingsNotices(settings({ built_before_v5: true }))).toEqual([]);
		const [notice] = settingsNotices(
			settings({ built_before_v5: true, env_keys: ["CACHE_TTL"] }),
		);
		expect(notice.color).toBe("orange");
		expect(notice.text).toContain("config.env");
	});
});
