"""ApiUI — `UI` adapter for the FastAPI HTTP server.

Same role as `CliUI`: translate inbound calls (HTTP requests instead of
argv) into typed `Command`\\ s and dispatch them through the universal
`Dispatcher` port. With ``TRCC_DAEMON=1`` the API process talks to a
running daemon (auto-spawning one if needed); without the flag it
dispatches in-process through ``_boot.trcc()``.

Phase 7 ships infrastructure. Existing FastAPI route handlers remain
untouched — they keep working through the legacy single-device proxy
during cutover. Phase 7b mechanically migrates each route to dispatch
via ApiUI so addressing ``device_id=N`` actually carries through to the
daemon's ``Trcc.lcd[N]``.
"""
from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

from ...core.dispatch import Dispatcher, InProcessDispatcher
from ...core.results import OpResult
from ...core.router import CommandRouter
from ..base import UI, ResultFormatter

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)

DAEMON_FLAG_ENV: str = "TRCC_DAEMON"


# =============================================================================
# JsonFormatter — OpResult → JSON-safe dict.
# =============================================================================

class JsonFormatter(ResultFormatter):
    """Format an `OpResult` as a JSON-serialisable mapping for HTTP responses."""

    def format(self, result: OpResult) -> dict[str, Any]:
        # Reuse the same result-to-dict logic the IPC server uses, so the
        # wire shape is identical between IPC and HTTP responses.
        from ...ipc import _result_to_dict
        return _result_to_dict(result)


# =============================================================================
# ApiUI — concrete `UI` for FastAPI.
# =============================================================================

class ApiUI(UI):
    """`UI` for the HTTP API.

    Concrete `handle()` (shared with every other UI) dispatches a named
    Command. `run()` is a no-op — uvicorn drives the FastAPI lifecycle
    from `_cmd_serve` in `cli/__init__.py`. Each route handler calls
    `ApiUI.handle("role.method", **kwargs)` and returns the formatted
    `OpResult`.
    """

    def run(self) -> int:
        # uvicorn owns the lifecycle; satisfies the UI ABC contract.
        return 0


# =============================================================================
# Dispatcher selection — flag-controlled, same shape as CliUI.
# =============================================================================

def daemon_mode_enabled() -> bool:
    """True when ``TRCC_DAEMON`` is set to a truthy value."""
    return os.environ.get(DAEMON_FLAG_ENV, "").lower() in ("1", "true", "yes", "on")


def get_dispatcher() -> Dispatcher:
    """Pick the `Dispatcher` for the current API process.

    Daemon-mode: connect to (or spawn) the daemon and proxy via
    `IpcDispatcher`. Standalone: dispatch in-process through
    `InProcessDispatcher` over the API process's own `Trcc`.
    """
    if daemon_mode_enabled():
        from ...daemon import ensure_daemon
        from ...ipc import IpcDispatcher
        if not ensure_daemon():
            raise RuntimeError(
                "TRCC_DAEMON=1 but no daemon is reachable. "
                "Start one explicitly with `trcc daemon` and try again.")
        return IpcDispatcher()
    from ...core.app import TrccApp
    return InProcessDispatcher(TrccApp.get()._trcc)


# =============================================================================
# Convenience — singleton ApiUI.
# =============================================================================

_api_ui: ApiUI | None = None


def api_ui() -> ApiUI:
    """Return the process-wide `ApiUI` ready to dispatch."""
    global _api_ui
    if _api_ui is None:
        _api_ui = ApiUI(get_dispatcher(), CommandRouter(), JsonFormatter())
    return _api_ui


def handle(name: str, **kwargs: Any) -> OpResult:
    """Build a Command, dispatch it, return the OpResult.

    Use from FastAPI route handlers::

        @router.post("/lcd/{lcd}/brightness")
        def set_lcd_brightness(lcd: int, body: BrightnessRequest):
            result = handle("lcd.set_brightness", index=lcd, percent=body.level)
            return _to_response(result)
    """
    return api_ui().handle(name, **kwargs)
