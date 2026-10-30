"""Tests for schema-level contracts in afterthread.schemas."""

import json
import typing
from typing import Annotated, Any

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, TypeAdapter, ValidationError

from afterthread import schemas
from afterthread.main import app
from afterthread.schemas import (
    ToolDeleteResponse,
    ToolDiscardResponse,
    ToolDraft,
    ToolDraftDiscardResponse,
    ToolInstallRequest,
    ToolJobDraft,
    ToolJobDraftPublish,
)
from afterthread.services.memory_ai import SECTION_FIELD_ORDER


def _allows_null(prop: dict[str, Any]) -> bool:
    """True if an OpenAPI property schema admits a JSON `null`."""
    if prop.get("type") == "null":
        return True
    return any(variant.get("type") == "null" for variant in prop.get("anyOf", []))


def test_memory_item_update_openapi_has_no_nullable_properties() -> None:
    """Every MemoryItemUpdate property must be a plain, non-nullable type.

    Previously every field was typed `X | None`, so OpenAPI advertised each
    property as nullable (an `anyOf` with a `null` member) while a runtime
    validator rejected explicit `null` with a 422 -- a client generated
    honestly from the schema could legally send `null` and be rejected.
    Fields are now their ordinary non-nullable types, so the schema and the
    runtime behaviour agree: `null` fails ordinary type validation instead.
    """
    client = TestClient(app)
    schema = client.get("/openapi.json").json()
    properties = schema["components"]["schemas"]["MemoryItemUpdate"]["properties"]

    assert properties, "expected MemoryItemUpdate to have properties"
    for name, prop in properties.items():
        assert not _allows_null(prop), f"{name} still allows null: {prop}"


def test_memory_item_update_openapi_properties_have_no_default_key() -> None:
    """No MemoryItemUpdate property may advertise a `default` in OpenAPI.

    Every field is declared with `Field(default_factory=...)` rather than a
    literal default (e.g. `title: str = ""`) precisely because pydantic v2
    omits `default_factory`-sourced values from the generated JSON schema.
    The router never applies these defaults -- `update_item` always calls
    `model_dump(exclude_unset=True)` -- so a literal default would be a pure
    schema artifact: an OpenAPI client that honestly materialises declared
    defaults would send e.g. `title=""` (or `status="capture-quick"`) on
    every PATCH, tripping the not-blank validator or silently resetting
    fields the caller never meant to touch. Asserting no property carries a
    `default` key keeps that failure mode from silently coming back.
    """
    client = TestClient(app)
    schema = client.get("/openapi.json").json()
    properties = schema["components"]["schemas"]["MemoryItemUpdate"]["properties"]

    assert properties, "expected MemoryItemUpdate to have properties"
    for name, prop in properties.items():
        assert "default" not in prop, f"{name} still has a default: {prop}"


def _assert_title_tags_section_bounds(props: dict[str, Any]) -> None:
    """title max_length 300, tags max 20 items of 1..100 chars, every section
    text field max_length 20000 -- the CRUD-side bound that keeps a
    legitimate POST/PATCH from writing an item whose title/tags/sections are
    large enough to blow the enrich/assist-update prompt header budget (see
    afterthread.services.memory_ai._serialize_item_for_prompt and its _CRUD_* mirror
    constants in afterthread.schemas).
    """
    assert props["title"]["maxLength"] == 300
    assert props["tags"]["maxItems"] == 20
    assert props["tags"]["items"]["minLength"] == 1
    assert props["tags"]["items"]["maxLength"] == 100
    for field in SECTION_FIELD_ORDER:
        assert props[field]["maxLength"] == 20000, field


def test_memory_item_create_declares_title_tags_and_section_bounds() -> None:
    client = TestClient(app)
    schema = client.get("/openapi.json").json()
    props = schema["components"]["schemas"]["MemoryItemCreate"]["properties"]
    _assert_title_tags_section_bounds(props)


def test_memory_item_update_declares_title_tags_and_section_bounds() -> None:
    client = TestClient(app)
    schema = client.get("/openapi.json").json()
    props = schema["components"]["schemas"]["MemoryItemUpdate"]["properties"]
    _assert_title_tags_section_bounds(props)


def test_progress_entry_create_declares_note_length_bound() -> None:
    client = TestClient(app)
    schema = client.get("/openapi.json").json()
    prop = schema["components"]["schemas"]["ProgressEntryCreate"]["properties"]["note"]
    assert prop["maxLength"] == 20000


