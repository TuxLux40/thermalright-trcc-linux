"""GUI composition root — wires Qt adapter.

Single entry point for the graphical interface. Builds the windowed
``QApplication`` (which Qt requires before any QWidget), constructs a
``QtRenderer`` against it, then delegates the rest of the DI graph to
``trcc._boot.trcc()``. ``discover_now=False`` lets the splash show
progress while USB connect + theme extraction run in a background
``BootstrapWorker``.
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
    # so refuse to start with a clear message. Removing the gate is a
    # follow-up GUI refactor (handlers consume Trcc.lcd command facade
    # instead of holding LCDDevice references).
    from trcc._boot import daemon_mode_enabled
    if daemon_mode_enabled():
        print(
            "[TRCC] GUI does not yet support TRCC_DAEMON=1.\n"
            "       Stop the daemon (`trcc kill`) and re-launch, or\n"
            "       unset TRCC_DAEMON to run the GUI in-process.",
            file=sys.stderr,
        )
        return 1

    # ── Platform first — needed for lock check, DPI config, autostart, etc.
    from trcc.adapters.system import make_platform
    platform = make_platform()

    # ── Single-instance lock — acquire before any heavy setup ────────────
    lock = platform.acquire_instance_lock()
    if lock is None:
        platform.raise_existing_instance()
        return 0

    # ── Qt bootstrap (windowed QApp — must precede QtRenderer construction)
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

    # ── Build Trcc with the windowed QApp's renderer; defer discovery so
    # the splash can show progress while USB connect + theme extraction
    # run in BootstrapWorker.
    from trcc._boot import trcc as _boot_trcc
    from trcc.adapters.render.qt import QtRenderer
    renderer = QtRenderer()
    t = _boot_trcc(platform, renderer=renderer,
                   discover_now=False, verbosity=verbosity)

    # ── Splash + background discover ─────────────────────────────────────
    from trcc.ui.gui.splash import run_bootstrap_with_splash
    if not run_bootstrap_with_splash(t):
        return 1

    # ── GUI adapter — pulls Trcc handle via _boot.trcc() (cached)  ───────
    from trcc.ui.gui.trcc_app import TRCCApp as _TRCCApp
    window = _TRCCApp(
        platform=platform,
        decorated=decorated,
    )

    # ── IPC server bound to Trcc — manifold dispatch for clients ────────
    from trcc.ipc import IPCServer
    ipc_server = IPCServer(trcc=t)
    ipc_server.start()
    window._ipc_server = ipc_server

    # ── Replay device list to the window — subscribers run in __init__,
    # so they missed the publish that happened during discover. ─────────
    from itertools import chain

    from trcc.core.events import Topic
    t.events.publish(
        Topic.DEVICE_LIST,
        tuple(chain(t.lcd_devices, t.led_devices)),
    )
    t.start_metrics_loop()

    # ── IPC raise + signals ───────────────────────────────────────────────
    signal.signal(signal.SIGINT, lambda *_: qapp.quit())
    platform.wire_ipc_raise(qapp, window)

    if not start_hidden:
        window.show()

    return qapp.exec()
