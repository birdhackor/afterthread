"""Build-time guard: refuse to package an artifact without the bundled SPA.

`scripts/build-wheel.sh` (repo root) stages the frontend's production build
into `context_memory/static/` before invoking `uv build`; pyproject.toml's
`artifacts` entries then carry that (gitignored) directory into the wheel
and sdist. Nothing in the build backend runs that staging step itself --
deliberately, per D02 in docs/web-v2-decisions.md: the build stays
transparent, with no pnpm invocation hidden inside build isolation. The
failure mode that leaves open is human: a bare `uv build` (or one run after
cleaning `static/`) would SILENTLY produce a wheel whose web UI 404s on
every page, discovered only after installation. This hook turns that silent
artifact corruption into a loud, immediate build error. It validates only --
it never builds anything.

Wired up via `[tool.hatch.build.targets.{wheel,sdist}.hooks.custom]` in
pyproject.toml (and included in the sdist file list, so the wheel-from-sdist
stage of `uv build` can run it too).
"""

from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class StaticBundleCheckHook(BuildHookInterface):
    """Fail non-editable builds when context_memory/static/index.html is absent."""

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        # Editable installs are the dev workflow (`uv sync`): dev serves the
        # frontend from the vite dev server, never from static/, so the
        # bundle is neither present nor needed there -- verified empirically
        # that `uv sync` passes version == "editable" here.
        if version == "editable":
            return
        index = Path(self.root) / "context_memory" / "static" / "index.html"
        if not index.is_file():
            raise RuntimeError(
                "context_memory/static/index.html is missing: the frontend bundle "
                "has not been staged, so this wheel/sdist would ship without its "
                "web UI (every page would 404). Build via scripts/build-wheel.sh "
                "(repo root), which stages the frontend and then runs uv build."
            )
