"""IPC server/client and proxies for single-device-owner pattern.

When another trcc instance owns the device, callers route through it
instead of touching USB directly. Two transport types:

  - IPCTransport — Unix domain socket to GUI / daemon
  - APITransport — HTTP to ``trcc serve``

Detection: ``core.instance.find_active()`` checks GUI socket, then API
health endpoint, returns InstanceKind or None.

Protocol — two wire formats coexist during the daemon migration:

  Legacy (single-device mode used by the GUI today)::

      Request:  {"cmd": "device.send_color", "args": [255, 0, 0], "kwargs": {}}
      Response: {"success": true, "message": "..."}

  Manifold (multi-device, used by the daemon and `IpcDispatcher`)::

      Request:  {"role": "lcd", "method": "send_color", "index": 1,
                 "kwargs": {"r": 255, "g": 0, "b": 0}}
      Response: {"success": true, "message": "..."}

The server detects which format is in use by looking at the request
keys, dispatches accordingly, and returns the same response shape.
The legacy format is kept working through the cutover so existing GUI
clients aren't broken; new clients (`IpcDispatcher`) speak the manifold
format and address devices by index.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import socket
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QBuffer, QByteArray, QIODevice

from .core.results import Frame, OpResult

if TYPE_CHECKING:
    from .core.trcc import Trcc

log = logging.getLogger(__name__)

# Socket path: same dir as the instance lock file
_SOCK_NAME = "trcc-linux.sock"


def _socket_path() -> Path:
    return Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / _SOCK_NAME


# Non-serializable keys to strip from dispatcher results (QImage, etc.)
_NON_SERIALIZABLE = frozenset({"image", "colors"})

# Unified whitelist — all Device methods callable via IPC.
# Device dispatches internally based on is_lcd / is_led.
_DEVICE_METHODS = frozenset({
    # LCD
    "send_image", "send_color", "reset",
    "set_brightness", "set_rotation", "set_split_mode",
    "load_theme_by_name", "load_mask_standalone",
    # LED
    "set_color", "set_mode", "off",
    "set_sensor_source",
    "set_zone_color", "set_zone_mode", "set_zone_brightness",
    "toggle_zone", "set_zone_sync",
    "toggle_segment", "set_clock_format", "set_temp_unit",
})

# Legacy prefix mapping — backward compat for one release cycle.
# Old clients send "display.X" or "led.X", new protocol uses "device.X".
_LEGACY_PREFIX = {"display", "led"}


def _sanitize(result: dict) -> dict:
    """Remove non-JSON-serializable keys from dispatcher result."""
    return {k: v for k, v in result.items() if k not in _NON_SERIALIZABLE}


def _result_to_dict(result: OpResult) -> dict[str, Any]:
    """Serialize an OpResult (or subclass) into a JSON-safe dict.

    `Frame` and any other non-JSON value is dropped — the wire format only
    carries the success / error envelope plus simple extras (is_animated,
    interval_ms, latest_version, etc.). UIs that need pixels subscribe to
    the EventBus stream instead of pulling them out of dispatch results.
    """
    out: dict[str, Any] = {
        "success": result.success,
        "message": result.message,
        "error": result.error,
    }
    for f in dataclasses.fields(result):
        if f.name in {"success", "message", "error"}:
            continue
        v = getattr(result, f.name)
        if isinstance(v, Frame) or v is None:
            continue
        try:
            json.dumps(v)
        except (TypeError, ValueError):
            continue
        out[f.name] = v
    return out


def _result_from_dict(data: dict[str, Any]) -> OpResult:
    """Build a generic OpResult from a wire-format dict.

    Subclass-specific extras are dropped at this boundary — the IPC
    contract is OpResult shape (success/message/error). Phase 4 keeps it
    minimal; richer typed results travel via the EventBus stream that
    Phase 4b wires up alongside this dispatcher.
    """
    return OpResult(
        success=bool(data.get("success", False)),
        message=str(data.get("message", "")),
        error=data.get("error"),
    )


# =========================================================================
# Server (runs in the GUI process, Qt event loop)
# =========================================================================

class IPCServer:
    """Unix socket IPC server — listens for CLI requests, routes to device.

    Integrates with Qt event loop via QSocketNotifier on the listening fd.
    Each client is handled synchronously (accept -> read -> dispatch -> respond
    -> close) in a single callback, which is safe because requests are small
    and local.
    """

    def __init__(self, device: Any = None, *, trcc: Trcc | None = None):
        # Two operating modes coexist during the daemon cutover:
        #   - device-bound: legacy single-device mode used by the GUI today
        #   - trcc-bound:   multi-device manifold used by the daemon
        # `bind_trcc` flips on the manifold dispatch path; legacy
        # `device` setter still works so existing GUI code is undisturbed.
        self._device = device
        self._trcc: Trcc | None = trcc
        self._sock: socket.socket | None = None
        self._notifier: Any = None  # QSocketNotifier
        self._current_frame: Any = None  # Last frame sent to LCD (QImage)
        # Long-lived event subscriber connections — each is (sub_id, sock).
        # Tracked so shutdown() can unsubscribe + close them cleanly.
        self._event_subs: list[tuple[int, socket.socket]] = []

    @property
    def device(self) -> Any:
        return self._device

    @device.setter
    def device(self, value: Any) -> None:
        self._device = value

    # Backward-compat aliases — GUI code may still set these
    @property
    def display(self) -> Any:
        return self._device

    @display.setter
    def display(self, value: Any) -> None:
        self._device = value

    @property
    def led(self) -> Any:
        return self._device

    @led.setter
    def led(self, value: Any) -> None:
        self._device = value

    def capture_frame(self, image: Any) -> None:
        """Store the latest frame sent to LCD (called by on_frame_sent callback)."""
        self._current_frame = image

    def bind_trcc(self, trcc: Trcc) -> None:
        """Attach a `Trcc` so manifold-format requests can dispatch.

        Idempotent — call again with a new Trcc to refresh the wiring.
        Legacy ``cmd: "device.X"`` requests keep working against
        ``self._device``; clients using `TrccProxy` send the manifold
        format and reach the bound Trcc directly.
        """
        self._trcc = trcc
        log.info("IPC server bound to Trcc (manifold mode active)")

    def start(self) -> None:
        """Bind and listen on Unix domain socket (Unix only)."""
        if not hasattr(socket, 'AF_UNIX'):
            log.debug("IPC server skipped -- AF_UNIX not available (Windows)")
            return

        path = _socket_path()
        if path.exists():
            path.unlink()

        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.setblocking(False)
        self._sock.bind(str(path))
        self._sock.listen(5)
        os.chmod(str(path), 0o600)

        from PySide6.QtCore import QSocketNotifier
        self._notifier = QSocketNotifier(
            self._sock.fileno(), QSocketNotifier.Type.Read)
        self._notifier.activated.connect(self._on_connection)
        log.info("IPC server listening on %s", path)

    def shutdown(self) -> None:
        """Close socket and clean up."""
        # Unsubscribe + close every long-lived event subscriber.
        if self._trcc is not None:
            for sub_id, _client in self._event_subs:
                try:
                    self._trcc.events.unsubscribe(sub_id)
                except Exception:
                    log.exception("shutdown: events.unsubscribe(%d) raised", sub_id)
        for _sub_id, client in self._event_subs:
            try:
                client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                client.close()
            except OSError:
                pass
        self._event_subs.clear()

        if self._notifier:
            self._notifier.setEnabled(False)
            self._notifier = None
        if self._sock:
            self._sock.close()
            self._sock = None
        path = _socket_path()
        if path.exists():
            path.unlink()
        log.info("IPC server shut down")

    def _on_connection(self) -> None:
        """Accept client, classify request, dispatch.

        Two modes: one-shot dispatch (close after responding) and
        long-lived subscription (keep open, write events back until
        the client disconnects).
        """
        if not self._sock:
            return
        try:
            client, _ = self._sock.accept()
        except OSError:
            return

        request: dict = {}
        is_subscribe = False
        try:
            client.settimeout(5.0)
            if not (data := client.recv(65536)):
                return

            parsed = json.loads(data.decode().strip())
            if not isinstance(parsed, dict):
                _send_error(client, "Request must be a JSON object")
                return
            request = parsed

            # Long-lived subscription connection — keep socket open.
            if "subscribe" in request:
                is_subscribe = True
                self._handle_subscribe(client, str(request["subscribe"]))
                return

            # One-shot dispatch — respond and close.
            result = self._dispatch(request)
            client.sendall(json.dumps(result).encode() + b"\n")
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            _send_error(client, f"Bad request: {e}")
        except Exception as e:
            log.warning("IPC dispatch error: %s", e)
            _send_error(client, str(e))
        finally:
            # Subscription connections stay open; one-shot connections close.
            if not is_subscribe:
                try:
                    client.close()
                except OSError:
                    pass

    def _handle_subscribe(self, client: socket.socket, topic: str) -> None:
        """Register a subscription that forwards EventBus events to ``client``.

        Sends a one-line ack ({success}) then keeps the socket open. Each
        publish on ``topic`` writes a JSON line to the client. Write
        failures (client disconnected) trigger automatic cleanup.
        """
        if self._trcc is None:
            try:
                client.sendall(json.dumps({
                    "success": False,
                    "error": "IPC server not bound to a Trcc.",
                }).encode() + b"\n")
            finally:
                try:
                    client.close()
                except OSError:
                    pass
            return

        # Ack the subscription before any events flow.
        try:
            client.sendall(json.dumps({"success": True}).encode() + b"\n")
        except OSError:
            try:
                client.close()
            except OSError:
                pass
            return
        # Drop the recv timeout — the connection now lives until the
        # client disconnects.
        client.settimeout(None)

        # Forwarder: serializes the EventBus payload and writes to the
        # client. On write failure, unsubscribes itself.
        sub_id_holder: list[int] = []

        def _forward(*payload: Any) -> None:
            line = json.dumps({"topic": topic, "payload": list(payload)}) + "\n"
            try:
                client.sendall(line.encode())
            except OSError:
                # Client gone — unsubscribe and remove from tracking.
                if not sub_id_holder:
                    return
                sid = sub_id_holder[0]
                try:
                    if self._trcc is not None:
                        self._trcc.events.unsubscribe(sid)
                except Exception:
                    log.exception("subscribe forwarder: unsubscribe raised")
                self._event_subs[:] = [
                    (i, c) for (i, c) in self._event_subs if i != sid
                ]
                try:
                    client.close()
                except OSError:
                    pass

        sub_id = self._trcc.events.subscribe(topic, _forward)
        sub_id_holder.append(sub_id)
        self._event_subs.append((sub_id, client))
        log.info("IPC subscription registered: id=%d topic=%r", sub_id, topic)

    def _dispatch(self, request: dict) -> dict:
        """Route request to the matching dispatcher.

        Three wire formats coexist:
          - ``{"kill": true}``      — daemon-control, ack and self-shutdown
          - ``{"role": ...}``       — manifold (multi-device by index)
          - ``{"cmd": "device.X"}`` — legacy single-device, kept for one
                                      release while clients migrate
        """
        if request.get("kill"):
            return self._handle_kill()
        if "role" in request:
            return self._dispatch_manifold(request)

        cmd = request.get("cmd", "")
        args = request.get("args", [])
        kwargs = request.get("kwargs", {})

        match cmd.split(".", 1):
            case ["status"]:
                return self._status()
            case [prefix, method] if prefix in ("device", *_LEGACY_PREFIX):
                return self._dispatch_device(method, cmd, args, kwargs)
            case _:
                return {"success": False, "error": f"Invalid command: {cmd}"}

    def _handle_kill(self) -> dict:
        """Acknowledge a kill request, then schedule a clean shutdown.

        Shutdown is deferred via a single-shot timer so the ack flushes
        back to the client before the Qt event loop tears down. The
        client's send_manifold_request returns the ack; the client can
        then poll daemon_running() to confirm the daemon is gone.
        """
        try:
            from PySide6.QtCore import QTimer
            QTimer.singleShot(50, self._kill_now)
        except Exception:
            log.exception("kill: failed to schedule shutdown — falling through")
            self._kill_now()
        return {"success": True, "message": "Daemon shutting down"}

    def _kill_now(self) -> None:
        """Tear down the IPC server and quit the daemon's Qt event loop."""
        try:
            self.shutdown()
        except Exception:
            log.exception("_kill_now: server.shutdown raised")
        try:
            from PySide6.QtWidgets import QApplication
            qapp = QApplication.instance()
            if qapp is not None:
                qapp.quit()
        except Exception:
            log.exception("_kill_now: qapp.quit raised")

    def _dispatch_manifold(self, request: dict) -> dict:
        """Manifold format: route by (role, method) on the bound Trcc.

        Wire format::

            {"role": "lcd", "method": "set_brightness",
             "args": [0, 75], "kwargs": {}}

        The role names a facade on the Trcc (``lcd`` / ``led`` /
        ``control_center``); the method is invoked on it as
        ``fn(*args, **kwargs)``.  No translation layer — the wire shape
        mirrors the Python call shape exactly, which is what makes
        `TrccProxy` a transparent drop-in replacement for `Trcc`.
        """
        if self._trcc is None:
            return {
                "success": False,
                "error": "IPC server not bound to a Trcc — manifold "
                         "dispatch unavailable on this instance.",
            }

        role = str(request.get("role", ""))
        method = str(request.get("method", ""))
        args = tuple(request.get("args", ()))
        kwargs = dict(request.get("kwargs", {}))

        target = {
            "lcd": self._trcc.lcd,
            "led": self._trcc.led,
            "control_center": self._trcc.control_center,
        }.get(role)
        if target is None:
            return {"success": False, "error": f"Unknown role: {role!r}"}

        if method.startswith("_"):
            return {"success": False, "error": f"Private method: {method!r}"}

        fn = getattr(target, method, None)
        if not callable(fn):
            return {"success": False,
                    "error": f"Unknown method: {role}.{method}"}

        try:
            result = fn(*args, **kwargs)
        except Exception as e:
            log.exception("manifold dispatch %s.%s failed", role, method)
            return {"success": False,
                    "error": f"{type(e).__name__}: {e}"}

        if not isinstance(result, OpResult):
            return {"success": False,
                    "error": f"{role}.{method} did not return an OpResult "
                             f"(got {type(result).__name__})"}
        return _result_to_dict(result)

    def _dispatch_device(self, method: str, cmd: str,
                         args: list, kwargs: dict) -> dict:
        """Route device sub-commands through unified whitelist."""
        match method:
            case "status":
                return self._device_status()
            case "get_frame":
                return self._get_frame()
            case "pause":
                return self._pause_device()
            case "resume":
                return self._resume_device()
            case _ if method in _DEVICE_METHODS:
                if not self._device or not self._device.connected:
                    return {"success": False, "error": "No device connected"}
                return _sanitize(getattr(self._device, method)(*args, **kwargs))
            case _:
                return {"success": False, "error": f"Unknown command: {cmd}"}

    def _device_status(self) -> dict:
        """Return flat device status with handshake identity."""
        if not self._device or not self._device.connected:
            return {"success": True, "connected": False}
        dev = self._device.device_info
        result: dict[str, Any] = {
            "success": True,
            "connected": True,
            "path": getattr(dev, 'path', ''),
            "vid": getattr(dev, 'vid', 0),
            "pid": getattr(dev, 'pid', 0),
            "pm_byte": getattr(dev, 'pm_byte', 0),
            "sub_byte": getattr(dev, 'sub_byte', 0),
            "model": getattr(dev, 'model', ''),
        }
        # LCD-specific fields
        if self._device.is_lcd:
            result["resolution"] = list(getattr(dev, 'resolution', (0, 0)))
            result["protocol"] = getattr(dev, 'protocol', '')
            result["fbl_code"] = getattr(dev, 'fbl_code', 0)
            result["button_image"] = getattr(dev, 'button_image', '')
        # LED-specific fields
        if self._device.is_led:
            result["led_style_id"] = getattr(dev, 'led_style_id', None)
        return result

    def _pause_device(self) -> dict:
        """Pause LCD frame sending (for exclusive device access)."""
        if not self._device or not self._device.connected:
            return {"success": True, "message": "No device connected"}
        self._device.auto_send = False
        log.info("IPC: device paused (auto_send=False)")
        return {"success": True, "message": "Device paused"}

    def _resume_device(self) -> dict:
        """Resume LCD frame sending after pause."""
        if not self._device or not self._device.connected:
            return {"success": True, "message": "No device connected"}
        self._device.auto_send = True
        log.info("IPC: device resumed (auto_send=True)")
        return {"success": True, "message": "Device resumed"}

    def _get_frame(self) -> dict:
        """Return the current LCD frame as base64 JPEG."""
        import base64

        if self._current_frame is None:
            return {"success": False, "error": "No frame available"}

        frame = self._current_frame

        buf = QByteArray()
        qbuf = QBuffer(buf)
        qbuf.open(QIODevice.OpenModeFlag.WriteOnly)
        frame.save(qbuf, 'jpeg', 85)  # type: ignore[call-overload]
        qbuf.close()
        jpeg_data = bytes(buf.data())

        return {
            "success": True,
            "frame": base64.b64encode(jpeg_data).decode("ascii"),
        }

    def _status(self) -> dict:
        """Return combined device status."""
        result: dict[str, Any] = {"success": True}
        if self._device and self._device.connected:
            dev = self._device.device_info
            if self._device.is_lcd:
                result["lcd"] = {
                    "connected": True,
                    "path": dev.path,
                    "resolution": list(dev.resolution),
                    "protocol": dev.protocol,
                }
            if self._device.is_led:
                result["led"] = {"connected": True}
        return result


