"""FastAPI application entrypoint for the Context Memory backend."""

import importlib.resources
import logging
import sys
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from context_memory.db import init_db
from context_memory.routers import ai, items, review, tools


def _configure_app_logging() -> None:
    """Attach a console handler to the "context_memory" logger namespace, once.

    context_memory.services.llm_log names its logger "context_memory.llm" but
    deliberately never calls ``addHandler``/``setLevel`` on it (see that
    module's comment) -- a LIBRARY-style module should not decide where its
    records end up, only how they are categorized. An APPLICATION entrypoint
    is exactly the layer that legitimately DOES make that call once, for the
    whole "context_memory" namespace: this is that one place, run at import
    time (before ``app = FastAPI(...)`` below) so it is in effect for every
    request the process ever serves, including one triggered by uvicorn's own
    import machinery before the ASGI app is even built.

    Without this, "context_memory.llm" has no handler and inherits the stdlib
    ROOT logger's default level (WARNING) -- so llm_log._log_summary's INFO
    line is silently discarded before a LogRecord is even constructed
    (``Logger.isEnabledFor`` fails first), and the "live console sink" that
    module's docstring promises never actually fires under a bare `uvicorn
    context_memory.main:app` run. Setting the level here, on the PARENT
    "context_memory" logger rather than the leaf "context_memory.llm", also
    means any future sibling module under this namespace (not just llm_log)
    gets the same console sink for free without a second setup call.

    ``sys.stderr`` (not stdout): uvicorn's own access/error logs already go to
    stderr by default, so this keeps the whole process to ONE interleaved,
    chronological console stream rather than splitting related lines across
    two file descriptors a reader would have to interleave by hand.

    ``propagate = False`` stops a record from continuing past this logger to
    the stdlib ROOT logger. Without it, a future consumer that configures the
    root logger (a different embedding, a test harness, uvicorn's own
    `--log-config`) would print every "context_memory.*" line TWICE -- once
    from the handler attached here, once from root's. Since this module is
    the one place that attaches a handler for the whole namespace, nothing
    upstream of it needs the record to keep traveling.

    The `if logger.handlers: return` guard makes this call idempotent, which
    matters for two real scenarios, not just hygiene: uvicorn's `--reload` /
    multi-worker modes re-import this module in ways that can run it more
    than once in the same process, and so can a test suite that imports
    `context_memory.main` from multiple test modules. Without the guard, a
    second call would attach a SECOND StreamHandler and every line would print
    twice from then on -- silently, since duplicate handlers is not an error.
    """
    logger = logging.getLogger("context_memory")
    if logger.handlers:
        return
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(handler)
    logger.propagate = False


_configure_app_logging()


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
    """Create database tables on startup."""
    init_db()
    yield


app = FastAPI(title="Context Memory API", lifespan=lifespan)

# Dev-only: the vite dev server (localhost:5173) calls this API cross-origin,
# so its responses need CORS headers to be readable by that page. Packaged
# mode never exercises this middleware at all -- the SPA is served same-origin
# from this very FastAPI process (see the frontend-serving block below), so
# the browser never issues a cross-origin request in the first place.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(items.router, prefix="/api")
app.include_router(review.router, prefix="/api")
app.include_router(ai.router, prefix="/api")
app.include_router(tools.router, prefix="/api")

# Packaged mode only. `scripts/build-wheel.sh` copies the built frontend into
# `context_memory/static/` before `uv build` runs, and that directory's
# `artifacts` entry in pyproject.toml carries it into the installed wheel
# (see D02 in docs/web-v2-decisions.md). In dev nothing ever populates this
# directory, so `_STATIC_DIR.is_dir()` is False and the block below is a
# no-op -- dev keeps serving the frontend from the separate `vite` dev server
# and its `/api` proxy, completely untouched by anything here.
#
# `app.frontend()` (FastAPI >=0.138) registers the SPA as *low-priority*
# routes: every path operation above -- including the ordinary 404 FastAPI
# itself raises for a registered-but-missing resource, e.g.
# `GET /api/items/999` -- is matched first and always wins, regardless of
# definition order; the frontend fallback is only ever consulted once
# nothing above has matched (see fastapi.routing._FrontendRouteGroup). Its
# `fallback="index.html"` only fires for requests that look like a browser
# navigating (GET/HEAD, no file extension on the last path segment, and an
# `Accept` that does not explicitly rule out HTML) -- a request for a
# missing asset path (e.g. `/assets/does-not-exist.js`) still 404s instead of
# silently getting index.html's bytes back under the wrong content type.
_STATIC_DIR = importlib.resources.files("context_memory") / "static"
if _STATIC_DIR.is_dir():
    app.frontend("/", directory=str(_STATIC_DIR), fallback="index.html")


