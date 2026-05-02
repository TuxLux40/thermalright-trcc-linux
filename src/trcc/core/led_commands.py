"""LEDCommands — every LED user capability, one method each.

UI-facing command surface for LED devices. GUI handlers, CLI subcommands,
and API endpoints all call these methods.

Device's existing `set_*` / `toggle_*` methods already return uniform
result dicts (with IPC proxy routing via `@_forward_to_proxy`). This
phase wraps them in typed dataclasses at the boundary. Persistence is
already inside Device for LED — no move needed.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .models.led import LEDMode
from .results import DiskInfo, LEDResult, LEDSnapshot, LEDStyleInfo, OpResult

if TYPE_CHECKING:
    from .device.led import LEDDevice
    from .events import EventBus

log = logging.getLogger(__name__)


class LEDCommands:
    """Command surface for LED devices."""

    def __init__(self, devices: list[LEDDevice], events: EventBus) -> None:
        self._devices = devices
        self._events = events

    def _get(self, led: int) -> LEDDevice | None:
        if not 0 <= led < len(self._devices):
            log.warning('LED index %d out of range (have %d)', led, len(self._devices))
            return None
        return self._devices[led]

    # ── Color / mode / brightness ────────────────────────────────────

    def set_color(
        self, led: int, r: int, g: int, b: int,
        *, zone: int | None = None,
    ) -> LEDResult:
        dev = self._get(led)
        if dev is None:
            return LEDResult(success=False, error=f'LED {led} not found')
        result = dev.set_zone_color(zone, r, g, b) if zone is not None \
            else dev.set_color(r, g, b)
        if result.get('success'):
            self._events.publish('led.color', led, r, g, b, zone)
        return LEDResult(
            success=result.get('success', False),
            message=result.get('message', ''),
            error=result.get('error'),
            display_colors=result.get('colors', []),
        )

    def set_mode(
        self, led: int, mode: LEDMode | str | int,
        *, zone: int | None = None,
    ) -> LEDResult:
        dev = self._get(led)
        if dev is None:
            return LEDResult(success=False, error=f'LED {led} not found')
        result = dev.set_zone_mode(zone, mode) if zone is not None \
            else dev.set_mode(mode)
        if result.get('success'):
            self._events.publish('led.mode', led, mode, zone)
        return LEDResult(
            success=result.get('success', False),
            message=result.get('message', ''),
            error=result.get('error'),
            display_colors=result.get('colors', []),
        )

    def set_brightness(
        self, led: int, percent: int, *, zone: int | None = None,
    ) -> LEDResult:
        dev = self._get(led)
        if dev is None:
            return LEDResult(success=False, error=f'LED {led} not found')
        result = dev.set_zone_brightness(zone, percent) if zone is not None \
            else dev.set_brightness(percent)
        if result.get('success'):
            self._events.publish('led.brightness', led, percent, zone)
        return LEDResult(
            success=result.get('success', False),
            message=result.get('message', ''),
            error=result.get('error'),
            display_colors=result.get('colors', []),
        )

    def toggle(self, led: int, on: bool, *, zone: int | None = None) -> LEDResult:
        dev = self._get(led)
        if dev is None:
            return LEDResult(success=False, error=f'LED {led} not found')
        result = dev.toggle_zone(zone, on) if zone is not None \
            else dev.toggle_global(on)
        if result.get('success'):
            self._events.publish('led.toggled', led, on, zone)
        return LEDResult(
            success=result.get('success', False),
            message=result.get('message', ''),
            error=result.get('error'),
        )

    def toggle_segment(self, led: int, index: int, on: bool) -> LEDResult:
        dev = self._get(led)
        if dev is None:
            return LEDResult(success=False, error=f'LED {led} not found')
        result = dev.toggle_segment(index, on)
        return LEDResult(
            success=result.get('success', False),
            message=result.get('message', ''),
            error=result.get('error'),
        )

    # ── Zones ────────────────────────────────────────────────────────

    def select_zone(self, led: int, zone: int) -> OpResult:
        dev = self._get(led)
        if dev is None:
            return OpResult(success=False, error=f'LED {led} not found')
        r = dev.set_selected_zone(zone)
        return OpResult(
            success=r.get('success', False),
            message=r.get('message', f'Zone {zone} selected'),
            error=r.get('error'),
        )

    def set_zone_sync(
        self, led: int, enabled: bool,
        *, zones: list[int] | None = None,
        interval_s: int | None = None,
    ) -> OpResult:
        dev = self._get(led)
        if dev is None:
            return OpResult(success=False, error=f'LED {led} not found')
        if zones is not None:
            for idx, z in enumerate(zones):
                dev.set_zone_sync_zone(z, True)
                log.debug('zone_sync include zone=%d (iter=%d)', z, idx)
        r = dev.set_zone_sync(enabled, interval_s)
        if r.get('success'):
            self._events.publish('led.zone_sync', led, enabled, interval_s)
        return OpResult(
            success=r.get('success', False),
            message=r.get('message', ''),
            error=r.get('error'),
        )

    # ── Display modes ────────────────────────────────────────────────

    def set_clock_format(self, led: int, is_24h: bool) -> OpResult:
        dev = self._get(led)
        if dev is None:
            return OpResult(success=False, error=f'LED {led} not found')
        r = dev.set_clock_format(is_24h)
        if r.get('success'):
            self._events.publish('led.clock', led, is_24h)
        return OpResult(
            success=r.get('success', False),
            message=r.get('message', ''),
            error=r.get('error'),
        )

    def set_week_start(self, led: int, sunday: bool) -> OpResult:
        dev = self._get(led)
        if dev is None:
            return OpResult(success=False, error=f'LED {led} not found')
        r = dev.set_week_start(sunday)
        return OpResult(
            success=r.get('success', False),
            message=r.get('message', ''),
            error=r.get('error'),
        )

    def set_memory_ratio(self, led: int, ratio: int) -> OpResult:
        dev = self._get(led)
        if dev is None:
            return OpResult(success=False, error=f'LED {led} not found')
        r = dev.set_memory_ratio(ratio)
        return OpResult(
            success=r.get('success', False),
            message=r.get('message', f'Memory ratio: {ratio}x'),
            error=r.get('error'),
        )

    def set_disk_index(self, led: int, index: int) -> OpResult:
        dev = self._get(led)
        if dev is None:
            return OpResult(success=False, error=f'LED {led} not found')
        r = dev.set_disk_index(index)
        return OpResult(
            success=r.get('success', False),
            message=r.get('message', f'Disk {index} selected'),
            error=r.get('error'),
        )

    def set_test_mode(self, led: int, enabled: bool) -> OpResult:
        dev = self._get(led)
        if dev is None:
            return OpResult(success=False, error=f'LED {led} not found')
        r = dev.set_test_mode(enabled)
        return OpResult(
            success=r.get('success', False),
            message=r.get('message', f'Test mode: {enabled}'),
            error=r.get('error'),
        )

    def set_sensor_source(self, led: int, source: str) -> OpResult:
        dev = self._get(led)
        if dev is None:
            return OpResult(success=False, error=f'LED {led} not found')
        r = dev.set_sensor_source(source)
        if r.get('success'):
            self._events.publish('led.sensor', led, source)
        return OpResult(
            success=r.get('success', False),
            message=r.get('message', ''),
            error=r.get('error'),
        )

    # ── Listing ──────────────────────────────────────────────────────

    def list_styles(self) -> list[LEDStyleInfo]:
        from .models.led import LED_STYLES
        return [
            LEDStyleInfo(
                style_id=sid,
                name=info.model_name,
                segment_count=info.segment_count,
                zone_count=info.zone_count,
                supported_modes=[m.name.lower() for m in LEDMode],
            )
            for sid, info in LED_STYLES.items()
        ]

    def list_modes(self, led: int) -> list[str]:
        dev = self._get(led)
        if dev is None:
            return []
        return [m.name.lower() for m in LEDMode]

    def list_disks(self) -> list[DiskInfo]:
        # Phase 5: delegate to Platform.get_disk_info() via ControlCenterCommands.
        log.debug('list_disks (phase-3 stub)')
        return []

    # ── Snapshot ─────────────────────────────────────────────────────

    def snapshot(self, led: int) -> LEDSnapshot:
        dev = self._get(led)
        if dev is None or dev.state is None:
            return LEDSnapshot(
                connected=False, style_id=0, mode=0, color=(0, 0, 0),
                brightness=0, global_on=False, zones=[], zone_sync=False,
                zone_sync_interval=0, selected_zone=0, segment_on=[],
                clock_24h=True, week_sunday=False, memory_ratio=1,
                disk_index=0, test_mode=False,
            )
        s = dev.state
        info = dev.device_info
        return LEDSnapshot(
            connected=dev.connected,
            style_id=getattr(info, 'led_style_id', 0) or 0,
            mode=s.mode.value if hasattr(s.mode, 'value') else int(s.mode),
            color=tuple(s.color) if s.color else (0, 0, 0),
            brightness=s.brightness,
            global_on=s.global_on,
            zones=[{'mode': z.mode.value, 'color': list(z.color),
                    'brightness': z.brightness, 'on': z.on} for z in s.zones],
            zone_sync=s.zone_sync,
            zone_sync_interval=s.zone_sync_interval,
            selected_zone=getattr(s, 'selected_zone', 0),
            segment_on=list(s.segment_on),
            clock_24h=getattr(s, 'is_timer_24h', True),
            week_sunday=getattr(s, 'is_week_sunday', False),
            memory_ratio=getattr(s, 'memory_ratio', 1),
            disk_index=getattr(s, 'disk_index', 0),
            test_mode=getattr(s, 'test_mode', False),
        )