def _send_error(client: socket.socket, msg: str) -> None:
    try:
        client.sendall(json.dumps({"success": False, "error": msg}).encode() + b"\n")
    except OSError:
        pass


# =========================================================================
# Transport ABC + implementations
# =========================================================================

class Transport(ABC):
    """Abstract transport for routing device commands to an owning instance."""

    is_ipc: bool = False

    @abstractmethod
    def send(self, cmd: str, args: list | None = None,
             kwargs: dict | None = None) -> dict:
        """Send a command and return the result dict."""


class IPCTransport(Transport):
    """Unix domain socket transport -- routes commands to the GUI daemon."""

    is_ipc: bool = True

    @staticmethod
    def available() -> bool:
        """Check if the IPC daemon is running and accepting connections."""
        if not hasattr(socket, 'AF_UNIX'):
            return False
        path = _socket_path()
        if not path.exists():
            return False
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(1.0)
            s.connect(str(path))
            s.close()
            return True
        except OSError:
            return False

    def send(self, cmd: str, args: list | None = None,
             kwargs: dict | None = None) -> dict:
        if not hasattr(socket, 'AF_UNIX'):
            return {"success": False, "error": "IPC not available on Windows"}
        request = {"cmd": cmd, "args": args or [], "kwargs": kwargs or {}}
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(10.0)
            s.connect(str(_socket_path()))
            s.sendall(json.dumps(request).encode() + b"\n")

            chunks: list[bytes] = []
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                if b"\n" in chunk:
                    break

            s.close()
            if not (data := b"".join(chunks).decode().strip()):
                return {"success": False, "error": "Empty response from daemon"}
            return json.loads(data)
        except TimeoutError:
            return {"success": False, "error": "IPC timeout -- daemon may be busy"}
        except OSError as e:
            return {"success": False, "error": f"IPC connection failed: {e}"}
        except json.JSONDecodeError:
            return {"success": False, "error": "Invalid response from daemon"}