def test_tool_openapi_describes_the_versioned_metadata_paths() -> None:
    """Published operator documentation must name the files runtime actually reads."""

    schema = TestClient(app).get("/openapi.json").json()
    toggle_description = schema["paths"]["/api/tools/{name}"]["patch"]["description"]
    summary_description = schema["components"]["schemas"]["ToolSummaryDetail"]["description"]

    assert ".afterthread.meta/state.json" in toggle_description
    assert ".afterthread-state.json" not in toggle_description
    assert "versions/<vid>/.afterthread.meta/summary.json" in summary_description
    assert ".ai_meta.json" not in summary_description


# --- ToolInstallRequest install-form secret pair (D36) ----------------------


def _install_req(**overrides: Any) -> ToolInstallRequest:
    base: dict[str, Any] = {
        "openapi_url": "http://kb.example/openapi.json",
        "instructions": "build",
    }
    base.update(overrides)
    return ToolInstallRequest.model_validate(base)


def test_tool_install_request_accepts_no_secret() -> None:
    req = _install_req()
    assert req.secret_name is None
    assert req.secret_value is None


def test_tool_install_request_accepts_and_strips_valid_secret_pair() -> None:
    req = _install_req(secret_name="  KB_API_KEY  ", secret_value="  the-value  ")
    assert req.secret_name == "KB_API_KEY"
    assert req.secret_value == "the-value"


def test_tool_install_request_empty_secret_strings_normalize_to_none() -> None:
    """Whitespace-only for BOTH is 'no secret' (None), not a half-pair error."""
    req = _install_req(secret_name="   ", secret_value="")
    assert req.secret_name is None
    assert req.secret_value is None


def test_tool_install_request_accepts_six_char_value() -> None:
    """Exactly 6 chars is the floor (>= 6), so it is accepted (F3)."""
    req = _install_req(secret_name="KB_API_KEY", secret_value="abcdef")
    assert req.secret_value == "abcdef"


def test_tool_install_request_rejects_short_value_with_message() -> None:
    """F3: a value under the 6-char floor 422s with the fixed zh-TW message -- the
    redactor skips <6-char values, so accepting one would be unredactable."""
    with pytest.raises(ValidationError) as excinfo:
        _install_req(secret_name="KB_API_KEY", secret_value="abc")
    assert "秘密值長度至少 6 字元" in str(excinfo.value)


