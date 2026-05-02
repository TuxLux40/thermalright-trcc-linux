"""CLI composition root — returns a `Trcc` or a `TrccProxy`.

Every CLI subcommand calls ``trcc()`` once and exits. What it gets back
depends on whether daemon mode is enabled:

  ``TRCC_DAEMON`` unset (default)
      A real `Trcc` — built once per process, cached, scans for devices
      directly. Identical to today's behaviour.

  ``TRCC_DAEMON=1`` (and AF_UNIX available)
      A `TrccProxy` connected to the running daemon, auto-spawning one
      via :func:`trcc.daemon.ensure_daemon` if not already up. Same
      surface as `Trcc`, so call sites are unchanged::

          trcc().lcd.set_brightness(0, 75)   # works identically either way

Falls back to in-process mode automatically when AF_UNIX is unavailable
(Windows builds older than 17063), so the flag is safe to leave set on
every OS — daemon mode just no-ops where the transport doesn't exist.

Test / dev injection still works: pass a `Platform` explicitly to
build a fresh in-process `Trcc` against a `MockPlatform`.
"""
from __future__ import annotations

import os
import socket
from typing import TYPE_CHECKING, cast

from trcc.core.trcc import Trcc

if TYPE_CHECKING:
    from trcc.core.ports import Platform

# Cached per-process Trcc — built on first call. In daemon mode this is
# actually a `TrccProxy`, but it's a structural drop-in for `Trcc` so
# call sites are statically typed against the same surface either way.
_cached: Trcc | None = None

# Env flag values that count as "on". Mirrors the rest of the codebase.
_TRUTHY_FLAG = frozenset({'1', 'true', 'yes', 'on'})


def _daemon_mode_enabled() -> bool:
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

    Caches the first result. Passing ``platform`` forces an in-process
    `Trcc` build against that platform, ignoring the daemon flag — used
    by tests and ``dev/mock_cli.py`` to inject a `MockPlatform`.

    Composition pattern (unchanged from before)::

        from trcc.ui.cli._boot import trcc
        result = trcc().lcd.set_brightness(0, 50)
        typer.echo(result.format())
        return result.exit_code
    """
    global _cached
    if _cached is not None:
        return _cached

    # Daemon mode — auto-spawn the daemon if not running, return a proxy.
    # Skipped when a Platform is injected (tests / mock_cli always run
    # in-process so they can supply their own fake hardware).
    if platform is None and _daemon_mode_enabled():
        from trcc.core.trcc_proxy import TrccProxy
        from trcc.daemon import ensure_daemon
        if not ensure_daemon():
            raise RuntimeError(
                "TRCC_DAEMON=1 but no daemon is reachable. "
                "Start one explicitly with `trcc daemon`.")
        # TrccProxy is a structural drop-in for Trcc — same surface
        # (.lcd, .led, .control_center, .events) so call sites are
        # statically typed against Trcc and just work at runtime.
        _cached = cast(Trcc, TrccProxy())
        return _cached

    # Production in-process path: TrccApp.init() ran first via
    # cli/__init__.py::main(). Reuse its composed Trcc so callers see
    # the same connected devices — single source of truth.
    from trcc.core.app import TrccApp
    if TrccApp._instance is not None and platform is None:
        inner = TrccApp._instance._trcc
        if not inner:
            TrccApp._instance.scan()
        _cached = inner
        return _cached

    # Standalone (`python -m trcc.daemon` or test path with explicit platform)
    from trcc.core.builder import ControllerBuilder
    from trcc.services.system import set_instance
    from trcc.ui.cli import _make_cli_renderer

    app = Trcc(platform) if platform is not None else Trcc.for_current_os()
    app.bootstrap()
    app.with_renderer(_make_cli_renderer())
    app.discover()

    sys_svc = ControllerBuilder(app._platform).build_system()
    set_instance(sys_svc)
    metrics = sys_svc.all_metrics
    for device in app:
        device.update_metrics(metrics)

    _cached = app
    return _cached
