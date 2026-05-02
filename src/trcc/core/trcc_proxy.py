"""TrccProxy — drop-in replacement for `Trcc` that routes calls over IPC.

The whole point: a UI that holds a `Trcc | TrccProxy` reference doesn't
need to know which one it has. Same surface, same call shape, same
return types. Composition root picks one at boot::

    trcc = TrccProxy() if TRCC_DAEMON else Trcc.for_current_os()

    # ... every UI call site, unchanged:
    trcc.lcd.set_brightness(0, 75)
    trcc.led.set_color(1, 255, 0, 0)
    trcc.control_center.set_temp_unit('F')

How it works: each facade attribute (``lcd``, ``led``, ``control_center``)
is a thin proxy whose ``__getattr__`` returns a callable. Calling it
serializes the method invocation as the manifold wire format, sends it
to the daemon, parses the response into an `OpResult`. Python's call
shape becomes the wire shape directly — there's no `Command` DTO and
no router translation between them.

Subscribe-side (``trcc.events.subscribe(topic, cb)``) is stubbed in R3
and wired by R5's long-lived IPC connection. In-process subscribers
(GUI in the daemon's own process, CLI play_video on the same Trcc) keep
working because they hold a real `Trcc` and never see the proxy.
"""
from __future__ import annotations

import json
import logging
import socket
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .results import OpResult

if TYPE_CHECKING:
    from collections.abc import Callable

log = logging.getLogger(__name__)


# =============================================================================
# Facade proxies — one per role on Trcc.
# =============================================================================

class _FacadeProxy:
    """Base class: ``__getattr__`` translates method calls into IPC requests.

    The role string ('lcd' / 'led' / 'control_center') tells the daemon
    which facade to dispatch on; the method name + args/kwargs travel
    verbatim through the wire format.
    """

    __slots__ = ('_role', '_socket_path', '_timeout')

    def __init__(self, role: str, socket_path: Path | None,
                 timeout: float) -> None:
        self._role = role
        self._socket_path = socket_path
        self._timeout = timeout

    def __getattr__(self, method: str) -> Callable[..., OpResult]:
        # Private attributes are not facade methods — let normal
        # AttributeError propagate so e.g. pickle / repr don't loop.
        if method.startswith('_'):
            raise AttributeError(method)

        role = self._role
        path = self._socket_path
        timeout = self._timeout

        def call(*args: Any, **kwargs: Any) -> OpResult:
            from ..ipc import _result_from_dict, send_manifold_request
            data = send_manifold_request(
                role, method, args, kwargs,
                socket_path=path, timeout=timeout,
            )
            return _result_from_dict(data)

        call.__name__ = f"{role}.{method}"
        call.__qualname__ = call.__name__
        return call


class LCDFacadeProxy(_FacadeProxy):
    """Proxy for ``Trcc.lcd`` — forwards every method through IPC."""

    def __init__(self, socket_path: Path | None = None,
                 timeout: float = 10.0) -> None:
        super().__init__('lcd', socket_path, timeout)


class LEDFacadeProxy(_FacadeProxy):
    """Proxy for ``Trcc.led``."""

    def __init__(self, socket_path: Path | None = None,
                 timeout: float = 10.0) -> None:
        super().__init__('led', socket_path, timeout)


class ControlCenterFacadeProxy(_FacadeProxy):
    """Proxy for ``Trcc.control_center``."""

    def __init__(self, socket_path: Path | None = None,
                 timeout: float = 10.0) -> None:
        super().__init__('control_center', socket_path, timeout)


# =============================================================================
# EventBusProxy — subscribe over IPC. R5 wires the long-lived connection.
# =============================================================================

