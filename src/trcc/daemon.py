"""trccd — the background process that owns USB and serves UIs.

One process per user. Built once, lives until SIGTERM/SIGINT or the
user runs ``trcc daemon stop``. Holds the singleton `Trcc`, discovers
devices, runs the metrics loop (Phase 9 wires that in), and serves
`Command` requests over the Unix socket through `IPCServer`.

Singleton enforcement is socket-presence: the first invocation that
finds an empty socket path becomes the daemon; later invocations see
the live socket and become clients.

Lifecycle::

    1. Probe the socket — refuse to start if a daemon is already running.
    2. Build offscreen QApplication so QSocketNotifier has an event loop.
    3. Build Trcc (renderer, bootstrap, discover).
    4. Bind IPCServer to the Trcc and start listening.
    5. Install SIGTERM / SIGINT handlers that shut the server cleanly.
    6. Run the Qt event loop until the signal handlers ask us to quit.

The daemon is opt-in today (Phase 5–8 sit behind ``TRCC_DAEMON=1``).
Phase 12 flips the default and Phase 11 adds OS-level service files
so it can survive reboots.
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from typing import Any

log = logging.getLogger(__name__)


# =============================================================================
# Daemon entry point
# =============================================================================

def run_daemon(*, verbosity: int = 0) -> int:
    """Run the daemon. Blocks until shutdown. Returns a process exit code."""
    from .adapters.infra.diagnostics import StandardLoggingConfigurator
    from .ipc import IPCServer, daemon_running

    # Singleton — bail out early with a clear message if another daemon
    # is already serving on the socket.
    if daemon_running():
        log.warning("trccd: another daemon is already running on the socket; "
                    "refusing to start a second one.")
        return 1

    # Logging — same configurator the GUI uses, but written to ~/.trcc/trcc.log
    # so the daemon's output is visible to support requests.
    StandardLoggingConfigurator().configure(verbosity=verbosity)

    log.info("trccd starting (pid=%d)", os.getpid())

    qapp = _build_qapp()
    trcc = _build_trcc()

    server = IPCServer(trcc=trcc)
    server.start()

    _install_signal_handlers(qapp, server)

    log.info("trccd ready — listening on IPC socket")
    return qapp.exec()


# =============================================================================
# Auto-spawn helper for clients (CLI / GUI / API when running standalone)
# =============================================================================

def ensure_daemon(*, timeout: float = 10.0) -> bool:
    """Ensure the daemon is running; spawn one in the background if not.

    Returns True when a daemon socket is reachable, False if the spawn
    didn't come up within ``timeout`` seconds. Idempotent — calling when
    a daemon is already running is a fast no-op.

    The spawned daemon runs in its own session (``start_new_session``),
    so it survives the calling process and stdin/stdout are detached.
    """
    from .ipc import daemon_running

    if daemon_running():
        return True

    cmd = _daemon_spawn_cmd()
    log.info("Spawning daemon: %s", " ".join(cmd))
    # argv is built from sys.executable + literals — safe.
    subprocess.Popen(
        cmd,
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        close_fds=True,
    )

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if daemon_running():
            log.info("Daemon ready (waited %.2fs)",
                     time.monotonic() - (deadline - timeout))
            return True
        time.sleep(0.05)

    log.warning("Daemon did not come up within %.1fs", timeout)
    return False


# =============================================================================
# kill — graceful shutdown of a running daemon from any client.
# =============================================================================

def kill_daemon(*, timeout: float = 5.0) -> bool:
    """Send a kill request to the running daemon, wait for it to exit.

    The daemon acks the request immediately and tears down via a
    single-shot Qt timer so the ack flushes back before the event loop
    quits. We poll the socket until it disappears (or hit the timeout).

    Returns True when the daemon is no longer reachable, False on timeout
    or if the kill request couldn't be delivered. Idempotent — calling
    when no daemon is running is a fast no-op that returns True.
    """
    import json
    import socket as _socket

    from .ipc import _socket_path, daemon_running

    if not daemon_running():
        return True

    if not hasattr(_socket, 'AF_UNIX'):
        return False

    # Direct socket round-trip with the {"kill": true} shape. Bypasses
    # send_manifold_request because the kill payload doesn't fit the
    # manifold role/method format.
    s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    s.settimeout(2.0)
    try:
        s.connect(str(_socket_path()))
        s.sendall(json.dumps({"kill": True}).encode() + b"\n")
        s.recv(4096)  # drain the ack so the daemon's buffer flushes
    except OSError as e:
        log.warning("kill_daemon: %s", e)
        return False
    finally:
        try:
            s.close()
        except OSError:
            pass

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not daemon_running():
            return True
        time.sleep(0.05)
    return False


# =============================================================================
# Internals
# =============================================================================

def _build_qapp() -> Any:
    """Build (or reuse) an offscreen QApplication for the daemon process."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    # Headless DPI hint to keep PySide6 quiet.
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "0")
    from PySide6.QtWidgets import QApplication
    existing = QApplication.instance()
    qapp = existing if isinstance(existing, QApplication) \
        else QApplication([sys.argv[0]])
    qapp.setQuitOnLastWindowClosed(False)
    return qapp


