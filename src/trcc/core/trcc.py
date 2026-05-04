"""Trcc — the unified command facade for GUI, CLI, and API.

The one class every UI talks to. Composes LCDCommands, LEDCommands,
ControlCenterCommands, and an EventBus. Holds discovered devices.
Owns the metrics tick loop, data extraction pipeline, and hotplug.

Parity rule: every method reachable from one UI is reachable from all
three. No shortcuts, no UI-specific extensions. See TRCC_CONTRACT.md.

Pure DI (Mark Seemann): every dependency arrives at construction. No
late ``set_X`` mutation. Composition root (``trcc._boot.trcc()``) is
the one place that wires real adapters; tests pass fakes via the same
keyword arguments.

Usage from a composition root::

    trcc = Trcc(platform, renderer=QtRenderer(),
                ensure_data_fn=DataManager.ensure_all)
    trcc.discover()

From any UI::

    trcc.lcd.set_brightness(0, 50)
    trcc.led.set_color(0, 255, 0, 0)
    trcc.control_center.set_temp_unit('F')
    trcc.events.subscribe('device.list', on_devices_changed)
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterator
from itertools import chain
from typing import TYPE_CHECKING, Any

from .control_center_commands import ControlCenterCommands
from .device.registry import DeviceRegistry
from .events import EventBus, Topic
from .lcd_commands import LCDCommands
from .led_commands import LEDCommands
from .results import DiscoveryResult

if TYPE_CHECKING:
    from ..services.system import SystemService
    from .device.lcd import LCDDevice
    from .device.led import LEDDevice
    from .models import DetectedDevice
    from .ports import EnsureDataFn, Platform, Renderer

log = logging.getLogger(__name__)

# Animation tick rate — 50ms gives 20 FPS for video and overlay updates.
_TICK_INTERVAL = 0.05


class Trcc:
    """Universal command facade and process context.

    Construction is explicit (takes Platform + optional infra deps).
    One Trcc per process — composition root builds it, UI adapters
    consume it.

    Slotted: no per-instance __dict__, ~40% smaller footprint, faster
    attribute access (direct offset, no dict lookup).
    """

    __slots__ = (
        '_current_metrics', '_download_pack_fn', '_ensure_data_fn',
        '_lcd_devices', '_led_devices', '_list_available_fn',
        '_metrics_stop', '_metrics_thread', '_metrics_wake',
        '_platform', '_renderer', '_settings', '_system_svc',
        'control_center', 'events', 'lcd', 'led',
    )

    def __init__(
        self,
        platform: Platform,
        *,
        renderer: Renderer | None = None,
        system_svc: SystemService | None = None,
        ensure_data_fn: EnsureDataFn | None = None,
        download_pack_fn: Callable[..., int] | None = None,
        list_available_fn: Callable[..., None] | None = None,
        settings: Any = None,
    ) -> None:
        self._platform = platform
        self._renderer = renderer
        self._system_svc = system_svc
        self._ensure_data_fn = ensure_data_fn
        self._download_pack_fn = download_pack_fn
        self._list_available_fn = list_available_fn
        # Settings injection (Phase 10A.3 partial). When None, falls back to
        # ``trcc.conf.settings`` global lazily — backwards compatible. The
        # full Pure-DI version (every reader takes settings as ctor arg,
        # global goes away) is its own focused session.
        self._settings = settings
        self._current_metrics: Any = None

        self._lcd_devices: DeviceRegistry[LCDDevice] = DeviceRegistry()
        self._led_devices: DeviceRegistry[LEDDevice] = DeviceRegistry()

        self._metrics_thread: threading.Thread | None = None
        self._metrics_stop: threading.Event = threading.Event()
        self._metrics_wake: threading.Event = threading.Event()

        self.events = EventBus()
        self.lcd = LCDCommands(self._lcd_devices, self.events)
        self.led = LEDCommands(self._led_devices, self.events)
        self.control_center = ControlCenterCommands(platform, self.events)

    # ── Lifecycle ────────────────────────────────────────────────────
    # No public ``bootstrap`` / ``with_renderer`` / factory classmethods —
    # composition lives in ``trcc._boot.trcc()``. Constructors are final
    # (Pure DI). Tests build a Trcc directly with the kwargs they need.

    @property
    def os(self) -> Platform:
        """The Platform this Trcc is bound to. Read-only — composition is final."""
        return self._platform

    @property
    def settings(self) -> Any:
        """The injected `Settings` instance, falling back to the module global.

        Phase 10A.3 (partial) — Trcc holds settings explicitly via DI when
        the composition root passes one. Older readers that haven't been
        migrated yet still get the global via the fallback. Tests that want
        isolation pass their own `settings` to `Trcc(settings=…)`.
        """
        if self._settings is not None:
            return self._settings
        from ..conf import settings as _global
        return _global

    # ── Convenience accessors — first-of-kind devices ───────────────────

    @property
    def lcd_device(self) -> LCDDevice | None:
        """First connected LCD device, or None.

        For sustained use access devices via iteration (`for d in trcc:`)
        or the typed lists (``lcd_devices``, ``led_devices``).
        """
        return self._lcd_devices[0] if self._lcd_devices else None

    @property
    def led_device(self) -> LEDDevice | None:
        """First connected LED device, or None."""
        return self._led_devices[0] if self._led_devices else None

    @property
    def lcd_devices(self) -> DeviceRegistry[LCDDevice]:
        """All connected LCD devices, in detection order.

        Returns the live `DeviceRegistry` — callers get rich indexing
        on top of normal iteration / len / membership::

            trcc.lcd_devices[0]                  # by index
            trcc.lcd_devices['/dev/sg0']         # by device_path
            trcc.lcd_devices[(0x0402, 0x3922)]   # by (vid, pid)
        """
        return self._lcd_devices

    @property
    def led_devices(self) -> DeviceRegistry[LEDDevice]:
        """All connected LED devices, in detection order. See :attr:`lcd_devices`."""
        return self._led_devices

    @property
    def has_lcd(self) -> bool:
        """True iff at least one LCD device is connected."""
        return bool(self._lcd_devices)

    @property
    def has_led(self) -> bool:
        """True iff at least one LED device is connected."""
        return bool(self._led_devices)

    def register_lcd(self, device: LCDDevice) -> int:
        """Register an already-built+connected LCD device with the command layer.

        Returns the index the device got.  UI adapters that manage their own
        device lifecycle (e.g. the GUI's per-handler detection flow) call
        this instead of `discover()` so their devices show up in
        `Trcc.lcd._devices` and command dispatch resolves the index.

        Idempotent: if the same device is already registered, returns its
        existing index.
        """
        if device in self._lcd_devices:
            return self._lcd_devices.index(device)
        self._lcd_devices.append(device)
        return len(self._lcd_devices) - 1

    def register_led(self, device: LEDDevice) -> int:
        """Register an already-built+connected LED device.  Same contract as
        `register_lcd`."""
        if device in self._led_devices:
            return self._led_devices.index(device)
        self._led_devices.append(device)
        return len(self._led_devices) - 1

    def discover(
        self, *,
        path: str | None = None,
        ensure_data: bool = True,
    ) -> DiscoveryResult:
        """Enumerate, connect, and register every reachable device.

        Detection is sequential (single USB enumerate call); per-device
        connect runs in parallel — one daemon thread per device, capped
        at 30s — so USB handshakes don't serialize.

        Each successful connect mirrors into ``_lcd_devices`` /
        ``_led_devices``, runs the LCD pipeline init, and publishes
        ``device.connected``. After the pool drains we publish a single
        ``device.list`` snapshot.

        ``ensure_data`` blocks on theme/web/mask extraction for every
        unique LCD resolution before returning, so the UI starts with
        themes present and skips the empty-list-then-populate flash.
        Hotplug uses :meth:`device_connected` instead — non-blocking.

        ``path`` constrains the result to a specific USB path; if no
        connected device matches we return a failed result so CLI
        callers like ``trcc lcd test --device /dev/sg0`` surface the
        error cleanly without a separate post-scan check.
        """
        from .builder import ControllerBuilder
        from .device.lcd import LCDDevice
        from .device.led import LEDDevice
        from .models import PROTOCOL_TRAITS

        builder = ControllerBuilder(self._platform)
        if self._renderer is not None:
            builder = builder.with_renderer(self._renderer)

        try:
            detect_fn = builder.build_detect_fn()
            detected = detect_fn()
        except Exception as e:
            log.exception('discover: detect failed')
            return DiscoveryResult(success=False, error=str(e))

        log.info('discover: detected %d device(s): %s', len(detected),
                 ', '.join(f'{d.path} ({d.protocol})' for d in detected) or '(none)')

        self._lcd_devices.clear()
        self._led_devices.clear()
        _settings = self.settings
        lock = threading.Lock()

        def _connect_one(found: DetectedDevice) -> None:
            traits = PROTOCOL_TRAITS.get(
                getattr(found, 'protocol', 'scsi'), PROTOCOL_TRAITS['scsi'])
            try:
                device = builder.build_device(found)
                connect_result = device.connect(found)
                if not connect_result.get('success'):
                    log.warning('discover: connect failed for %s: %s',
                                found.path, connect_result.get('error'))
                    return
            except Exception:
                log.exception('discover: build/connect raised for %s', found.path)
                return

            info = device.device_info
            res = getattr(info, 'resolution', (0, 0)) if info else (0, 0)
            log.info('discover: connected %s %dx%d', found.path, *res)

            with lock:
                if traits.is_lcd and isinstance(device, LCDDevice):
                    self._lcd_devices.append(device)
                    if _settings is not None:
                        device.initialize_pipeline(_settings)
                elif traits.is_led and isinstance(device, LEDDevice):
                    self._led_devices.append(device)
                self.events.publish(Topic.DEVICE_CONNECTED, device)

        threads = [
            threading.Thread(target=_connect_one, args=(d,), daemon=True)
            for d in detected
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        all_devices = tuple(chain(self._lcd_devices, self._led_devices))
        self.events.publish(Topic.DEVICE_LIST, all_devices)
        log.info('discover: %d of %d device(s) connected (%d LCD, %d LED)',
                 len(all_devices), len(detected),
                 len(self._lcd_devices), len(self._led_devices))

        if path and not any(
            getattr(d, 'device_path', None) == path for d in all_devices
        ):
            return DiscoveryResult(
                success=False, error=f'Device not found: {path}')

        if ensure_data:
            self._ensure_data_blocking()

        lcd_infos = [d.device_info for d in self._lcd_devices]
        led_infos = [d.device_info for d in self._led_devices]
        return DiscoveryResult(
            success=True,
            message=f'Found {len(lcd_infos)} LCD(s), {len(led_infos)} LED(s)',
            lcd_devices=lcd_infos,
            led_devices=led_infos,
        )

    # ── Hotplug ──────────────────────────────────────────────────────

    def device_connected(self, detected: DetectedDevice) -> None:
        """Build, connect, and register a hotplugged device.

        Background data-ensure runs in a thread so the UI keeps
        responding while themes for the new resolution download.
        """
        from .builder import ControllerBuilder
        from .device.lcd import LCDDevice
        from .device.led import LEDDevice
        from .models import PROTOCOL_TRAITS

        builder = ControllerBuilder(self._platform)
        if self._renderer is not None:
            builder = builder.with_renderer(self._renderer)

        log.info('device_connected: hotplug %s (%s)', detected.path, detected.protocol)
        try:
            device = builder.build_device(detected)
            connect_result = device.connect(detected)
            if not connect_result.get('success'):
                log.warning('device_connected: connect failed for %s', detected.path)
                return
        except Exception:
            log.exception('device_connected: build/connect raised for %s', detected.path)
            return

        info = device.device_info
        res = getattr(info, 'resolution', (0, 0)) if info else (0, 0)
        log.info('device_connected: connected %s %dx%d', detected.path, *res)

        traits = PROTOCOL_TRAITS.get(
            getattr(detected, 'protocol', 'scsi'), PROTOCOL_TRAITS['scsi'])
        _settings = self.settings
        if traits.is_lcd and isinstance(device, LCDDevice):
            self._lcd_devices.append(device)
            if _settings is not None:
                device.initialize_pipeline(_settings)
            w, h = res
            if w and h:
                self._ensure_data_background(device, w, h)
        elif traits.is_led and isinstance(device, LEDDevice):
            self._led_devices.append(device)

        self.events.publish(Topic.DEVICE_CONNECTED, device)
        self.events.publish(
            Topic.DEVICE_LIST,
            tuple(chain(self._lcd_devices, self._led_devices)),
        )

    def device_lost(self, path: str) -> None:
        """Remove a device by USB path; publish disconnected + new list."""
        gone: LCDDevice | LEDDevice
        if path in self._lcd_devices:
            gone = self._lcd_devices[path]
            self._lcd_devices.remove(gone)
        elif path in self._led_devices:
            gone = self._led_devices[path]
            self._led_devices.remove(gone)
        else:
            return  # path not found in either registry — no-op
        self.events.publish(Topic.DEVICE_DISCONNECTED, gone)
        self.events.publish(
            Topic.DEVICE_LIST,
            tuple(chain(self._lcd_devices, self._led_devices)),
        )

    # ── Data extraction ──────────────────────────────────────────────

    def _ensure_data_blocking(self) -> None:
        """Run ensure_all() synchronously for every unique LCD resolution.

        Called at the end of discover() so the UI starts with data
        present. Hotplug uses :meth:`_ensure_data_background` instead.
        """
        if (ensure_fn := self._ensure_data_fn) is None:
            log.warning('_ensure_data_blocking: no ensure_fn injected — skipping')
            return
        seen: set[tuple[int, int]] = set()
        for device in self._lcd_devices:
            info = device.device_info
            path = getattr(info, 'path', '?') if info else '?'
            w, h = getattr(info, 'resolution', (0, 0)) if info else (0, 0)
            if not (w and h):
                log.warning('_ensure_data_blocking: skip %s — resolution (0,0)', path)
                continue
            if (w, h) in seen:
                continue
            seen.add((w, h))
            log.info('_ensure_data_blocking: ensuring data %dx%d for %s', w, h, path)
            ensure_fn(
                w, h,
                progress_fn=lambda msg: self.events.publish(
                    Topic.BOOTSTRAP_PROGRESS, msg),
            )
            device.notify_data_ready()
        log.info('_ensure_data_blocking: done — %d resolution(s) processed', len(seen))

    def _ensure_data_background(
        self, device: LCDDevice, w: int, h: int,
    ) -> None:
        """Ensure theme data in a background thread — hotplug path."""
        ensure_fn = self._ensure_data_fn
        info = device.device_info
        path = getattr(info, 'path', '?') if info else '?'
        log.info('_ensure_data_background: starting %dx%d for %s', w, h, path)

        def _bg() -> None:
            try:
                if ensure_fn is not None:
                    ensure_fn(w, h)
                else:
                    log.warning('_ensure_data_background: no ensure_fn for %s', path)
                device.notify_data_ready()
                log.info('_ensure_data_background: done %dx%d for %s', w, h, path)
            except Exception:
                log.exception('_ensure_data_background: failed %dx%d for %s', w, h, path)

        threading.Thread(target=_bg, daemon=True, name='data-extract').start()

    # ── Metrics / animation loop ─────────────────────────────────────

    def set_system_service(self, system_svc: SystemService) -> None:
        """Inject SystemService post-construction (pre-existing tests + dev mocks).

        Composition root prefers the constructor kwarg; this is the
        deprecated late-DI seam kept for the test fixture chain that
        builds a Trcc before the service exists.
        """
        from ..services.system import set_instance
        self._system_svc = system_svc
        set_instance(system_svc)

    @property
    def current_metrics(self) -> Any:
        """Most recently polled metrics with temp unit applied, or None."""
        return self._current_metrics

    def start_metrics_loop(self, interval: float | None = None) -> None:
        """Start the 50ms tick + sensor-poll loop in a background thread.

        Two cadences in one thread:
          * **Tick** (every 50ms): advance device animation, send frames,
            publish ``frame`` events.
          * **Poll** (every ``settings.refresh_interval``): read sensors,
            update every device, publish ``metrics``.

        UIs (GUI / CLI / API) subscribe to events — none run their own
        tick loops.
        """
        if self._system_svc is None:
            raise RuntimeError(
                'Trcc.start_metrics_loop: SystemService not injected. '
                'Pass system_svc=… at construction or call set_system_service().')
        self.stop_metrics_loop()
        self._metrics_stop.clear()
        sys_svc = self._system_svc

        # Capture settings at loop start — bound through DI when available,
        # falls back to the global lazily otherwise (Phase 10A.3 partial).
        _settings = self.settings

        def _loop() -> None:
            from .models import HardwareMetrics
            tick_count = 0
            while not self._metrics_stop.is_set():
                try:
                    poll_interval = (
                        interval if interval is not None
                        else max(1, _settings.refresh_interval)
                    )
                    metrics_every = max(1, int(poll_interval / _TICK_INTERVAL))
                    if tick_count % metrics_every == 0:
                        try:
                            metrics = HardwareMetrics.with_temp_unit(
                                sys_svc.all_metrics, _settings.temp_unit)
                            self._current_metrics = metrics
                            for device in tuple(chain(
                                self._lcd_devices, self._led_devices,
                            )):
                                try:
                                    device.update_metrics(metrics)
                                except Exception:
                                    log.exception('Metrics update error')
                            self.events.publish(Topic.METRICS, metrics)
                        except Exception:
                            log.exception('Metrics poll error')

                    for device in tuple(self._lcd_devices):
                        path = getattr(device, 'device_path', '?') or '?'
                        try:
                            if (result := device.tick()) is not None:
                                self.events.publish(Topic.FRAME, path, result)
                            elif device.playing:
                                device.update_video_cache_text(self._current_metrics)
                        except Exception:
                            log.exception('LCD tick error: %s', path)
                    for device in tuple(self._led_devices):
                        info = device.device_info
                        path = (getattr(info, 'path', '?') if info else '?') or '?'
                        try:
                            if (result := device.tick()) is not None:
                                self.events.publish(Topic.FRAME, path, result)
                        except Exception:
                            log.exception('LED tick error: %s', path)
                except Exception:
                    log.exception('Tick loop error')
                tick_count += 1
                self._metrics_wake.wait(_TICK_INTERVAL)
                self._metrics_wake.clear()

        self._metrics_thread = threading.Thread(
            target=_loop, daemon=True, name='trcc-metrics')
        self._metrics_thread.start()
        log.debug('Metrics loop started (tick=%.0fms, poll=settings.refresh_interval)',
                  _TICK_INTERVAL * 1000)

    def stop_metrics_loop(self) -> None:
        """Stop the background metrics loop. Idempotent."""
        self._metrics_stop.set()
        self._metrics_wake.set()
        if self._metrics_thread and self._metrics_thread.is_alive():
            self._metrics_thread.join(timeout=3)
        self._metrics_thread = None
        self._metrics_wake.clear()
        self._metrics_stop.clear()

    def wake_metrics_loop(self) -> None:
        """Wake the metrics loop immediately — used after settings changes."""
        self._metrics_wake.set()

    # ── Cross-cutting OS operations (touch every device + the loop) ──

    def apply_temp_unit(self, unit: int) -> dict[str, Any]:
        """Persist temp unit + push to every device + refresh metrics.

        Cross-cuts settings, every connected device, and the metrics
        loop — lives on Trcc rather than ControlCenterCommands because
        no facade owns the device list.
        """
        from .models import HardwareMetrics

        self.settings.set_temp_unit(unit)
        fresh = None
        if (svc := self._system_svc) is not None:
            fresh = HardwareMetrics.with_temp_unit(svc.all_metrics, unit)
            self._current_metrics = fresh

        for device in chain(self._lcd_devices, self._led_devices):
            device.set_temp_unit(unit)
            if fresh is not None:
                device.update_metrics(fresh)

        self.wake_metrics_loop()
        unit_str = 'F' if unit else 'C'
        self.events.publish(Topic.CONTROL_CENTER_TEMP_UNIT, unit_str)
        return {'success': True, 'message': f'Temperature unit set to °{unit_str}'}

    def set_metrics_refresh(self, seconds: int) -> dict[str, Any]:
        """Persist the metrics refresh interval and wake the loop."""
        clamped = max(1, min(100, seconds))
        self.settings.set_refresh_interval(clamped)
        self.wake_metrics_loop()
        self.events.publish(Topic.CONTROL_CENTER_REFRESH, clamped)
        return {'success': True, 'message': f'Refresh interval set to {clamped}s'}

    # ── Data / theme distribution (delegates to injected callables) ──

    def download_themes(self, pack: str = '', force: bool = False) -> int:
        """Download a theme pack, or list available packs when pack=''.

        Returns a CLI-shaped exit code (0 = ok, 1 = error / no pack fn).
        """
        if not pack:
            if self._list_available_fn is not None:
                self._list_available_fn()
            return 0
        if self._download_pack_fn is not None:
            return self._download_pack_fn(pack, force)
        log.warning('download_themes: no download_pack_fn injected')
        return 1

    def cleanup(self) -> None:
        """Stop the metrics loop, release every device, drop subscribers."""
        self.stop_metrics_loop()
        for dev in self:
            try:
                dev.cleanup()
            except Exception:
                log.exception('cleanup failed for %s', dev)
        self._lcd_devices.clear()
        self._led_devices.clear()
        self.events.clear()

    # ── Container protocol ───────────────────────────────────────────
    # Trcc IS the registry of connected devices — `for d in trcc` walks
    # every LCD then every LED, `len(trcc)` is total device count, and
    # `bool(trcc)` is True iff anything is connected.

    def __iter__(self) -> Iterator[LCDDevice | LEDDevice]:
        return chain(self._lcd_devices, self._led_devices)

    def __len__(self) -> int:
        return len(self._lcd_devices) + len(self._led_devices)

    def __bool__(self) -> bool:
        return bool(self._lcd_devices) or bool(self._led_devices)

    # ── Context manager — deterministic cleanup on `with` exit ─────────

    def __enter__(self) -> Trcc:
        return self

    def __exit__(self, *exc: object) -> None:
        self.cleanup()
