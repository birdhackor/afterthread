"""Unit tests for context_memory.main._cache_control_for.

Only the pure decision function is tested here (immutable / no-cache / no
header), per docs/web-v2-plan.md's Phase 2 brief -- the surrounding
middleware is just plumbing that calls it and is covered end-to-end by
e2e/wheel_smoke.sh against a real packaged build.
"""

from context_memory.main import _cache_control_for

_IMMUTABLE = "public, max-age=31536000, immutable"


def test_assets_200_is_immutable() -> None:
    assert (
        _cache_control_for("/assets/index-a1b2c3.js", "text/javascript; charset=utf-8", 200)
        == _IMMUTABLE
    )


def test_assets_404_gets_no_header() -> None:
    # A missing asset's 404 (served as JSON by FastAPI's HTTPException) is a
    # statement about the current moment, not about the hashed content --
    # caching it for a year would pin the failure. It must fall through the
    # immutable branch AND stay clear of the html branch.
    assert _cache_control_for("/assets/gone-b4d.js", "application/json", 404) is None


def test_html_response_is_no_cache() -> None:
    assert _cache_control_for("/", "text/html; charset=utf-8", 200) == "no-cache"


def test_html_deep_link_fallback_is_no_cache() -> None:
    assert _cache_control_for("/items/123", "text/html", 200) == "no-cache"


def test_html_error_page_is_no_cache_regardless_of_status() -> None:
    # The html branch is deliberately status-blind: an error page rendered as
    # HTML is even less worth caching than a stale shell.
    assert _cache_control_for("/whatever", "text/html; charset=utf-8", 404) == "no-cache"


def test_api_json_response_gets_no_header() -> None:
    assert _cache_control_for("/api/items", "application/json", 200) is None


def test_missing_content_type_gets_no_header() -> None:
    assert _cache_control_for("/api/health", "", 200) is None


def test_assets_200_wins_even_with_html_content_type() -> None:
    # Should not happen in practice (assets are never served as text/html),
    # but a 200 under /assets/ is immutable by construction (Vite
    # content-hashes the filename), so it must never fall through to the
    # no-cache branch regardless of whatever content type ends up on the
    # response.
    assert _cache_control_for("/assets/weird.html", "text/html", 200) == _IMMUTABLE


def test_assets_non_200_html_falls_to_no_cache() -> None:
    # Composite of the two rules above: not a 200, so no immutable; text/html,
    # so the status-blind html branch still applies.
    assert _cache_control_for("/assets/weird.html", "text/html", 404) == "no-cache"


def test_non_assets_prefix_lookalike_is_not_immutable() -> None:
    # "/assets" (no trailing slash) and "/assetsfoo" must not match the
    # `/assets/` prefix check -- only a real path segment counts.
    assert _cache_control_for("/assets", "application/json", 200) is None
    assert _cache_control_for("/assetsfoo/bar.js", "application/javascript", 200) is None