def _build_trcc() -> Any:
    """Get the Trcc to serve from.

    Two arrival paths converge here:

      ``trcc daemon`` (Typer subcommand)
          ``cli/__init__.py::main`` has already initialised ``TrccApp``
          and bootstrapped the platform.  Reuse TrccApp's composed
          ``Trcc`` so we don't end up with two parallel container
          instances in the same process.

      ``python -m trcc.daemon`` (ad-hoc)
          Nothing was initialised.  Build a fresh ``Trcc`` here and
          discover devices ourselves.

    Phase 9 dissolves ``TrccApp`` into ``Trcc`` and the first branch
    becomes the only one.
    """
    from .adapters.render.qt import QtRenderer
    from .core.app import TrccApp
    from .core.trcc import Trcc

    if TrccApp._instance is not None:
        inner = TrccApp._instance._trcc
        if not list(TrccApp._instance._devices):
            try:
                TrccApp._instance.scan()
            except Exception:
                log.exception("trccd: TrccApp.scan raised — continuing")
        return inner

    trcc = Trcc.for_current_os()
    trcc.bootstrap()
    trcc.with_renderer(QtRenderer())   # QApplication is already alive (_build_qapp)
    try:
        trcc.discover()
    except Exception:
        log.exception("trccd: device discovery raised — continuing with no devices")
    return trcc


# Module-level holder so the Qt heartbeat timer survives past the function
# that creates it (Qt drops QObject children without a Python ref).
_HEARTBEAT_TIMER: Any = None


def _install_signal_handlers(qapp: Any, server: Any) -> None:
    """SIGTERM / SIGINT shut the server cleanly and break the event loop.

    Qt's C++ event loop yields to Python only between Python opcodes; on
    a fully-idle daemon the next opcode may be many seconds away, leaving
    a registered ``signal.signal`` handler unprocessed. A 100 ms QTimer
    that does nothing is the standard, reliable fix — every tick
    transitions through Python, which is when CPython runs pending
    signal handlers. Cheap (no real work, no I/O) and robust across
    every Qt platform plugin.
    """
    global _HEARTBEAT_TIMER
    from PySide6.QtCore import QTimer

    def _shutdown(signo: int, _frame: Any) -> None:
        name = signal.Signals(signo).name
        log.info("trccd: received %s — shutting down", name)
        try:
            server.shutdown()
        except Exception:
            log.exception("trccd: server.shutdown raised")
        qapp.quit()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    timer = QTimer()
    timer.setInterval(100)
    timer.timeout.connect(lambda: None)
    timer.start()
    _HEARTBEAT_TIMER = timer  # keep alive past this function


def _daemon_spawn_cmd() -> list[str]:
    """Return the argv that re-invokes this Python interpreter as the daemon.

    Honours the on-disk install: if ``trcc`` is on PATH, prefer that so
    the daemon picks up the user's installed entry point. Otherwise fall
    back to ``python -m trcc daemon``.
    """
    from shutil import which
    if (trcc_bin := which("trcc")) is not None:
        return [trcc_bin, "daemon"]
    return [sys.executable, "-m", "trcc", "daemon"]


# =============================================================================
# CLI shim — for `python -m trcc.daemon`
# =============================================================================

def main() -> int:
    """Allow ``python -m trcc.daemon`` for ad-hoc invocation."""
    return run_daemon()


if __name__ == "__main__":
    raise SystemExit(main())
