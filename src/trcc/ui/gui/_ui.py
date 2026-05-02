"""GuiUI — `UI` adapter for the PySide6 desktop GUI.

The GUI process is the most invasive UI to migrate because every Qt
slot today does ``self._trcc.lcd.X(idx, ...)`` directly. Phase 8 ships
the infrastructure (this module) so per-handler / per-panel call sites
can migrate one at a time onto :meth:`GuiUI.handle`. Phase 8b sweeps
the remaining ~70 call sites in ``gui/trcc_app.py`` + ``gui/lcd_handler.py``
+ ``gui/uc_*.py``.

Two modes coexist behind ``TRCC_DAEMON=1``:

  Off (default during cutover) — the GUI process owns its own `Trcc`,
  the daemon does not exist, and `GuiUI`'s dispatcher is an
  `InProcessDispatcher` over that local `Trcc`. Behaviour is identical
  to today's ``self._trcc.lcd.X(...)`` direct-call form.

  On — the GUI launches as a client of an existing daemon (or spawns
  one via :func:`trcc.daemon.ensure_daemon`). `GuiUI`'s dispatcher is
  an `IpcDispatcher` and every Command travels over the IPC socket.
  Multi-LCD keep-alive (`_ui_active`, `set_inactive`,
  `restore_inactive_state` in `LCDHandler`) is unaffected — that logic
  is pure GUI state and doesn't care about the dispatch transport.

`WidgetFormatter` is the GUI's `ResultFormatter`. It returns a typed
tuple of (success, message) for the calling slot to apply to the
status label / dialog / whatever Qt widget makes sense at the call
site. The GUI doesn't have one universal "format" output — every slot
applies the result differently — so the formatter just normalises the
shape and lets each caller decide.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from ...core.dispatch import Dispatcher, InProcessDispatcher
from ...core.results import OpResult
from ...core.router import CommandRouter
from ..base import UI, ResultFormatter

log = logging.getLogger(__name__)

DAEMON_FLAG_ENV: str = "TRCC_DAEMON"


# =============================================================================
# WidgetFormatter — GUI doesn't have one canonical output shape; this
# just normalises the result so each Qt slot can present it.
# =============================================================================

class WidgetFormatter(ResultFormatter):
    """Format an `OpResult` into a (success, message) tuple for Qt slots."""

    def format(self, result: OpResult) -> tuple[bool, str]:
        if result.success:
            return True, result.message or "OK"
        return False, result.error or result.message or "Failed"


# =============================================================================
# GuiUI — concrete `UI` for PySide6.
# =============================================================================

class GuiUI(UI):
    """`UI` for the desktop GUI.

    `handle` (inherited) dispatches a named Command and returns an
    `OpResult`. `run` is a thin shim — `qapp.exec()` is invoked from
    `gui/__init__.py::launch`, which pre-dates this UI ABC and stays
    in charge of the lifecycle through the cutover.
    """

    def run(self) -> int:
        # Lifecycle is owned by gui/__init__.py::launch (which calls
        # qapp.exec()). This satisfies the UI ABC contract.
        return 0


# =============================================================================
# Dispatcher selection — flag-controlled, same shape as CliUI / ApiUI.
# =============================================================================

def daemon_mode_enabled() -> bool:
    """True when ``TRCC_DAEMON`` is set to a truthy value."""
    return os.environ.get(DAEMON_FLAG_ENV, "").lower() in ("1", "true", "yes", "on")


def get_dispatcher(trcc: Any) -> Dispatcher:
    """Pick the `Dispatcher` for the GUI process.

    Daemon-on: connect to / spawn the daemon via `IpcDispatcher`.
    Daemon-off: dispatch in-process against the GUI's local `Trcc`.
    """
    if daemon_mode_enabled():
        from ...daemon import ensure_daemon
        from ...ipc import IpcDispatcher
        if not ensure_daemon():
            log.warning("TRCC_DAEMON=1 but daemon not reachable; "
                        "falling back to in-process dispatch")
            return InProcessDispatcher(trcc)
        return IpcDispatcher()
    return InProcessDispatcher(trcc)


# =============================================================================
# Convenience — GUI does NOT use a process-wide singleton like CliUI/ApiUI
# because each TRCCApp window holds its own Trcc and may rebuild it on
# device hotplug. Construct GuiUI per-window from gui/__init__.py::launch
# and pass it down to LCDHandler / panels via DI.
# =============================================================================

def build_gui_ui(trcc: Any) -> GuiUI:
    """Build a `GuiUI` for the given local `Trcc`.

    Called once at GUI startup from `gui/__init__.py::launch`. The
    resulting `GuiUI` is injected into `TRCCApp` and propagated to
    `LCDHandler`s and panels — same pattern as today's `_trcc` injection.
    """
    return GuiUI(get_dispatcher(trcc), CommandRouter(), WidgetFormatter())
