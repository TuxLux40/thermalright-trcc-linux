"""Windows Platform — single file, single class, all Windows logic."""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psutil

from trcc.adapters.system._base import SensorEnumeratorBase
from trcc.adapters.system._shared import (
    _confirm,
    _copy_assets_to_user_dir,
    _print_summary,
)
from trcc.core.models import SensorInfo
from trcc.core.ports import (
    DoctorPlatformConfig,
    Platform,
    ReportPlatformConfig,
)

log = logging.getLogger(__name__)


# =========================================================================
# Private constants
# =========================================================================

_REG_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
_REG_VALUE = 'TRCC Linux'

# LHM SensorType → (category, unit). Strings come from the WMI Sensor.SensorType
# column; mirrors LibreHardwareMonitorLib's SensorType enum names.
_LHM_TYPE_MAP: dict[str, tuple[str, str]] = {
    'Temperature': ('temperature', '°C'),
    'Fan': ('fan', 'RPM'),
    'Clock': ('clock', 'MHz'),
    'Load': ('usage', '%'),
    'Power': ('power', 'W'),
    'Voltage': ('voltage', 'V'),
    'SmallData': ('memory', 'MB'),
    'Data': ('memory', 'GB'),
    'Throughput': ('throughput', 'B/s'),
}


# =========================================================================
# Private helper functions
# =========================================================================

def _winreg() -> Any:
    """Return winreg stdlib module."""
    import winreg  # pyright: ignore[reportMissingImports]
    return winreg


def _format_size(size_bytes: str | int | None) -> str:
    if not size_bytes:
        return ''
    try:
        b = int(size_bytes)
        if b >= 1024 ** 4:
            return f'{b / (1024 ** 4):.1f} TB'
        if b >= 1024 ** 3:
            return f'{b / (1024 ** 3):.0f} GB'
        if b >= 1024 ** 2:
            return f'{b / (1024 ** 2):.0f} MB'
        return f'{b} B'
    except (ValueError, TypeError):
        return str(size_bytes)


def _memory_form_factor(code: int | None) -> str:
    factors = {8: 'DIMM', 12: 'SODIMM', 13: 'RIMM', 15: 'FB-DIMM', 16: 'Die'}
    return factors.get(code or 0, 'Unknown')


def _memory_type(code: int | None) -> str:
    types = {20: 'DDR', 21: 'DDR2', 24: 'DDR3', 26: 'DDR4', 30: 'LPDDR4', 34: 'DDR5', 35: 'LPDDR5'}
    return types.get(code or 0, 'Unknown')


def _disk_type(disk: object) -> str:
    model = (getattr(disk, 'Model', '') or '').upper()
    media_type = (getattr(disk, 'MediaType', '') or '').upper()
    if 'SSD' in model or 'NVME' in model or 'SOLID' in media_type:
        return 'SSD'
    if 'HDD' in model or 'FIXED' in media_type:
        return 'HDD'
    return 'Unknown'


def _get_disk_health(device_id: str | None) -> str:
    if not device_id:
        return 'Unknown'
    try:
        from ._windows_wmi import wmi_handle
        w = wmi_handle(namespace='root\\WMI')
        for status in w.MSStorageDriver_FailurePredictStatus():
            if status.Active:
                return 'FAILED' if status.PredictFailure else 'PASSED'
    except ImportError:
        log.debug("wmi package unavailable for disk health probe")
    except Exception as e:
        # WMI/COM exceptions don't share a common Python base — keep broad, log.
        log.debug("WMI MSStorageDriver_FailurePredictStatus query failed for %s: %s", device_id, e)
    return 'Unknown'