class _APIClient:
    """Minimal HTTP client for routing through a running API server."""

    def __init__(self, port: int | None = None) -> None:
        from .core.instance import DEFAULT_API_PORT
        self._port = port or DEFAULT_API_PORT

    def _request(self, method: str, path: str,
                 body: dict | None = None) -> dict:
        """Send HTTP request, return parsed JSON response."""
        import http.client

        try:
            conn = http.client.HTTPConnection("127.0.0.1", self._port,
                                              timeout=10)
            headers = {"Content-Type": "application/json"}
            payload = json.dumps(body).encode() if body else None
            conn.request(method, path, body=payload, headers=headers)
            resp = conn.getresponse()
            data = resp.read().decode()
            conn.close()
            if resp.status >= 400:
                result = json.loads(data) if data else {}
                detail = result.get("detail", data)
                return {"success": False, "error": detail}
            return {"success": True, **json.loads(data)} if data else {"success": True}
        except (OSError, json.JSONDecodeError) as e:
            return {"success": False, "error": f"API connection failed: {e}"}


# Unified API route table — maps Device method names to HTTP endpoints.
_DEVICE_API_ROUTES: dict[str, tuple[str, str, Any]] = {
    # LCD
    "send_color":           ("POST", "/display/color",
                             lambda r, g, b: {"hex": f"{r:02x}{g:02x}{b:02x}"}),
    "set_brightness":       ("POST", "/display/brightness",
                             lambda level: {"level": level}),
    "set_rotation":         ("POST", "/display/rotation",
                             lambda angle: {"angle": angle}),
    "set_split_mode":       ("POST", "/display/split",
                             lambda mode: {"mode": mode}),
    "reset":                ("POST", "/display/reset", lambda: None),
    "load_theme_by_name":   ("POST", "/themes/load",
                             lambda name, w=0, h=0: {
                                 "name": name,
                                 **({"resolution": f"{w}x{h}"} if w and h else {}),
                             }),
    "load_mask_standalone":  ("POST", "/display/mask",
                              lambda path: {"path": path}),
    # LED
    "set_color":        ("POST", "/led/color",
                         lambda r, g, b: {"hex": f"{r:02x}{g:02x}{b:02x}"}),
    "set_mode":         ("POST", "/led/mode", lambda mode: {"mode": mode}),
    "off":              ("POST", "/led/off", lambda: None),
    "set_sensor_source": ("POST", "/led/sensor",
                          lambda source: {"source": source}),
    "set_clock_format":  ("POST", "/led/clock",
                          lambda is_24h: {"is_24h": is_24h}),
    "set_temp_unit":     ("POST", "/led/temp-unit",
                          lambda unit: {"unit": unit}),
    "status":            ("GET", "/devices/0", lambda: None),
}


