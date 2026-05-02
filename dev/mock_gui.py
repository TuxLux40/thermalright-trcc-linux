#!/usr/bin/env python3
"""Mock GUI — real TRCC GUI with fake USB devices via MockPlatform.

Bootstrapped via dev/_mock_bootstrap.py: paths → preflight → MockPlatform
patched into ControllerBuilder. Then this script does only the GUI-specific
work (Qt setup, window, event loop). Bugs found here are real bugs.

Device config (dev/devices.json):
    [
        {"type": "lcd", "resolution": "320x320", "name": "Frozen Warframe Pro",
         "vid": "0402", "pid": "3922", "pm": 32, "sub": 1},
        {"type": "led", "model": "AX120_DIGITAL", "name": "AX120 R3",
         "vid": "0416", "pid": "8001"}
    ]

Usage:
    PYTHONPATH=src python3 dev/mock_gui.py
    PYTHONPATH=src python3 dev/mock_gui.py --decorated
    PYTHONPATH=src python3 dev/mock_gui.py --report report.txt   # emulate user's setup
    PYTHONPATH=src python3 dev/mock_gui.py --init                # generate default devices.json
    PYTHONPATH=src python3 dev/mock_gui.py --list                # list resolutions
"""
from __future__ import annotations

import json
import os
import signal
import sys
from pathlib import Path
from typing import Any, cast

os.environ.pop('QT_QPA_PLATFORM', None)  # use real display

# Bootstrap handles sys.path, paths, preflight, MockPlatform install.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _mock_bootstrap import (
    DEV_DATA,
    DEV_TRCC,
    DEVICES_JSON,
    bootstrap,
)


def _parse_args() -> tuple[bool, int, str | None]:
    decorated = False
    verbosity = 0
    report_path: str | None = None
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        arg = args[i]
        if arg.startswith('-v'):
            verbosity = arg.count('v')
        elif arg == '--list':
            from trcc.core.models import FBL_TO_RESOLUTION
            resolutions = sorted(set(FBL_TO_RESOLUTION.values()),
                                 key=lambda r: (r[0] * r[1], r[0]))
            print("Available resolutions:")
            for w, h in resolutions:
                print(f"  {w}x{h}")
            sys.exit(0)
        elif arg == '--init':
            from tests.mock_platform import DEFAULT_DEVICES
            DEVICES_JSON.write_text(json.dumps(list(DEFAULT_DEVICES), indent=2))
            print(f"Created {DEVICES_JSON}")
            sys.exit(0)
        elif arg == '--report':
            i += 1
            if i < len(args):
                report_path = args[i]
            else:
                print("Error: --report requires a file path")
                sys.exit(1)
        elif arg == '--decorated':
            decorated = True
        i += 1
    return decorated, verbosity, report_path


def main() -> None:
    decorated, verbosity, report_path = _parse_args()

    # Bootstrap: paths, preflight, MockPlatform installed in ControllerBuilder.
    platform = bootstrap(report_path)

    # ── Build TrccApp manually (mirrors gui/__init__.py::launch()) ────────
    from trcc.core.app import AppEvent, TrccApp
    from trcc.core.builder import ControllerBuilder

    builder = ControllerBuilder(cast(Any, platform))
    TrccApp.reset()
    app = TrccApp(builder)
    TrccApp._instance = app
    app._ensure_data_fn = builder.build_ensure_data_fn()
    dl_pack, dl_list = builder.build_download_fns()
    app._download_pack_fn = dl_pack
    app._list_available_fn = dl_list

    # ── Qt bootstrap ─────────────────────────────────────────────────────
    from trcc.ui.gui.assets import _PKG_ASSETS_DIR, set_assets_dir
    set_assets_dir(platform.resolve_assets_dir(_PKG_ASSETS_DIR))

    os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.services=false")
    os.environ["QT_ENABLE_HIGHDPI_SCALING"] = "0"

    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication
    qapp = cast(QApplication, QApplication.instance() or QApplication(sys.argv))
    qapp.setQuitOnLastWindowClosed(True)
    qapp.setDesktopFileName("trcc-mock")

    font = QFont("Microsoft YaHei", 10)
    if not font.exactMatch():
        font = QFont("Sans Serif", 10)
    qapp.setFont(font)

    # ── Bootstrap — THE REAL FLOW ────────────────────────────────────────
    from trcc.adapters.render.qt import QtRenderer
    app.init_platform(verbosity=verbosity, renderer_factory=lambda: QtRenderer())
    app.scan()
    app._ensure_data_blocking()

    # ── System service (real sensors from this machine) ──────────────────
    system_svc = app.build_system()
    app.set_system(system_svc)

    from trcc.services.system import set_instance
    set_instance(system_svc)

    # ── GUI — production TRCCApp, injected deps ──────────────────────────
    from trcc.ui.gui.trcc_app import TRCCApp as _TRCCApp
    window = _TRCCApp(
        system_svc=system_svc,
        platform=cast(Any, platform),
        decorated=decorated,
    )

    # ── Register + replay device scan → handlers created ─────────────────
    app.register(cast(Any, window))
    app._notify(AppEvent.DEVICES_CHANGED, list(app._devices.values()))
    app.start_metrics_loop(interval=2)

    # ── Run ──────────────────────────────────────────────────────────────
    signal.signal(signal.SIGINT, lambda *_: qapp.quit())
    window.show()

    print(f"\nConfig: {DEV_TRCC / 'config.json'}")
    print(f"Data:   {DEV_DATA}")
    print(f"Devices: {DEVICES_JSON}")
    print("Close window or Ctrl+C to quit.")

    sys.exit(qapp.exec())


if __name__ == '__main__':
    main()