def get_memory_info() -> list[dict[str, str]]:
    """Get DRAM slot info via WMI Win32_PhysicalMemory."""
    slots: list[dict[str, str]] = []
    try:
        from ._windows_wmi import wmi_handle
        w = wmi_handle()
        for mem in w.Win32_PhysicalMemory():
            slot: dict[str, str] = {}
            slot['manufacturer'] = (mem.Manufacturer or '').strip()
            slot['part_number'] = (mem.PartNumber or '').strip()
            slot['speed'] = str(mem.ConfiguredClockSpeed or mem.Speed or '')
            slot['configured_memory_speed'] = str(mem.ConfiguredClockSpeed or '')
            slot['size'] = _format_size(mem.Capacity)
            slot['form_factor'] = _memory_form_factor(mem.FormFactor)
            slot['type'] = _memory_type(mem.SMBIOSMemoryType)
            slot['locator'] = mem.DeviceLocator or ''
            slot['rank'] = str(mem.Rank or '')
            slot['data_width'] = str(mem.DataWidth or '')
            slot['total_width'] = str(mem.TotalWidth or '')
            if slot['size'] and slot['size'] != '0':
                slots.append(slot)
    except ImportError:
        log.debug("wmi package not available — using psutil fallback")
        mem = psutil.virtual_memory()
        slots.append({
            'manufacturer': 'Unknown', 'part_number': 'Unknown',
            'type': 'Unknown', 'speed': 'Unknown',
            'size': f'{mem.total // (1024 ** 3)} GB',
            'form_factor': 'Unknown', 'locator': 'Total',
        })
    except Exception:
        log.exception("WMI memory query failed")
    return slots


def get_disk_info() -> list[dict[str, str]]:
    """Get physical disk info via WMI Win32_DiskDrive."""
    disks: list[dict[str, str]] = []
    try:
        from ._windows_wmi import wmi_handle
        w = wmi_handle()
        for disk in w.Win32_DiskDrive():
            info: dict[str, str] = {}
            info['name'] = disk.DeviceID or ''
            info['model'] = (disk.Model or '').strip()
            info['size'] = _format_size(disk.Size)
            info['type'] = _disk_type(disk)
            info['health'] = _get_disk_health(disk.DeviceID)
            disks.append(info)
    except ImportError:
        log.debug("wmi package not available")
    except Exception:
        log.exception("WMI disk query failed")
    return disks


# =========================================================================
# SensorEnumerator — file-scoped
# =========================================================================