class EventBusProxy:
    """Subscribe over IPC to events the daemon's EventBus publishes.

    Each :meth:`subscribe` call opens its own long-lived Unix socket to
    the daemon and spawns a daemon thread that reads JSON event lines
    and dispatches them to the registered callback. :meth:`unsubscribe`
    closes the socket; the reader exits naturally on the next read.

    ``publish`` is intentionally not supported — events flow daemon →
    client only. In-process publishers (the daemon's own facades) call
    `Trcc.events.publish` directly, never through a proxy.
    """

    __slots__ = ('_lock', '_next_id', '_socket_path', '_subs', '_timeout')

    def __init__(self, socket_path: Path | None = None,
                 timeout: float = 10.0) -> None:
        self._socket_path = socket_path
        self._timeout = timeout
        self._lock = threading.Lock()
        self._next_id = 0
        # sub_id → (sock, reader_thread). Reader closes sock on exit;
        # unsubscribe shuts the sock to wake a blocked recv().
        self._subs: dict[int, tuple[socket.socket, threading.Thread]] = {}

    def subscribe(self, event: str, callback: Callable[..., Any]) -> int:
        """Subscribe to ``event``. Returns a local sub_id for unsubscribe.

        Spawns a background daemon thread that reads from the socket
        and calls ``callback(*payload)`` for each event. Errors in the
        callback are logged and don't break the subscription.
        """
        from ..ipc import _socket_path

        path = self._socket_path or _socket_path()
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self._timeout)
        try:
            s.connect(str(path))
            s.sendall(json.dumps({"subscribe": event}).encode() + b"\n")
        except OSError as e:
            try:
                s.close()
            except OSError:
                pass
            raise RuntimeError(
                f"EventBusProxy.subscribe: cannot reach daemon at "
                f"{path}: {e}") from e

        # Read the ack — server confirms the subscription before events flow.
        ack_line = _read_line(s)
        ack = json.loads(ack_line) if ack_line else {}
        if not ack.get("success"):
            try:
                s.close()
            except OSError:
                pass
            err = ack.get("error", "unknown error")
            raise RuntimeError(f"EventBusProxy.subscribe: server rejected: {err}")

        # Drop timeout — events arrive when they arrive.
        s.settimeout(None)

        def _reader() -> None:
            try:
                buf = b""
                while True:
                    chunk = s.recv(65536)
                    if not chunk:
                        return
                    buf += chunk
                    while b"\n" in buf:
                        line, _, buf = buf.partition(b"\n")
                        text = line.decode().strip()
                        if not text:
                            continue
                        try:
                            msg = json.loads(text)
                            payload = tuple(msg.get("payload", ()))
                            callback(*payload)
                        except Exception:
                            log.exception(
                                "EventBusProxy: callback for %r raised",
                                event)
            except OSError:
                pass
            finally:
                try:
                    s.close()
                except OSError:
                    pass

        thread = threading.Thread(
            target=_reader, daemon=True,
            name=f"EventBusProxy-{event}",
        )
        thread.start()

        with self._lock:
            sub_id = self._next_id
            self._next_id += 1
            self._subs[sub_id] = (s, thread)
        log.debug("EventBusProxy: subscribed id=%d event=%r", sub_id, event)
        return sub_id

    def unsubscribe(self, sub_id: int) -> None:
        """Cancel a subscription. Closes the socket so the reader exits."""
        with self._lock:
            sub = self._subs.pop(sub_id, None)
        if sub is None:
            return
        s, _thread = sub
        try:
            s.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            s.close()
        except OSError:
            pass
        log.debug("EventBusProxy: unsubscribed id=%d", sub_id)

    def publish(self, event: str, *payload: Any) -> None:
        raise RuntimeError(
            "TrccProxy clients cannot publish — events flow daemon → client. "
            "Use Trcc.events.publish from inside the daemon process.")


def _read_line(s: socket.socket) -> str:
    """Read a single newline-terminated JSON line from a Unix socket."""
    buf = b""
    while b"\n" not in buf:
        chunk = s.recv(4096)
        if not chunk:
            break
        buf += chunk
    line, _, _ = buf.partition(b"\n")
    return line.decode().strip()


# =============================================================================
# TrccProxy — the public surface. Drop-in for `Trcc`.
# =============================================================================

class TrccProxy:
    """Drop-in replacement for `Trcc` that routes everything over IPC.

    Surface mirrors `Trcc`: ``lcd``, ``led``, ``control_center``,
    ``events``. Each is a proxy that serializes calls into the manifold
    wire format and parses responses back into typed results. UIs hold a
    ``Trcc | TrccProxy`` reference and never need to distinguish — that's
    what makes daemon-mode opt-in by changing one line at the composition
    root rather than rewriting every call site.

    Construction takes the standard socket path and a per-call timeout;
    test code passes a custom path. Construction never blocks on the
    daemon — calls fail-fast with a transport error if the daemon is
    unreachable, so you can build a proxy before the daemon is up and
    have it work as soon as the daemon binds.
    """

    __slots__ = ('control_center', 'events', 'lcd', 'led')

    def __init__(self, *, socket_path: Path | None = None,
                 timeout: float = 10.0) -> None:
        # Build all four facade proxies up front. Cheap (no IPC at
        # construction), and leaves the public surface as plain
        # attribute access — no property-descriptor surprises with
        # __slots__ / __getattr__ interaction.
        self.lcd = LCDFacadeProxy(socket_path, timeout)
        self.led = LEDFacadeProxy(socket_path, timeout)
        self.control_center = ControlCenterFacadeProxy(socket_path, timeout)
        self.events = EventBusProxy(socket_path, timeout)
