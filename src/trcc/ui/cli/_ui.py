"""CliUI — `UI` adapter for Typer.

Translates argv-derived calls into typed `Command`\\ s and dispatches
them. Lives behind ``TRCC_DAEMON=1`` for now: when the flag is on, the
CLI talks to the running daemon (auto-spawning one if needed); when the
flag is off the legacy in-process path through ``_boot.trcc()`` is used,
so existing scripts keep working through the cutover.

Phase 6 ships the infrastructure (this module) plus a few example
migrations in ``_display.py``. Phase 6b mechanically rewrites the
remaining ~50 CLI command functions to dispatch through `CliUI.handle`
instead of the direct ``trcc().lcd.X(...)`` form.
"""
from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from ...core.dispatch import Dispatcher, InProcessDispatcher
from ...core.results import OpResult
from ...core.router import CommandRouter
from ..base import UI, ResultFormatter

if TYPE_CHECKING:
    from typing import Any

log = logging.getLogger(__name__)

DAEMON_FLAG_ENV: str = "TRCC_DAEMON"


# =============================================================================
# TextFormatter — OpResult → stdout line.
# =============================================================================

class TextFormatter(ResultFormatter):
    """Format an `OpResult` as a single human-readable terminal line."""

    def format(self, result: OpResult) -> str:
        return result.format()


# =============================================================================
# CliUI — concrete `UI` for the terminal.
# =============================================================================

class CliUI(UI):
    """`UI` for the terminal.

    Concrete `handle` (shared by every UI) routes a name + kwargs to a
    `Dispatcher`. `run` is intentionally a thin shim — the heavy lifting
    is Typer's argv parsing, which lives in ``cli/__init__.py``. Each
    Typer command function calls ``CliUI.handle("role.method", **kw)``
    and prints the formatted result.
    """

    def run(self) -> int:
        # Typer drives the lifecycle from main(); CliUI.run() is a no-op
        # so the UI ABC contract is satisfied without re-implementing the
        # Click/Typer dispatch loop. Concrete commands invoke handle()
        # directly.
        return 0


# =============================================================================
# Dispatcher selection — flag-controlled.
# =============================================================================

def daemon_mode_enabled() -> bool:
    """True when ``TRCC_DAEMON`` is set to a truthy value."""
    return os.environ.get(DAEMON_FLAG_ENV, "").lower() in ("1", "true", "yes", "on")


def get_dispatcher() -> Dispatcher:
    """Pick the `Dispatcher` for the current environment.

    With ``TRCC_DAEMON=1``: connect to the running daemon (auto-spawning
    one if needed) and return an `IpcDispatcher`. Without the flag:
    build an `InProcessDispatcher` from the per-process `Trcc` so legacy
    one-shot CLI invocations keep working without a daemon.
    """
    if daemon_mode_enabled():
        return _ipc_dispatcher_or_die()
    from ._boot import trcc as _trcc
    return InProcessDispatcher(_trcc())


def _ipc_dispatcher_or_die() -> Dispatcher:
    """Daemon-mode dispatcher. Auto-spawns the daemon if not running."""
    from ...daemon import ensure_daemon
    from ...ipc import IpcDispatcher
    if not ensure_daemon():
        raise RuntimeError(
            "TRCC_DAEMON=1 but no daemon is reachable. "
            "Start one explicitly with `trcc daemon` and try again.")
    return IpcDispatcher()


# =============================================================================
# Convenience — singleton CliUI for command modules to share.
# =============================================================================

_cli_ui: CliUI | None = None


def cli_ui() -> CliUI:
    """Return a process-wide `CliUI` ready to dispatch.

    Built once on first call; subsequent calls return the cache. The
    underlying `Dispatcher` is selected at construction time, so changing
    ``TRCC_DAEMON`` mid-process does not affect an already-cached UI —
    that mirrors how the env flag works elsewhere in the codebase.
    """
    global _cli_ui
    if _cli_ui is None:
        dispatcher = get_dispatcher()
        # CommandRouter without a Trcc → pass-through. When daemon-mode
        # is on, validation happens server-side; when off, the dispatcher's
        # own InProcessDispatcher already validates against the live Trcc.
        router = CommandRouter()
        _cli_ui = CliUI(dispatcher, router, TextFormatter())
    return _cli_ui


# =============================================================================
# Helpers used by command modules to dispatch + emit in one line.
# =============================================================================

def emit(result: OpResult) -> int:
    """Print the result and return its exit code.

    This replaces the ad-hoc ``_emit`` helpers scattered across each CLI
    submodule — every command can call ``return cli.emit(cli.handle(...))``
    once the migration completes.
    """
    import typer

    fmt = cli_ui()._format.format(result)  # ResultFormatter is concrete here
    if isinstance(fmt, str):
        if result.success:
            typer.echo(fmt)
        else:
            typer.echo(fmt, err=True)
    return result.exit_code


def handle(name: str, **kwargs: Any) -> OpResult:
    """Build a Command, dispatch it, return the OpResult.

    Shorthand for ``cli_ui().handle(name, **kwargs)``. Use this from CLI
    command functions::

        from trcc.ui.cli._ui import emit, handle
        @_cli_handler
        def send_image(image_path, *, lcd: int = 0):
            from pathlib import Path
            return emit(handle("lcd.send_image", index=lcd, path=str(Path(image_path))))
    """
    return cli_ui().handle(name, **kwargs)