class SensorEnumerator(SensorEnumeratorBase):
    """Windows sensor discovery: LHM > pynvml > psutil > WMI."""

    def __init__(self) -> None:
        super().__init__()
        from ._lhm_subprocess import _LHMSubprocess
        self._lhm = _LHMSubprocess()
        self._lhm_gpu_used = False

    def discover(self) -> list[SensorInfo]:
        self._sensors.clear()
        self._lhm_gpu_used = False
        self._discover_psutil_win()
        self._discover_lhm()
        if not self._lhm_gpu_used:
            self._discover_nvidia()
        self._discover_wmi()
        self._discover_computed()
        log.info("Windows sensor discovery: %d sensors", len(self._sensors))
        return self._sensors

    def _on_stop(self) -> None:
        self._lhm.stop()

    def _discover_psutil_win(self) -> None:
        self._discover_psutil_base()
        temps = psutil.sensors_temperatures() if hasattr(psutil, 'sensors_temperatures') else {}
        for chip, entries in temps.items():
            for i, entry in enumerate(entries):
                sid = f'psutil:temp:{chip}:{i}'
                label = entry.label or f'{chip} temp{i}'
                self._sensors.append(SensorInfo(sid, label, 'temperature', '°C', 'psutil'))

    def _discover_lhm(self) -> None:
        """Spawn LHM (or reuse a running instance) and register its sensors.

        Sensors come from ``root\\LibreHardwareMonitor`` WMI namespace; LHM
        runs as a subprocess (``_lhm_subprocess.py``) and exposes a flat
        Hardware/Sensor tree we walk via ``_walk_lhm_nodes``.
        """
        if self._lhm.start() is None:
            return
        for hw_key, hw_row in self._walk_lhm_nodes():
            if 'Gpu' in str(hw_row.HardwareType):
                self._lhm_gpu_used = True
            self._register_lhm_sensors(hw_key, hw_row)
        log.info("LHM discovery: %d sensors (GPU via NVAPI: %s)",
                 len(self._sensors), self._lhm_gpu_used)

    def _register_lhm_sensors(self, hw_key: str, hw_row: Any) -> None:
        """Register every sensor that belongs to one Hardware row.

        ``hw_row`` is a WMI Hardware result; sensors are queried by parent
        identifier so the relationship survives the round-trip through WMI.
        """
        hw_name = str(hw_row.Name)
        ns = self._lhm.namespace
        if ns is None:
            return
        for sensor in ns.Sensor(Parent=hw_row.Identifier):
            s_type = str(sensor.SensorType)
            s_name = str(sensor.Name)
            if not (mapping := _LHM_TYPE_MAP.get(s_type)):
                continue
            category, unit = mapping
            sid = f'lhm:{hw_key}:{s_name.lower().replace(" ", "_")}'
            self._sensors.append(SensorInfo(sid, f'{hw_name} {s_name}', category, unit, 'lhm'))

    def _discover_wmi(self) -> None:
        try:
            from ._windows_wmi import wmi_handle
            w = wmi_handle(namespace='root\\WMI')
            try:
                for tz in w.MSAcpi_ThermalZoneTemperature():
                    sid = f'wmi:thermal:{tz.InstanceName}'
                    self._sensors.append(SensorInfo(sid, 'Thermal Zone', 'temperature', '°C', 'wmi'))
            except Exception:
                log.debug("WMI thermal zones not accessible")
        except ImportError:
            log.debug("wmi package not available")
        except Exception:
            log.debug("WMI sensor discovery failed")

    def _poll_platform(self, readings: dict[str, float]) -> None:
        if hasattr(psutil, 'sensors_temperatures'):
            temps = psutil.sensors_temperatures()
            for chip, entries in temps.items():
                for i, entry in enumerate(entries):
                    readings[f'psutil:temp:{chip}:{i}'] = entry.current
        if self._lhm.namespace is not None:
            self._poll_lhm(readings)

    def _poll_lhm(self, readings: dict[str, float]) -> None:
        """Read LHM sensor values via WMI into the readings dict."""
        ns = self._lhm.namespace
        if ns is None:
            return
        try:
            for hw_key, hw_row in self._walk_lhm_nodes():
                self._read_lhm_node(readings, hw_key, hw_row)
        except Exception:
            log.debug("LHM poll failed", exc_info=True)

    def _read_lhm_node(self, readings: dict[str, float], hw_key: str, hw_row: Any) -> None:
        ns = self._lhm.namespace
        if ns is None:
            return
        for sensor in ns.Sensor(Parent=hw_row.Identifier):
            val = sensor.Value
            if val is None:
                continue
            s_name = str(sensor.Name).lower().replace(' ', '_')
            readings[f'lhm:{hw_key}:{s_name}'] = float(val)

    # ── LHM walker (single chokepoint for all three call sites) ─────────
    @staticmethod
    def _lhm_node_key(hw_row: Any) -> str:
        """Normalize a Hardware row's Name to a sensor-ID-safe key.

        Identical formula to v9.5.x to preserve sensor IDs across the
        pythonnet→WMI migration; existing user theme configs continue to
        match.
        """
        return str(hw_row.Name).lower().replace(' ', '_')[:20]

    def _walk_lhm_nodes(self) -> Iterator[tuple[str, Any]]:
        """Yield ``(hw_key, hw_row)`` for every Hardware row from WMI.

        SubHardware items are exposed as Hardware rows with non-empty
        ``Parent``; iterating ``Hardware()`` returns the flat tree.
        Single chokepoint — discovery, polling, and GPU enumeration all
        funnel through here.
        """
        ns = self._lhm.namespace
        if ns is None:
            return
        for hw_row in ns.Hardware():
            yield self._lhm_node_key(hw_row), hw_row

    def get_gpu_list(self) -> list[tuple[str, str]]:
        """Enumerate GPUs on Windows.

        Order of preference:
            1. **LibreHardwareMonitor** — covers NVIDIA/AMD/Intel WITH sensor
               metrics (temp, usage, clock, power). Best UX when present.
            2. **pynvml** — NVIDIA-only, no LHM dependency.
            3. **WMI Win32_VideoController** — universal fallback. Detects
               every GPU Windows knows about (AMD/Intel/NVIDIA, integrated +
               discrete) but offers NO sensor data. The user can still
               select the GPU; metrics show as ``--`` until LHM is added.
               Closes #131 for AMD-on-Windows users without LHM running.
        """
        gpus: list[tuple[str, str]] = []
        try:
            for hw_key, hw_row in self._walk_lhm_nodes():
                if 'Gpu' in str(hw_row.HardwareType):
                    gpus.append((f'lhm:{hw_key}', str(hw_row.Name)))
        except Exception:
            log.debug("LHM GPU enumeration failed", exc_info=True)
        if not gpus:
            gpus = super().get_gpu_list()
        if not gpus:
            gpus = self._wmi_get_gpu_list()
        return gpus

    @staticmethod
    def _wmi_get_gpu_list() -> list[tuple[str, str]]:
        """Enumerate every video controller via ``Win32_VideoController``.

        Microsoft's Win32_VideoController.AdapterRAM is a 32-bit unsigned
        int — it caps at 4 GiB on cards with more VRAM (a known WMI
        limitation, not a code bug). We accept that and just show the
        name; sensor data lives in LHM/ADLX, not here.
        """
        try:
            from ._windows_wmi import wmi_handle
            w = wmi_handle()
            return [
                (f'wmi:{i}', str(vc.Name).strip() or f'GPU {i}')
                for i, vc in enumerate(w.Win32_VideoController())
                if vc.Name
            ]
        except ImportError:
            log.debug("wmi package unavailable — no WMI GPU enumeration")
            return []
        except Exception:
            log.exception("WMI Win32_VideoController query failed")
            return []

    def _build_mapping(self) -> dict[str, str]:
        sensors = self._sensors
        _ff = self._find_first
        mapping: dict[str, str] = {}
        self._map_common(mapping)

        mapping['cpu_temp'] = (
            _ff(sensors, source='lhm', name_contains='Package', category='temperature')
            or _ff(sensors, source='lhm', name_contains='CPU', category='temperature')
            or _ff(sensors, source='psutil', category='temperature')
        )
        mapping['cpu_power'] = (
            _ff(sensors, source='lhm', name_contains='Package', category='power')
            or _ff(sensors, source='lhm', name_contains='CPU', category='power')
        )

        lhm_gpu_temp = _ff(sensors, source='lhm', name_contains='GPU', category='temperature')
        nvidia_gpu_temp = _ff(sensors, source='nvidia', category='temperature')
        if lhm_gpu_temp:
            mapping['gpu_temp'] = lhm_gpu_temp
            mapping['gpu_usage'] = _ff(sensors, source='lhm', name_contains='GPU', category='usage')
            mapping['gpu_clock'] = _ff(sensors, source='lhm', name_contains='GPU', category='clock')
            mapping['gpu_power'] = _ff(sensors, source='lhm', name_contains='GPU', category='power')
        elif nvidia_gpu_temp:
            mapping['gpu_temp'] = nvidia_gpu_temp
            mapping['gpu_usage'] = _ff(sensors, source='nvidia', category='gpu_busy')
            mapping['gpu_clock'] = _ff(sensors, source='nvidia', category='clock')
            mapping['gpu_power'] = _ff(sensors, source='nvidia', category='power')
        else:
            mapping['gpu_temp'] = ''
            mapping['gpu_usage'] = ''
            mapping['gpu_clock'] = ''
            mapping['gpu_power'] = ''

        mapping['mem_temp'] = _ff(sensors, source='lhm', name_contains='Memory', category='temperature')
        mapping['disk_temp'] = (
            _ff(sensors, source='lhm', name_contains='Drive', category='temperature')
            or _ff(sensors, source='lhm', name_contains='SSD', category='temperature')
            or _ff(sensors, source='lhm', name_contains='NVMe', category='temperature')
        )

        self._map_fans(mapping, fan_sources=('lhm', 'nvidia'))
        return mapping


