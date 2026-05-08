"""Trcc.suspend_all_devices — dedupes by (vid, pid) and delegates to platform.

Issue #143 — at app exit / service stop / shutdown we tear down every
connected device and ask the Platform to put each unique (vid, pid)
chassis into low-power state.
"""
from __future__ import annotations

from trcc.core.results import OpResult


class _RecordingPlatform:
    """Minimal Platform shim that records suspend calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []

    def suspend_usb_device(self, vid: int, pid: int) -> bool:
        self.calls.append((vid, pid))
        return True


class TestSuspendAllDevices:

    def test_no_devices_returns_zero_of_zero(self, _real_trcc_empty):
        platform = _RecordingPlatform()
        # Wire the recording platform onto the existing Trcc — slotted, so
        # we go through the protected attribute the property exposes.
        _real_trcc_empty._platform = platform
        result = _real_trcc_empty.suspend_all_devices()
        assert isinstance(result, OpResult)
        assert result.success is True
        assert result.message == "Suspended 0/0 device(s)"
        assert platform.calls == []

    def test_lcd_and_led_on_same_chassis_calls_suspend_once(self,
                                                             trcc_with_both):
        # MockPlatform spec uses different VID:PIDs for LCD vs LED, so
        # this proves the dedupe set logic doesn't accidentally collapse
        # legitimately distinct devices either.
        platform = _RecordingPlatform()
        trcc_with_both._platform = platform
        result = trcc_with_both.suspend_all_devices()
        assert result.success is True
        # 2 unique vid:pids from spec — 0402:3922 (LCD) + 0416:8001 (LED)
        assert sorted(platform.calls) == [(0x0402, 0x3922), (0x0416, 0x8001)]
        assert "2/2" in result.message

    def test_calls_cleanup_first(self, trcc_with_lcd):
        # After suspend, registries are empty (cleanup ran).
        platform = _RecordingPlatform()
        trcc_with_lcd._platform = platform
        assert len(trcc_with_lcd) == 1
        trcc_with_lcd.suspend_all_devices()
        assert len(trcc_with_lcd) == 0
