"""Unit tests for context_memory.main._cache_control_for.

Only the pure decision function is tested here (immutable / no-cache / no
header), per docs/web-v2-plan.md's Phase 2 brief -- the surrounding
middleware is just plumbing that calls it and is covered end-to-end by
e2e/wheel_smoke.sh against a real packaged build.
"""

from context_memory.main import _cache_control_for


def test_assets_path_is_immutable() -> None:
    assert (
        _cache_control_for("/assets/index-a1b2c3.js", "text/javascript; charset=utf-8")
        == "public, max-age=31536000, immutable"
    )


def test_html_response_is_no_cache() -> None:
    assert _cache_control_for("/", "text/html; charset=utf-8") == "no-cache"


def test_html_response_without_charset_is_no_cache() -> None:
    assert _cache_control_for("/items/123", "text/html") == "no-cache"


def test_api_json_response_gets_no_header() -> None:
    assert _cache_control_for("/api/items", "application/json") is None


def test_missing_content_type_gets_no_header() -> None:
    assert _cache_control_for("/api/health", "") is None


def test_assets_path_wins_even_with_html_content_type() -> None:
    # Should not happen in practice (assets are never served as text/html),
    # but the path check is deliberately unconditional -- an /assets/ URL is
    # immutable by construction (Vite content-hashes the filename), so it
    # must never fall through to the no-cache branch regardless of whatever
    # content type ends up on the response.
    assert (
        _cache_control_for("/assets/weird.html", "text/html")
        == "public, max-age=31536000, immutable"
    )


def test_non_assets_prefix_lookalike_is_not_immutable() -> None:
    # "/assets" (no trailing slash) and "/assetsfoo" must not match the
    # `/assets/` prefix check -- only a real path segment counts.
    assert _cache_control_for("/assets", "application/json") is None
    assert _cache_control_for("/assetsfoo/bar.js", "application/javascript") is None