class APITransport(Transport):
    """HTTP transport -- routes commands to the ``trcc serve`` API."""

    def __init__(self, port: int | None = None) -> None:
        self._client = _APIClient(port)

    def send(self, cmd: str, args: list | None = None,
             kwargs: dict | None = None) -> dict:
        # Strip domain prefix: "device.send_color" -> "send_color"
        _, _, method = cmd.rpartition(".")
        if (route := _DEVICE_API_ROUTES.get(method)):
            http_method, path, body_fn = route
            body = body_fn(*(args or []), **(kwargs or {})) if body_fn else None
            return self._client._request(http_method, path, body)
        return {"success": False,
                "error": f"No API route for '{cmd}'"}


# =========================================================================
# Unified proxy
# =========================================================================

class DeviceProxy:
    """Proxy — routes method calls through a Transport to the owning instance."""

    connected = True

    def __init__(self, transport: Transport) -> None:
        self._transport = transport

    @property
    def is_ipc(self) -> bool:
        return self._transport.is_ipc

    @property
    def device_path(self) -> str | None:
        result = self._transport.send("device.status")
        return result.get("path")

    @property
    def resolution(self) -> tuple[int, int]:
        result = self._transport.send("device.status")
        r = result.get("resolution", [0, 0])
        return (r[0], r[1])

    @property
    def status(self) -> str | None:
        result = self._transport.send("device.status")
        if result.get("connected"):
            kind = "GUI daemon" if self._transport.is_ipc else "API server"
            return f"Connected (via {kind})"
        return None

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)

        def _proxy(*args: Any, **kwargs: Any) -> dict:
            return self._transport.send(f"device.{name}", list(args), kwargs)
        return _proxy