# =========================================================================
# WindowsPlatform — THE one class
# =========================================================================

class WindowsPlatform(Platform):
    """Windows Platform — all OS logic inline."""

    def __init__(self) -> None:
        super().__init__()

    # ── Sensor enumerator ─────────────────────────────────────

    def _make_sensor_enumerator(self) -> SensorEnumerator:
        return SensorEnumerator()

    # ── Device detection ──────────────────────────────────────

    def create_detect_fn(self):
        from trcc.adapters.device.windows.detector import WindowsDeviceDetector
        return WindowsDeviceDetector.detect

    # ── Transport creation ────────────────────────────────────

    def create_scsi_transport(self, path: str, vid: int = 0, pid: int = 0) -> Any:
        from trcc.adapters.device.windows.scsi import WindowsScsiTransport
        return WindowsScsiTransport(path)

    # ── Screen capture ────────────────────────────────────────

    def _screen_capture_format(self) -> str | None:
        return 'gdigrab'

    # ── Directories (override) ────────────────────────────────

    def resolve_assets_dir(self, pkg_dir: Any) -> Any:
        return _copy_assets_to_user_dir(pkg_dir)

    # ── Autostart (winreg) ────────────────────────────────────

    def autostart_enable(self) -> None:
        from trcc.core.ports import AutostartManager
        wr = _winreg()
        exec_path = AutostartManager.get_exec()
        key = wr.OpenKey(wr.HKEY_CURRENT_USER, _REG_KEY, 0, wr.KEY_SET_VALUE)
        wr.SetValueEx(key, _REG_VALUE, 0, wr.REG_SZ, f'"{exec_path}" gui --resume')
        wr.CloseKey(key)
        log.info("Autostart enabled: HKCU\\%s", _REG_KEY)

    def autostart_disable(self) -> None:
        try:
            wr = _winreg()
            key = wr.OpenKey(wr.HKEY_CURRENT_USER, _REG_KEY, 0, wr.KEY_SET_VALUE)
            wr.DeleteValue(key, _REG_VALUE)
            wr.CloseKey(key)
        except OSError:
            pass
        log.info("Autostart disabled")

    def autostart_enabled(self) -> bool:
        try:
            wr = _winreg()
            key = wr.OpenKey(wr.HKEY_CURRENT_USER, _REG_KEY, 0, wr.KEY_READ)
            wr.QueryValueEx(key, _REG_VALUE)
            wr.CloseKey(key)
            return True
        except OSError:
            return False

    def acquire_instance_lock(self) -> object | None:
        import msvcrt  # pyright: ignore[reportMissingImports]
        lock_path = Path(self.config_dir()) / "trcc-linux.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fh = open(lock_path, "w")
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)  # pyright: ignore[reportAttributeAccessIssue]
            fh.write(str(os.getpid()))
            fh.flush()
            return fh
        except OSError:
            return None

    def raise_existing_instance(self) -> None:
        pass  # No SIGUSR1 on Windows

    def configure_dpi(self) -> None:
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # type: ignore[attr-defined]
        except (OSError, AttributeError) as e:
            log.debug("SetProcessDpiAwareness failed (older Windows or missing shcore): %s", e)

    def minimize_on_close(self) -> bool:
        return True

    def configure_stdout(self) -> None:
        import io
        import sys as _sys
        if hasattr(_sys.stdout, 'buffer'):
            _sys.stdout = io.TextIOWrapper(_sys.stdout.buffer, encoding='utf-8', errors='replace')
        if hasattr(_sys.stderr, 'buffer'):
            _sys.stderr = io.TextIOWrapper(_sys.stderr.buffer, encoding='utf-8', errors='replace')

    # ── Administration ────────────────────────────────────────

    def get_pkg_manager(self) -> str | None:
        return 'winget' if shutil.which('winget') else None

    def check_deps(self) -> list:
        from trcc.adapters.infra.doctor import check_system_deps
        return check_system_deps(self.get_pkg_manager())

    def install_rules(self) -> int:
        """Print Zadig-based WinUSB driver installation guide."""
        from trcc.core.models import BULK_DEVICES, HID_LCD_DEVICES, LED_DEVICES, LY_DEVICES

        winusb_vids: set[tuple[int, int]] = set()
        for registry in (BULK_DEVICES, HID_LCD_DEVICES, LED_DEVICES, LY_DEVICES):
            for vid, pid in registry:
                winusb_vids.add((vid, pid))

        print("\n  TRCC WinUSB Driver Setup\n")
        print("  SCSI devices use the default USB Mass Storage driver — no setup needed.\n")
        print("  HID, Bulk, and LY devices need the WinUSB driver.")
        print("  Install via Zadig (https://zadig.akeo.ie/):\n")
        print("  1. Run Zadig → Options → List All Devices")
        print("  2. Select your Thermalright device")
        print("  3. Set target driver to WinUSB")
        print("  4. Click 'Replace Driver'")
        print("  5. Replug the USB device\n")
        print("  Devices that need WinUSB:")
        for vid, pid in sorted(winusb_vids):
            for registry in (BULK_DEVICES, HID_LCD_DEVICES, LED_DEVICES, LY_DEVICES):
                if (vid, pid) in registry:
                    print(f"    {vid:04X}:{pid:04X}  {registry[(vid, pid)].product}")
                    break
        print()
        return 0

    def check_permissions(self, devices: list) -> list[str]:
        return []

    def get_system_files(self) -> list[str]:
        return []

    # ── Identity ──────────────────────────────────────────────

    def distro_name(self) -> str:
        import platform
        return f"Windows {platform.version()}"

    def no_devices_hint(self) -> str | None:
        return (
            "\nOn Windows, non-SCSI devices (HID, Bulk, LY) need the\n"
            "WinUSB driver. Run 'trcc setup-winusb' for instructions."
        )

    def doctor_config(self) -> DoctorPlatformConfig:
        return DoctorPlatformConfig(
            distro_name=self.distro_name(),
            pkg_manager=self.get_pkg_manager(),
            check_libusb=False,
            extra_binaries=[],
            run_gpu_check=False,
            run_udev_check=False,
            run_selinux_check=False,
            run_rapl_check=False,
            run_polkit_check=False,
            run_winusb_check=True,
            enable_ansi=True,
        )

    def report_config(self) -> ReportPlatformConfig:
        return ReportPlatformConfig(
            distro_name=self.distro_name(),
            collect_lsusb=False,
            collect_udev=False,
            collect_selinux=False,
            collect_rapl=False,
            collect_device_permissions=False,
        )

    # ── Setup operations ──────────────────────────────────────

    def run_setup(self, auto_yes: bool = False) -> int:
        from trcc.adapters.infra.doctor import check_system_deps

        pm = self.get_pkg_manager()
        print(f"\n  TRCC Setup — {self.distro_name()}\n")
        actions: list[str] = []

        print("  Step 1/2: System dependencies")
        deps = check_system_deps(pm)
        for dep in deps:
            if dep.ok:
                ver = f" {dep.version}" if dep.version else ""
                print(f"    [OK]  {dep.name}{ver}")
            elif dep.required:
                note = f" ({dep.note})" if dep.note else ""
                print(f"    [!!]  {dep.name} — MISSING{note}")
                if dep.install_cmd and pm:
                    if _confirm(f"Install? -> {dep.install_cmd}", auto_yes):
                        result = subprocess.run(dep.install_cmd.split(), capture_output=True)
                        if result.returncode == 0:
                            actions.append(f"Installed: {dep.name}")
                        else:
                            print(f"    [!!] Install failed (exit {result.returncode})")
            else:
                note = f" ({dep.note})" if dep.note else ""
                print(f"    [--]  {dep.name} — not installed{note}")
        print()

        print("  Step 2/2: USB driver")
        print("    SCSI devices use default USB Mass Storage — no setup needed.")
        print("    HID/Bulk/LY devices need WinUSB via Zadig (https://zadig.akeo.ie/).")
        print()

        _print_summary(actions)
        return 0

    # ── Help text ─────────────────────────────────────────────

    def archive_tool_install_help(self) -> str:
        return (
            "7z not found. Install 7-Zip for Windows:\n"
            "  Download from https://7-zip.org/ and install, or run:\n"
            "  winget install 7zip.7zip"
        )

    def ffmpeg_install_help(self) -> str:
        return "ffmpeg not found. Install:\n  winget install Gyan.FFmpeg"

    # ── Hardware info ─────────────────────────────────────────

    def get_memory_info(self) -> list[dict[str, str]]:
        return get_memory_info()

    def get_disk_info(self) -> list[dict[str, str]]:
        return get_disk_info()