@app.middleware("http")
async def _api_accept_normalization_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Pin the `/api` namespace to JSON: it never negotiates HTML.

    The SPA fallback (`app.frontend()` above) serves index.html for any
    unmatched GET/HEAD whose `Accept` does not rule HTML out -- and a bare
    `*/*` (curl's default, a plain `fetch()`'s default) does not rule it
    out. Without this middleware, a typo'd or removed `/api/...` path
    answers 200 text/html to such clients instead of 404 JSON, turning
    failures into fake successes for anyone scripting the API directly.
    The app's own client (frontend/src/api/client.js) already sends
    `Accept: application/json`; rewriting it here extends that same
    contract to every caller, so the API's 404s survive any client.

    A catch-all `/api/{path:path}` route was deliberately rejected: route
    matching prefers the first FULL match over a PARTIAL (method-mismatch)
    one, so a catch-all would convert every wrong-method request against a
    real endpoint (e.g. DELETE /api/health) from its correct 405 into a
    404. Rewriting `Accept` leaves route matching untouched. No endpoint
    under /api content-negotiates on Accept, so the rewrite is observable
    only in the unmatched-path case this exists for.

    The bare `/api` (no trailing slash) is matched explicitly: it is part
    of the namespace but not of the `/api/` prefix, and without it a
    `curl -f http://host/api` typo would exit 0 with the SPA shell -- the
    exact fake-success this middleware exists to prevent.
    """
    path = request.url.path
    if path == "/api" or path.startswith("/api/"):
        request.scope["headers"] = [
            (name, value) for name, value in request.scope["headers"] if name != b"accept"
        ] + [(b"accept", b"application/json")]
    return await call_next(request)


def _cache_control_for(path: str, content_type: str, status_code: int) -> str | None:
    """Decide the `Cache-Control` value for an outgoing response, if any.

    Pulled out of the middleware below as a pure function so pytest exercises
    the decision itself, not the header-mutation plumbing around it.

    - Anything served as `text/html` must never be cached -- regardless of
      status OR path: it is the SPA shell (`/`, a deep-link fallback like
      `/items/123`) or an error page, and a cached shell can outlive the
      hashed assets it references. Checked FIRST because the two rules
      overlap: an extensionless path under `/assets/` (e.g. a browser
      navigating to `/assets/missing`) is answered by the SPA fallback with
      200 index.html -- by path it looks like an asset, but it IS the shell,
      and marking it immutable would pin an old shell for a year.
    - Vite content-hashes every filename it emits under `/assets/`
      (e.g. `index-a1B2c3.js`): a given URL's bytes never change, so caching
      it "forever" and skipping revalidation entirely is safe. Only for a
      200, though: the hash's immutability promise is about the file's
      CONTENT, and any non-200 under `/assets/` (a 404 for an asset that is
      missing right now, a 405, ...) is a statement about the current moment
      -- caching it for a year would pin the failure long past the point a
      later deploy fixed it.
    - Everything else (JSON API responses, asset 404s, ...) is left alone:
      FastAPI already does the right thing for those, and returning `None`
      tells the middleware not to touch the response at all.
    """
    if content_type.startswith("text/html"):
        return "no-cache"
    if path.startswith("/assets/") and status_code == 200:
        return "public, max-age=31536000, immutable"
    return None


@app.middleware("http")
async def _cache_control_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Attach `Cache-Control` to responses per `_cache_control_for` above.

    `setdefault` rather than a plain assignment: Starlette's `StaticFiles`
    (which backs `app.frontend()`'s asset serving) already sets `ETag` /
    `Last-Modified` on every file response but never sets `Cache-Control`
    itself, so there is no existing value to preserve here -- this only
    guards against ever double-setting a header this middleware already
    applied.
    """
    response = await call_next(request)
    value = _cache_control_for(
        request.url.path, response.headers.get("content-type", ""), response.status_code
    )
    if value is not None:
        response.headers.setdefault("Cache-Control", value)
    return response


@app.get("/api/health")
def health() -> dict[str, str]:
    """Liveness/readiness probe."""
    return {"status": "ok"}