# Backward-compat aliases — remove after one release cycle
DisplayProxy = DeviceProxy
LEDProxy = DeviceProxy


# =========================================================================
# Proxy factory — injected into core devices via DI
# =========================================================================

def create_device_proxy(kind: Any) -> DeviceProxy:
    """Create a device proxy. Injected into Device as proxy_factory_fn."""
    from trcc.core.instance import InstanceKind

    if kind == InstanceKind.GUI:
        return DeviceProxy(IPCTransport())
    return DeviceProxy(APITransport())


# Backward-compat aliases — remove after one release cycle
create_lcd_proxy = create_device_proxy
create_led_proxy = create_device_proxy


# =========================================================================
# Manifold-format client helpers — used by `TrccProxy` in core/trcc_proxy.py.
# Kept here so the wire format stays in one module (server + client side).
# =========================================================================

def daemon_running(*, socket_path: Path | None = None) -> bool:
    """Probe the daemon socket without sending a request."""
    if not hasattr(socket, 'AF_UNIX'):
        return False
    path = socket_path or _socket_path()
    if not path.exists():
        return False
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(1.0)
        s.connect(str(path))
        s.close()
        return True
    except OSError:
        return False


def send_manifold_request(role: str, method: str,
                          args: tuple, kwargs: dict,
                          *, socket_path: Path | None = None,
                          timeout: float = 10.0) -> dict:
    """Send a manifold-format request to the daemon, return the response dict.

    Wire format (request)::

        {"role": "lcd", "method": "set_brightness",
         "args": [0, 75], "kwargs": {}}

    Transport-level errors (no daemon, timeout, malformed reply) come
    back as ``{"success": False, "error": "<details>"}`` — every call
    produces a result, never raises.
    """
    request = {"role": role, "method": method,
               "args": list(args), "kwargs": kwargs}
    if not hasattr(socket, 'AF_UNIX'):
        return {"success": False,
                "error": "IPC transport: AF_UNIX not available"}
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(str(socket_path or _socket_path()))
        s.sendall(json.dumps(request).encode() + b"\n")
        chunks: list[bytes] = []
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
            if b"\n" in chunk:
                break
        payload = b"".join(chunks).decode().strip()
        if not payload:
            return {"success": False, "error": "IPC: empty response"}
        return json.loads(payload)
    except (TimeoutError, OSError, json.JSONDecodeError) as e:
        return {"success": False,
                "error": f"IPC transport: {type(e).__name__}: {e}"}
    finally:
        try:
            s.close()
        except OSError:
            pass
