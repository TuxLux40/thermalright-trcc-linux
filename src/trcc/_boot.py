"""TRCC composition root — canonical Trcc factory.

One module, used uniformly by CLI / API / GUI. Every UI calls
:func:`trcc` to obtain its per-process Trcc handle and gets the same
answer based on environment + state. Honors ``TRCC_DAEMON`` to switch
between in-process and daemon-proxy modes — every UI does the right
thing automatically.

Composition pattern (the only one in this codebase)::

    from trcc._boot import trcc
    result = trcc().lcd.set_brightness(0, 50)
    typer.echo(result.format())          # CLI
    return _to_response(result)          # API
    self._status_label.setText(...)      # GUI

Modes:

  ``TRCC_DAEMON`` unset (default)
      Returns the in-process `Trcc` — single instance per process,
      cached. Reuses ``TrccApp._instance._trcc`` when a composition
      root has already initialised one (production CLI / GUI). Builds
      a fresh ``Trcc`` with offscreen ``QtRenderer`` otherwise (tests,
      ``python -m trcc.daemon``).

  ``TRCC_DAEMON=1`` (and AF_UNIX available)
      Returns a `TrccProxy` connected to a running daemon, auto-spawning
      one via :func:`trcc.daemon.ensure_daemon` if necessary. The proxy
      is a structural drop-in for `Trcc` — call sites are statically
      typed against the same surface either way.

  Windows < build 17063
      ``AF_UNIX`` is unavailable; daemon mode silently falls back to
      in-process. The flag is safe to set on any OS.

Test / dev injection still works: pass an explicit `Platform` (e.g.
``MockPlatform``) to force in-process mode and bypass the daemon-flag
short-circuit::

    from trcc._boot import trcc
    from tests.mock_platform import MockPlatform
    trcc(MockPlatform(specs))   # subsequent trcc() calls return this
"""
from __future__ import annotations

import logging
import os
import socket
import threading
from typing import TYPE_CHECKING, cast

from trcc.core.trcc import Trcc

if TYPE_CHECKING:
    from trcc.core.ports import Platform

log = logging.getLogger(__name__)

# Cached per-process Trcc — built on first call. In daemon mode this
# is actually a `TrccProxy`, but it satisfies the `Trcc` contract via
# structural typing so call sites never need to distinguish.
_cached: Trcc | None = None
_lock = threading.Lock()

# Strong reference to the offscreen QApplication we may create. Without
# this, PySide6 collects the Python wrapper between calls and the next
# QtRenderer construction segfaults. Module-level ref keeps it alive for
# the process lifetime.
_qapp_strong_ref: object | None = None

# Env flag values that count as "on". Mirrors usage elsewhere in the
# codebase (CLI flags, conf parsing).
_TRUTHY_FLAG = frozenset({'1', 'true', 'yes', 'on'})


def daemon_mode_enabled() -> bool:
    """True when ``TRCC_DAEMON`` is set truthy AND the platform supports it.

    Returns False on Windows builds older than 17063 where ``AF_UNIX``
    is unavailable — the user gets in-process mode silently rather than
    a hard error, so the flag is safe to set on every OS.
    """
    if os.environ.get('TRCC_DAEMON', '').lower() not in _TRUTHY_FLAG:
        return False
    return hasattr(socket, 'AF_UNIX')


def trcc(platform: Platform | None = None) -> Trcc:
    """Return the per-process `Trcc` (or `TrccProxy` in daemon mode).

    Cached, thread-safe. Identical contract for every UI — they all
    see the same handle, no UI-specific composition logic anywhere.

    Pass ``platform`` once (typically from a test harness or
    ``dev/mock_*`` script) to force an in-process build against a
    specific Platform — daemon mode short-circuit is bypassed in that
    case so mock devices stay in this process.
    """
    global _cached
    with _lock:
        if _cached is not None:
            return _cached

        # 1. Daemon mode short-circuit — proxy, no Qt setup needed
        # (the daemon process already has its own).
        if platform is None and daemon_mode_enabled():
            from trcc.core.trcc_proxy import TrccProxy
            from trcc.daemon import ensure_daemon
            if not ensure_daemon():
                raise RuntimeError(
                    "TRCC_DAEMON=1 but no daemon is reachable. "
                    "Start one explicitly with `trcc daemon`.")
            # TrccProxy is a structural drop-in for Trcc — same .lcd /
            # .led / .control_center / .events surface so call sites
            # stay typed against Trcc and just work at runtime.
            _cached = cast(Trcc, TrccProxy())
            return _cached

        # 2. In-process production path: a composition root (cli/main,
        # gui/launch) already ran TrccApp.init() and has a composed
        # inner Trcc. Reuse it so callers share the same connected
        # devices — single source of truth.
        from trcc.core.app import TrccApp
        if TrccApp._instance is not None and platform is None:
            inner = TrccApp._instance._trcc
            if not inner:
                TrccApp._instance.scan()
            _cached = inner
            return _cached

        # 3. Standalone — build a fresh Trcc with an offscreen
        # QApplication so QtRenderer can do its image work. Used by
        # tests, dev/mock_*, and `python -m trcc.daemon`.
        _ensure_qapp()
        from trcc.adapters.render.qt import QtRenderer
        from trcc.core.builder import ControllerBuilder
        from trcc.services.system import set_instance

        app = Trcc(platform) if platform is not None else Trcc.for_current_os()
        app.bootstrap()
        app.with_renderer(QtRenderer())
        app.discover()

        # Seed each connected device with one fresh metrics snapshot so
        # sensor-linked LED modes (temp_linked, load_linked) and overlay
        # sensors render real values when this is the only UI running
        # on a headless box (issue #130).
        sys_svc = ControllerBuilder(app._platform).build_system()
        set_instance(sys_svc)
        metrics = sys_svc.all_metrics
        for device in app:
            device.update_metrics(metrics)

        _cached = app
        return _cached


def cleanup() -> None:
    """Release the cached Trcc. Idempotent + thread-safe.

    For an in-process `Trcc`: releases device handles, clears event
    subscribers. For a `TrccProxy`: closes long-lived event subscription
    sockets. Either way, the next :func:`trcc` call rebuilds.
    """
    global _cached
    with _lock:
        if _cached is None:
            return
        cleanup_fn = getattr(_cached, 'cleanup', None)
        if callable(cleanup_fn):
            try:
                cleanup_fn()
            except Exception:
                log.exception("trcc cleanup failed")
        _cached = None


def _ensure_qapp() -> None:
    """Make sure SOME QApplication is alive for QtRenderer to use.

    No-op if one already exists (e.g. the GUI created a display app
    earlier in the process). Otherwise creates an offscreen
    QApplication — sufficient for the renderer's image work without
    requiring a graphical session.
    """
    global _qapp_strong_ref
    from PySide6.QtWidgets import QApplication
    if QApplication.instance() is not None:
        return
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    _qapp_strong_ref = QApplication([])
    log.debug("created offscreen QApplication for QtRenderer")
