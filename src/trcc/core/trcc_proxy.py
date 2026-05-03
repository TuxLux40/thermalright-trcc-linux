"""TrccProxy — drop-in replacement for `Trcc` that routes calls over IPC.

The whole point: a UI that holds a `Trcc | TrccProxy` reference doesn't
need to know which one it has. Same surface, same call shape, same
return types. Composition root picks one at boot::

    trcc = TrccProxy() if TRCC_DAEMON else _boot.trcc()

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

Subscribe-side (``trcc.events.subscribe(topic, cb)``) opens its own
long-lived socket per subscription; a daemon thread reads JSON event
lines and dispatches to the registered callback. ``unsubscribe`` shuts
the socket so the reader exits naturally. In-process subscribers (GUI
in the daemon's own process, CLI ``play_video`` on the same Trcc) keep
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
            # Sanitize Path/bytes at the wire boundary so JSON can
            # serialize them. The server unsanitizes on its end before
            # dispatching to the real facade method.
            from ..ipc import (
                _result_from_dict,
                sanitize_for_wire,
                send_manifold_request,
            )
            data = send_manifold_request(
                role, method,
                tuple(sanitize_for_wire(a) for a in args),
                {k: sanitize_for_wire(v) for k, v in kwargs.items()},
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
        from ..ipc import open_and_send, read_json_line

        try:
            s = open_and_send({"subscribe": event},
                              socket_path=self._socket_path,
                              timeout=self._timeout)
        except OSError as e:
            raise RuntimeError(
                f"EventBusProxy.subscribe: cannot reach daemon: {e}") from e

        # Read the ack — server confirms the subscription before events flow.
        try:
            ack = read_json_line(s)
        except (OSError, json.JSONDecodeError) as e:
            try:
                s.close()
            except OSError:
                pass
            raise RuntimeError(
                f"EventBusProxy.subscribe: malformed ack: {e}") from e
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
        self._close_sub(sub)
        log.debug("EventBusProxy: unsubscribed id=%d", sub_id)

    def publish(self, event: str, *payload: Any) -> None:
        raise RuntimeError(
            "TrccProxy clients cannot publish — events flow daemon → client. "
            "Use Trcc.events.publish from inside the daemon process.")

    def cleanup(self) -> None:
        """Close every open subscription. Idempotent.

        Called from :meth:`TrccProxy.cleanup` so a single ``trcc._boot.cleanup()``
        tears down both the proxy itself and its subscription threads.
        """
        with self._lock:
            subs = list(self._subs.values())
            self._subs.clear()
        for sub in subs:
            self._close_sub(sub)

    @staticmethod
    def _close_sub(sub: tuple[socket.socket, threading.Thread]) -> None:
        """Shut + close a subscription's socket; reader thread exits naturally."""
        s, _thread = sub
        try:
            s.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            s.close()
        except OSError:
            pass


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

    __slots__ = ('_socket_path', '_timeout', 'control_center', 'events', 'lcd', 'led')

    def __init__(self, *, socket_path: Path | None = None,
                 timeout: float = 10.0) -> None:
        # Build all four facade proxies up front. Cheap (no IPC at
        # construction), and leaves the public surface as plain
        # attribute access — no property-descriptor surprises with
        # __slots__ / __getattr__ interaction.
        self._socket_path = socket_path
        self._timeout = timeout
        self.lcd = LCDFacadeProxy(socket_path, timeout)
        self.led = LEDFacadeProxy(socket_path, timeout)
        self.control_center = ControlCenterFacadeProxy(socket_path, timeout)
        self.events = EventBusProxy(socket_path, timeout)

    # ── Trcc-level methods (proxied through the `_meta` role) ───────────────

    def discover(self) -> Any:
        """Trigger a device rescan on the daemon, return the result.

        Mirrors ``Trcc.discover()``'s contract: the daemon does the
        actual USB enumeration; we return a `DiscoveryResult`-shaped
        OpResult. Subclass extras (lcd_devices, led_devices) come back
        empty for now since `DeviceInfo` isn't JSON-serializable on the
        wire — extend if/when a caller needs the device descriptors.
        """
        from ..ipc import send_manifold_request
        from .results import DiscoveryResult
        response = send_manifold_request(
            "_meta", "discover", (), {},
            socket_path=self._socket_path, timeout=self._timeout,
        )
        return DiscoveryResult(
            success=bool(response.get("success", False)),
            message=str(response.get("message", "")),
            error=response.get("error"),
        )

    def cleanup(self) -> None:
        """Release proxy-local resources.

        Closes every open event subscription socket. Does NOT touch the
        daemon's own state — daemon devices stay alive for other clients.
        Idempotent; safe to call multiple times.
        """
        try:
            self.events.cleanup()
        except Exception:
            log.exception("TrccProxy.cleanup: events.cleanup raised")

    # ── Lifecycle stubs — daemon owns these, clients can't ─────────────────
    #
    # Raise loudly rather than silently accept — callers in daemon mode
    # should NOT be trying to bootstrap / register devices / swap renderers
    # from the client side. The daemon already did all of that.

    def bootstrap(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError(
            "TrccProxy.bootstrap is not supported — the daemon owns "
            "lifecycle. The daemon has already bootstrapped its Trcc.")

    def with_renderer(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(
            "TrccProxy.with_renderer is not supported — the daemon "
            "manages its own renderer.")

    def register_lcd(self, *args: Any, **kwargs: Any) -> int:
        raise RuntimeError(
            "TrccProxy.register_lcd is not supported — devices are "
            "discovered and registered on the daemon side.")

    def register_led(self, *args: Any, **kwargs: Any) -> int:
        raise RuntimeError(
            "TrccProxy.register_led is not supported — devices are "
            "discovered and registered on the daemon side.")