@pytest.mark.parametrize(
    "overrides",
    [
        {"secret_name": "KB_API_KEY"},  # value missing (half a pair)
        {"secret_value": "v"},  # name missing (half a pair)
        {"secret_name": "kb_key", "secret_value": "v"},  # lowercase-start name
        {"secret_name": "1KEY", "secret_value": "v"},  # digit-start name
        {"secret_name": "A" * 65, "secret_value": "v"},  # over 64 chars
        {"secret_name": "KB KEY", "secret_value": "v"},  # space in name
        {"secret_name": "KB_KEY", "secret_value": "line1\nline2"},  # multiline value
        {"secret_name": "KB_KEY", "secret_value": "abc"},  # under the 6-char floor (F3)
    ],
    ids=[
        "value-missing",
        "name-missing",
        "lowercase-start",
        "digit-start",
        "too-long",
        "space-in-name",
        "multiline-value",
        "short-value",
    ],
)
def test_tool_install_request_rejects_bad_secret_pair(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _install_req(**overrides)


def test_only_by_id_routes_declare_404() -> None:
    """Every by-id route -- and only those -- must declare 404 in OpenAPI so
    generated clients and docs match runtime. That is the four item routes
    (GET/PATCH/DELETE /api/items/{item_id} and POST /api/items/{item_id}/progress),
    the two by-id AI routes (POST /api/items/{item_id}/enrich and /assist-update),
    the by-id LLM log route (GET /api/llm/logs/{log_id}, which 404s for an
    unknown/evicted id), and the six by-name/by-id tool routes (PATCH/DELETE
    /api/tools/{name} for a missing package, GET /api/tools/jobs/{job_id}
    for an unknown/evicted/post-restart job, the two AI-summary routes
    under /api/tools/{name}/summary -- D40 -- which 404 when the named tool does
    not exist, TOOLS_DIR being unset included, and POST /api/tools/{name}/revise,
    plus the version-addressed discard, which 404 on the same gate), the draft
    discard (DELETE /api/tools/{name}/draft, a 404 for a missing tool or a tool with
    no draft), the draft publish (POST /api/tools/{name}/draft/publish, a 404 for
    a missing tool), the settings and
    run-list reads under /api/tools/{name}, and the by-id run record read and delete
    (/api/tool-runs/{run_id}, web-v6 §6), and the 跑一次範例 submit
    (POST /api/tools/{name}/check, web-v6 §10). The collection, review, health,
    status, capture, log-list, tool-list and install-submit routes cannot 404
    and must not declare it.

    The job poll is `/api/tools/jobs/{job_id}` since D40 renamed it from
    `/api/tools/install/{job_id}` (one endpoint now serves install AND revise
    jobs); the old path is gone, not aliased, so a stale entry here would be a
    route nothing serves.
    """
    client = TestClient(app)
    schema = client.get("/openapi.json").json()
    declaring_404 = {
        (path, method)
        for path, operations in schema["paths"].items()
        for method, operation in operations.items()
        if "404" in operation.get("responses", {})
    }
    assert declaring_404 == {
        ("/api/items/{item_id}", "get"),
        ("/api/items/{item_id}", "patch"),
        ("/api/items/{item_id}", "delete"),
        ("/api/items/{item_id}/progress", "post"),
        ("/api/items/{item_id}/enrich", "post"),
        ("/api/items/{item_id}/assist-update", "post"),
        ("/api/llm/logs/{log_id}", "get"),
        ("/api/tools/{name}", "patch"),
        ("/api/tools/{name}", "delete"),
        ("/api/tools/{name}/versions/{vid}", "delete"),
        ("/api/tools/{name}/draft", "delete"),
        ("/api/tools/{name}/draft/publish", "post"),
        ("/api/tools/jobs/{job_id}", "get"),
        ("/api/tools/{name}/summary", "get"),
        ("/api/tools/{name}/summary/regenerate", "post"),
        ("/api/tools/{name}/revise", "post"),
        ("/api/tools/{name}/settings", "get"),
        ("/api/tools/{name}/runs", "get"),
        ("/api/tool-runs/{run_id}", "get"),
        ("/api/tool-runs/{run_id}", "delete"),
        ("/api/tools/{name}/check", "post"),
    }


# --- backend paths in responses ----------------------------------------------

# A directory name read on Linux decodes with a lone surrogate for each byte that
# is not UTF-8 (here ``\xff``); made encodable, the surrogate becomes three
# replacement characters.
_NOT_UTF8_PATH = "/srv/tools-\udcff/kb/.afterthread.meta/draft"
_MADE_ENCODABLE = "/srv/tools-���/kb/.afterthread.meta/draft"

# The response fields that state a path on the backend's own filesystem, as
# decided by hand, with what else each model needs to be valid.
_UNUSABLE_DRAFT: dict[str, Any] = {
    "draft_id": None,
    "generation": None,
    "attempts": None,
    "state": "unusable",
    "interrupted": False,
    "base_vid": None,
    "path": "/srv/tools/kb/.afterthread.meta/draft",
    "draft_dir": "/srv/tools/kb/.afterthread.meta/draft",
    "kept": [],
    "discard_token": "0" * 64,
    "last": None,
}
_BACKEND_PATH_FIELDS: dict[tuple[type[BaseModel], str], dict[str, Any]] = {
    (ToolDraft, "path"): _UNUSABLE_DRAFT,
    (ToolDraft, "draft_dir"): _UNUSABLE_DRAFT,
    (ToolDraft, "kept"): _UNUSABLE_DRAFT,
    (ToolDraftDiscardResponse, "residual_path"): {},
    (ToolDeleteResponse, "retained_path"): {
        "outcome": "retained",
        "retention_reason": "cleanup_failed",
        "runs_deleted": False,
    },
    (ToolDiscardResponse, "retained_path"): {
        "outcome": "retained",
        "retention_reason": "cleanup_failed",
    },
    (ToolJobDraft, "retained_path"): {
        "saved": False,
        "draft_id": None,
        "generation": None,
        "attempts": None,
        "reason": "沒能存成草稿",
    },
    (ToolJobDraftPublish, "retained_path"): {
        "draft_id": "0" * 32,
        "generation": 2,
        "removed": False,
        "reason": None,
    },
}
# The listed fields that state a list of paths, each one made encodable.
_BACKEND_PATH_LISTS = {(ToolDraft, "kept")}


def _carries(annotation: Any, check: object) -> bool:
    """Whether ``check`` is ``Annotated`` metadata anywhere inside ``annotation``."""
    if typing.get_origin(annotation) is Annotated:
        base, *metadata = typing.get_args(annotation)
        return any(item == check for item in metadata) or _carries(base, check)
    return any(_carries(arg, check) for arg in typing.get_args(annotation))


def _fields_typed_backend_path() -> set[tuple[type[BaseModel], str]]:
    """Every field of every model in ``afterthread.schemas`` whose type carries
    ``BackendPath``'s check, however deep."""
    check = typing.get_args(schemas.BackendPath)[1]
    found: set[tuple[type[BaseModel], str]] = set()
    for model in vars(schemas).values():
        if not (
            isinstance(model, type)
            and issubclass(model, BaseModel)
            and model.__module__ == schemas.__name__
        ):
            continue
        for name, info in model.model_fields.items():
            if any(item == check for item in info.metadata) or _carries(info.annotation, check):
                found.add((model, name))
    return found


def test_backend_path_makes_a_path_encodable_and_leaves_an_encodable_one_as_it_is() -> None:
    """A path whose name is not UTF-8 comes back with replacement characters, and
    encodes; a path that encodes already, CJK included, comes back unchanged."""
    adapter = TypeAdapter(schemas.BackendPath)

    made = adapter.validate_python(_NOT_UTF8_PATH)

    assert made == _MADE_ENCODABLE
    made.encode("utf-8")
    for path in ("/srv/tools/kb", "/srv/工具/kb/草稿"):
        assert adapter.validate_python(path) == path


def test_the_listed_backend_path_fields_are_exactly_the_fields_typed_backend_path() -> None:
    """The fields listed as stating a backend path take the one type, and no other
    field does: a listed field typed plain ``str`` drops out of what is found
    here, and a field given the type without being listed is found. A new field
    that states a path, typed plain ``str`` and not listed, is in neither."""
    found = _fields_typed_backend_path()

    assert found
    assert found == set(_BACKEND_PATH_FIELDS)


@pytest.mark.parametrize(
    ("model", "field"),
    list(_BACKEND_PATH_FIELDS),
    ids=lambda value: getattr(value, "__name__", value),
)
def test_each_listed_backend_path_field_answers_a_path_that_is_not_utf8_made_encodable(
    model: type[BaseModel], field: str
) -> None:
    """Built with a path that does not encode as UTF-8, the response holds it made
    encodable and serializes: one such character would fail the encode of the
    whole response."""
    listed = (model, field) in _BACKEND_PATH_LISTS
    sent = [_NOT_UTF8_PATH] if listed else _NOT_UTF8_PATH
    expected = [_MADE_ENCODABLE] if listed else _MADE_ENCODABLE
    answer = model.model_validate({**_BACKEND_PATH_FIELDS[(model, field)], field: sent})

    assert getattr(answer, field) == expected
    assert json.loads(answer.model_dump_json())[field] == expected


def test_tools_and_the_schemas_make_text_encodable_with_one_definition() -> None:
    """``tools._utf8_safe`` and ``BackendPath``'s check are one function, so a path
    the services write and a path a response states are made encodable alike."""
    from afterthread import utf8
    from afterthread.services import tools

    assert tools._utf8_safe is utf8.encodable
    assert typing.get_args(schemas.BackendPath)[1].func is utf8.encodable


def test_the_revise_request_takes_a_draft_id_shaped_as_the_draft_store_mints_it() -> None:
    """The schema spells the draft id's pattern rather than import the tool
    subsystem; it is the draft store's own."""
    from afterthread.services import tool_drafts

    assert schemas._DRAFT_ID_PATTERN == tool_drafts.DRAFT_ID_PATTERN


@pytest.mark.parametrize(
    "fields",
    [{"draft_id": "a" * 32}, {"draft_generation": 1}],
    ids=["id-alone", "generation-alone"],
)
def test_a_revise_request_names_a_draft_by_both_its_id_and_generation_or_neither(
    fields: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        schemas.ToolReviseRequest.model_validate(
            {"feedback": "x", "expected_vid": "20260728T010203Z-abcdef", **fields}
        )


def test_a_revise_request_attaches_as_many_runs_as_one_revise_carries() -> None:
    """The request's cap on ``run_ids`` is the number of records a revise carries
    and a draft keeps: more would be attached and then dropped unseen."""
    from afterthread.services import tool_drafts, tool_runs

    assert schemas._REVISE_RUN_IDS_MAX == tool_runs.RUNS_PER_REVISE == tool_drafts._RUNS_KEPT


def test_a_revise_request_naming_a_draft_needs_neither_feedback_nor_runs() -> None:
    """Continuing a draft is something to do on its own; without one, a request
    with neither feedback nor runs still has nothing to do."""
    continuing = schemas.ToolReviseRequest.model_validate(
        {"expected_vid": "20260728T010203Z-abcdef", "draft_id": "a" * 32, "draft_generation": 1}
    )

    assert (continuing.feedback, continuing.run_ids) == (None, [])
    with pytest.raises(ValidationError):
        schemas.ToolReviseRequest.model_validate({"expected_vid": "20260728T010203Z-abcdef"})
