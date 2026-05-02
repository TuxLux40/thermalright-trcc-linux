"""GUI composition root — wires TrccApp + Qt adapter.

Single entry point for the graphical interface. Owns all DI wiring:
    TrccApp.init() → renderer → system_svc → setup → autostart → TRCCApp

TRCCApp knows nothing about TrccApp. It implements AppObserver and receives
devices via on_app_event. All adapter deps are injected here.
"""
from __future__ import annotations

import logging
import os
import signal
import sys

from .base import BasePanel, ImageLabel
from .trcc_app import TRCCApp
from .uc_device import UCDevice
from .uc_preview import UCPreview
from .uc_theme_local import UCThemeLocal
from .uc_theme_mask import UCThemeMask
from .uc_theme_setting import UCThemeSetting
from .uc_theme_web import UCThemeWeb

__all__ = [
    'BasePanel',
    'ImageLabel',
    'TRCCApp',
    'UCDevice',
    'UCPreview',
    'UCThemeLocal',
    'UCThemeMask',
    'UCThemeSetting',
    'UCThemeWeb',
]

log = logging.getLogger(__name__)


def launch(verbosity: int = 0, decorated: bool = False,
           start_hidden: bool = False) -> int:
    """Bootstrap and run the GUI application.

    Returns the Qt exit code.
    """
    # Daemon-mode gate. The GUI's LCDHandler talks to real `LCDDevice`
    # instances; today it has no path to drive a `TrccProxy` over IPC.
    # Running anyway under TRCC_DAEMON=1 would race the daemon for USB,
    # so refuse to start with a clear message until the GUI gets its
    # Phase 9 refactor to use `_boot.trcc()` end-to-end.
    from trcc._boot import daemon_mode_enabled
    if daemon_mode_enabled():
        print(
            "[TRCC] GUI does not yet support TRCC_DAEMON=1.\n"
            "       Stop the daemon (`trcc kill`) and re-launch, or\n"
            "       unset TRCC_DAEMON to run the GUI in-process.",
            file=sys.stderr,
        )
        return 1

    from trcc.core.app import AppEvent, TrccApp
    app = TrccApp.init()

    # ── Platform deps (before Qt — configure_dpi must precede QApplication) ──
    platform = app.os

    # ── Single-instance lock ──────────────────────────────────────────────
    lock = platform.acquire_instance_lock()
    if lock is None:
        platform.raise_existing_instance()
        return 0

    # ── Qt bootstrap ──────────────────────────────────────────────────────
    from trcc.ui.gui.assets import _PKG_ASSETS_DIR, set_assets_dir
    set_assets_dir(platform.resolve_assets_dir(_PKG_ASSETS_DIR))

    os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.services=false")
    os.environ["QT_ENABLE_HIGHDPI_SCALING"] = "0"
    os.environ.pop("QT_QPA_PLATFORM", None)  # clear offscreen set by CLI

    platform.configure_dpi()

    from typing import cast

    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication
    qapp = cast(QApplication, QApplication.instance() or QApplication(sys.argv))
    qapp.setQuitOnLastWindowClosed(False)
    qapp.setDesktopFileName("trcc-linux")
    qapp.setProperty("_instance_lock", lock)

    font = QFont("Microsoft YaHei", 10)
    if not font.exactMatch():
        font = QFont("Sans Serif", 10)
    qapp.setFont(font)

    # ── Bootstrap — platform init + device scan, with splash progress ────
    from trcc.adapters.render.qt import QtRenderer
    from trcc.ui.gui.splash import run_bootstrap_with_splash
    if not run_bootstrap_with_splash(app, QtRenderer):
        return 1

    # ── System service (OS-specific sensors) ──────────────────────────────
    system_svc = app.build_system()
    app.set_system(system_svc)

    # ── GUI adapter — receives everything injected, knows nothing of TrccApp ─
    from trcc.ui.gui.trcc_app import TRCCApp as _TRCCApp
    window = _TRCCApp(
        system_svc=system_svc,
        platform=platform,
        decorated=decorated,
    )

    # ── IPC server ────────────────────────────────────────────────────────
    from trcc.ipc import IPCServer
    ipc_server = IPCServer()  # device wired later via on_app_event
    ipc_server.start()
    window._ipc_server = ipc_server

    # ── Register window as observer, replay scan results, start metrics ──
    # bootstrap() already ran scan(); registering now replays DEVICES_CHANGED
    # so window.on_app_event creates handlers for all pre-discovered devices.
    app.register(window)  # type: ignore[arg-type]
    app._notify(AppEvent.DEVICES_CHANGED, list(app._devices.values()))
    app.start_metrics_loop()

    # ── IPC raise + signals ───────────────────────────────────────────────
    signal.signal(signal.SIGINT, lambda *_: qapp.quit())
    platform.wire_ipc_raise(qapp, window)

    if not start_hidden:
        window.show()

    return qapp.exec()
