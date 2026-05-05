"""Linux Platform — single file, single class, all Linux logic.

Everything the app needs from the OS lives on LinuxPlatform.
No intermediate classes, no OS names leaking out.
Private helpers and a SensorEnumerator (lifecycle needs its own class)
are scoped to this file.
"""
from __future__ import annotations

import logging
import os
import shutil
import site
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import psutil

from trcc.adapters.infra.data_repository import SysUtils
from trcc.adapters.system._base import (
    NVML_EXC,
    PSUTIL_EXC,
    SUBPROCESS_EXC,
    SensorEnumeratorBase,
    _ensure_nvml,
    pynvml,
)
from trcc.adapters.system._shared import (
    _confirm,
    _posix_acquire_instance_lock,
    _posix_raise_existing_instance,
    _print_summary,
)
from trcc.core.models import SensorInfo
from trcc.core.paths import _TRCC_PKG
from trcc.core.platform import is_root
from trcc.core.ports import (
    AutostartManager,
    DoctorPlatformConfig,
    Platform,
    ReportPlatformConfig,
)

log = logging.getLogger(__name__)


# =========================================================================
# Private constants
# =========================================================================

_AUTOSTART_DIR = Path.home() / '.config' / 'autostart'
_AUTOSTART_FILE = _AUTOSTART_DIR / 'trcc-linux.desktop'
_LEGACY_AUTOSTART = _AUTOSTART_DIR / 'trcc.desktop'


class LinuxAutostartManager(AutostartManager):
    """XDG autostart — used by tests and BSD platform."""

    def is_enabled(self) -> bool:
        return _AUTOSTART_FILE.exists()

    def enable(self) -> None:
        _AUTOSTART_DIR.mkdir(parents=True, exist_ok=True)
        _AUTOSTART_FILE.write_text(self._desktop_entry())
        log.info("Autostart enabled: %s", _AUTOSTART_FILE)

    def disable(self) -> None:
        if _AUTOSTART_FILE.exists():
            _AUTOSTART_FILE.unlink()
        log.info("Autostart disabled")

    def refresh(self) -> None:
        if not _AUTOSTART_FILE.exists():
            return
        expected = self._desktop_entry()
        if _AUTOSTART_FILE.read_text() != expected:
            _AUTOSTART_FILE.write_text(expected)
            log.info("Autostart refreshed: %s", _AUTOSTART_FILE)

    def ensure(self) -> bool:
        if _LEGACY_AUTOSTART.exists():
            _LEGACY_AUTOSTART.unlink()
            log.info("Removed legacy autostart file: %s", _LEGACY_AUTOSTART)
        return super().ensure()

    def _desktop_entry(self) -> str:
        exec_path = self.get_exec()
        return (
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=TRCC Linux\n"
            "Comment=Thermalright LCD Control Center\n"
            f"Exec={exec_path} gui --resume\n"
            "Icon=trcc\n"
            "Terminal=false\n"
            "Categories=Utility;System;\n"
            "StartupWMClass=trcc-linux\n"
            "X-GNOME-Autostart-enabled=true\n"
        )


_DMI_MEMORY_FIELDS = {
    'manufacturer', 'part_number', 'type', 'speed',
    'configured_memory_speed', 'size', 'locator', 'form_factor',
    'rank', 'data_width', 'total_width', 'configured_voltage',
    'minimum_voltage', 'maximum_voltage', 'memory_technology',
}
_POLKIT_POLICY = '/usr/share/polkit-1/actions/com.github.lexonight1.trcc.policy'

_HWMON_TYPES = {
    'temp': ('temperature', '°C'),
    'fan': ('fan', 'RPM'),
    'in': ('voltage', 'V'),
    'power': ('power', 'W'),
    'freq': ('clock', 'MHz'),
}
_HWMON_DIVISORS = {
    'temp': 1000.0,
    'fan': 1.0,
    'in': 1000.0,
    'power': 1000000.0,
    'freq': 1000000.0,
}
_GPU_VENDOR_NVIDIA = '10de'
_GPU_VENDOR_AMD = '1002'
_GPU_VENDOR_INTEL = '8086'


# =========================================================================
# Private helper functions
# =========================================================================

def _real_user_home() -> Path:
    """Return the real (non-root) user's home directory."""
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user and sudo_user != "root":
        import pwd
        return Path(pwd.getpwnam(sudo_user).pw_dir)
    return Path.home()


def _privileged_cmd(binary: str, args: list[str]) -> list[str]:
    """Build command with pkexec elevation when polkit policy is installed."""
    if hasattr(os, 'geteuid') and os.geteuid() == 0:
        return [binary, *args]
    full_path = shutil.which(binary)
    if full_path and os.path.isfile(_POLKIT_POLICY) and shutil.which('pkexec'):
        return ['pkexec', full_path, *args]
    return [binary, *args]


def _detect_gpu_vendors() -> list[str]:
    """Detect GPU vendors via PCI sysfs, discrete first."""
    pci_base = Path('/sys/bus/pci/devices')
    if not pci_base.exists():
        return []

    vendors: list[str] = []
    for dev_dir in pci_base.iterdir():
        class_path = dev_dir / 'class'
        vendor_path = dev_dir / 'vendor'
        if not class_path.exists() or not vendor_path.exists():
            continue
        try:
            pci_class = class_path.read_text().strip()
            if not (pci_class.startswith(('0x0300', '0x0302'))):
                continue
            vendor = vendor_path.read_text().strip().removeprefix('0x')
            if vendor not in vendors:
                vendors.append(vendor)
        except OSError:
            continue

    priority = {_GPU_VENDOR_NVIDIA: 0, _GPU_VENDOR_AMD: 1, _GPU_VENDOR_INTEL: 2}
    vendors.sort(key=lambda v: priority.get(v, 99))
    return vendors


def _autostart_desktop_entry() -> str:
    """Generate XDG .desktop autostart content."""
    exec_path = AutostartManager.get_exec()
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=TRCC Linux\n"
        "Comment=Thermalright LCD Control Center\n"
        f"Exec={exec_path} gui --resume\n"
        "Icon=trcc\n"
        "Terminal=false\n"
        "Categories=Utility;System;\n"
        "StartupWMClass=trcc-linux\n"
        "X-GNOME-Autostart-enabled=true\n"
    )


def _get_smart_health(dev_name: str) -> str | None:
    """Get SMART health status via smartctl."""
    try:
        result = subprocess.run(
            _privileged_cmd('smartctl', ['-H', f'/dev/{dev_name}']),
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            if 'overall-health' in line.lower() or 'health status' in line.lower():
                if 'PASSED' in line:
                    return 'PASSED'
                if 'FAILED' in line:
                    return 'FAILED'
    except SUBPROCESS_EXC as e:
        log.debug("smartctl health probe failed for %s: %s", dev_name, e)
    return None


# =========================================================================
# Module-level setup functions (importable for sudo dispatch)
# =========================================================================

_SUDO_DISPATCH: dict[str, str] = {
    "setup-udev": "from trcc.adapters.system.linux_platform import setup_udev; exit(setup_udev())",
    "setup-selinux": "from trcc.adapters.system.linux_platform import setup_selinux; exit(setup_selinux())",
    "setup-polkit": "from trcc.adapters.system.linux_platform import setup_polkit; exit(setup_polkit())",
}


def sudo_reexec(subcommand: str) -> int:
    """Re-exec a setup function as root via sudo with correct PYTHONPATH."""
    paths: list[str] = []
    paths.extend(site.getsitepackages())
    paths.append(site.getusersitepackages())
    trcc_pkg = str(Path(__file__).resolve().parents[3])
    paths.append(trcc_pkg)
    pythonpath = os.pathsep.join(paths)

    path_inject = f"import sys; sys.path[:0] = {paths!r}; "

    if (snippet := _SUDO_DISPATCH.get(subcommand)):
        cmd = ["sudo", sys.executable, "-c", path_inject + snippet]
    else:
        cmd = [
            "sudo", "env", f"PYTHONPATH={pythonpath}",
            sys.executable, "-m", "trcc.cli", subcommand,
        ]

    print("Root required — requesting sudo...")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        print(f"\n  sudo re-exec failed (exit {result.returncode}).")
        print(f"  Try running directly:  sudo trcc {subcommand}")
        print(f"  Or with full path:     sudo {sys.executable} -m trcc.cli {subcommand}")
    return result.returncode


def setup_rapl_permissions() -> None:
    """Make Intel/AMD RAPL energy counters readable by non-root users."""
    rapl_base = Path("/sys/class/powercap")
    if not rapl_base.exists():
        return

    energy_files = sorted(rapl_base.glob("intel-rapl:*/energy_uj"))
    if not energy_files:
        return

    tmpfiles_path = "/etc/tmpfiles.d/trcc-rapl.conf"
    lines = [
        "# Thermalright TRCC — allow non-root CPU power reading (RAPL)",
        "# Auto-generated by trcc setup-udev",
    ]
    for energy_file in energy_files:
        lines.append(f"z {energy_file} 0444 root root -")
    tmpfiles_content = "\n".join(lines) + "\n"

    with open(tmpfiles_path, "w") as f:
        f.write(tmpfiles_content)
    print(f"Wrote {tmpfiles_path}")

    made_readable = 0
    for energy_file in energy_files:
        try:
            energy_file.chmod(0o444)
            made_readable += 1
        except OSError as e:
            print(f"  WARNING: could not chmod {energy_file}: {e}")
    if made_readable == len(energy_files):
        print(f"RAPL power sensors: {made_readable} domain(s) made readable")
    else:
        print(f"RAPL power sensors: {made_readable}/{len(energy_files)} domain(s) made readable"
              f" — {len(energy_files) - made_readable} failed (tmpfiles.d will fix on next boot)")

    restorecon = subprocess.run(
        ["which", "restorecon"], capture_output=True, text=True,
    )
    if restorecon.returncode == 0:
        subprocess.run(
            ["restorecon", tmpfiles_path], capture_output=True, check=False,
        )


def setup_udev(dry_run: bool = False) -> int:
    """Generate and install udev rules + USB storage quirks from KNOWN_DEVICES."""
    from trcc.core.models import ALL_DEVICES, PROTOCOL_TRAITS, SCSI_DEVICES

    rules_path = "/etc/udev/rules.d/99-trcc-lcd.rules"
    rules_lines = ["# Thermalright LCD/LED cooler devices — auto-generated by trcc setup-udev"]

    for (vid, pid), info in sorted(ALL_DEVICES.items()):
        traits = PROTOCOL_TRAITS.get(info.protocol, PROTOCOL_TRAITS['scsi'])
        rule_parts = [f'# {info.vendor} {info.product}']
        for subsystem in traits.udev_subsystems:
            attr = 'ATTRS' if subsystem in ('hidraw', 'scsi_generic') else 'ATTR'
            rule_parts.append(
                f'SUBSYSTEM=="{subsystem}", '
                f'{attr}{{idVendor}}=="{vid:04x}", '
                f'{attr}{{idProduct}}=="{pid:04x}", '
                f'MODE="0666"'
            )
        rule_parts.append(
            f'ACTION=="add", SUBSYSTEM=="usb", '
            f'ATTR{{idVendor}}=="{vid:04x}", '
            f'ATTR{{idProduct}}=="{pid:04x}", '
            f'ATTR{{power/autosuspend}}="-1"'
        )
        rules_lines.append('\n'.join(rule_parts))

    rules_content = "\n\n".join(rules_lines) + "\n"

    quirk_entries = [f"{vid:04x}:{pid:04x}:u" for vid, pid in sorted(SCSI_DEVICES)]
    quirks_param = ",".join(quirk_entries)

    modprobe_path = "/etc/modprobe.d/trcc-lcd.conf"
    modprobe_content = (
        "# Thermalright LCD — force usb-storage bulk-only (bypass UAS)\n"
        "# Without this, devices are ignored and /dev/sgX is never created\n"
        "# Auto-generated by trcc setup-udev\n"
        f"options usb-storage quirks={quirks_param}\n"
    )

    if dry_run:
        print("=== udev rules ===")
        print(rules_content)
        print(f"# Would write to {rules_path}\n")
        print("=== usb-storage quirks ===")
        print(modprobe_content)
        print(f"# Would write to {modprobe_path}\n")
        print("=== sg module autoload ===")
        print("sg")
        print("# Would write to /etc/modules-load.d/trcc-sg.conf")
        return 0

    if not is_root():
        return sudo_reexec("setup-udev")

    with open(rules_path, "w") as f:
        f.write(rules_content)
    print(f"Wrote {rules_path}")

    with open(modprobe_path, "w") as f:
        f.write(modprobe_content)
    print(f"Wrote {modprobe_path}")

    quirks_sysfs = "/sys/module/usb_storage/parameters/quirks"
    if os.path.exists(quirks_sysfs):
        with open(quirks_sysfs, "w") as f:
            f.write(quirks_param)
        print(f"Applied quirks: {quirks_param}")

    modules_load_path = "/etc/modules-load.d/trcc-sg.conf"
    modules_load_content = (
        "# Thermalright LCD — ensure SCSI generic (/dev/sgX) is available\n"
        "# Without this, some distros only create /dev/sdX for USB mass storage\n"
        "# Auto-generated by trcc setup-udev\n"
        "sg\n"
    )
    with open(modules_load_path, "w") as f:
        f.write(modules_load_content)
    print(f"Wrote {modules_load_path}")

    subprocess.run(["modprobe", "sg"], check=False, capture_output=True)
    setup_rapl_permissions()
    subprocess.run(["udevadm", "control", "--reload-rules"], check=False)
    subprocess.run(["udevadm", "trigger"], check=False)
    print("\nDone. Unplug and replug the USB cable (or reboot if it's not easily accessible).")
    return 0


def setup_selinux() -> int:
    """Install SELinux policy module allowing USB device access."""
    import tempfile

    if not is_root():
        return sudo_reexec("setup-selinux")

    try:
        r = subprocess.run(["getenforce"], capture_output=True, text=True, timeout=5)
        status = r.stdout.strip().lower()
    except FileNotFoundError:
        print("SELinux not installed — nothing to do.")
        return 0

    if status != 'enforcing':
        print(f"SELinux is {status} — no policy needed.")
        return 0

    try:
        r = subprocess.run(["semodule", "-l"], capture_output=True, text=True, timeout=10)
        if 'trcc_usb' in r.stdout:
            print("SELinux module trcc_usb already loaded.")
            return 0
    except FileNotFoundError:
        print("semodule not found — cannot manage SELinux policies.")
        return 1

    from trcc.adapters.infra.doctor import _detect_pkg_manager, _install_hint
    pm = _detect_pkg_manager()

    missing: list[str] = []
    for tool in ('checkmodule', 'semodule_package'):
        if not shutil.which(tool):
            missing.append(tool)
    if missing:
        for tool in missing:
            print(f"  {tool} not found — {_install_hint(tool, pm)}")
        return 1

    te_src = os.path.join(_TRCC_PKG, 'data', 'trcc_usb.te')
    if not os.path.isfile(te_src):
        print(f"SELinux policy source not found: {te_src}")
        return 1

    try:
        with tempfile.TemporaryDirectory() as tmp:
            te_path = os.path.join(tmp, 'trcc_usb.te')
            mod_path = os.path.join(tmp, 'trcc_usb.mod')
            pp_path = os.path.join(tmp, 'trcc_usb.pp')
            shutil.copy2(te_src, te_path)

            r = subprocess.run(
                ['checkmodule', '-M', '-m', '-o', mod_path, te_path],
                capture_output=True, text=True,
            )
            if r.returncode != 0:
                print(f"checkmodule failed: {r.stderr.strip()}")
                return 1

            r = subprocess.run(
                ['semodule_package', '-o', pp_path, '-m', mod_path],
                capture_output=True, text=True,
            )
            if r.returncode != 0:
                print(f"semodule_package failed: {r.stderr.strip()}")
                return 1

            r = subprocess.run(
                ['semodule', '-i', pp_path],
                capture_output=True, text=True,
            )
            if r.returncode != 0:
                print(f"semodule install failed: {r.stderr.strip()}")
                return 1

        print("Installed SELinux module trcc_usb (USB device access for TRCC).")
        return 0

    except Exception as e:
        log.exception("setup_selinux failed")
        print(f"Error installing SELinux policy: {e}")
        return 1


def install_desktop() -> int:
    """Install .desktop menu entry and icon for app launchers."""
    home = _real_user_home()
    app_dir = home / ".local" / "share" / "applications"

    pkg_root = Path(_TRCC_PKG)
    icon_pkg_dir = pkg_root / "assets" / "icons"
    desktop_src = pkg_root / "assets" / "trcc-linux.desktop"

    app_dir.mkdir(parents=True, exist_ok=True)
    desktop_dst = app_dir / "trcc-linux.desktop"
    if desktop_src.exists():
        shutil.copy2(desktop_src, desktop_dst)
    else:
        desktop_dst.write_text(
            "[Desktop Entry]\nName=TRCC Linux\n"
            "Comment=Thermalright LCD Control Center\nExec=trcc gui\n"
            "Icon=trcc\nTerminal=false\nType=Application\n"
            "Categories=Utility;System;\n"
            "Keywords=thermalright;lcd;cooler;aio;cpu;\n"
            "StartupWMClass=trcc-linux\n"
        )
    print(f"Installed {desktop_dst}")

    installed_icon = False
    for size in [256, 128, 64, 48, 32, 24, 16]:
        icon_src = icon_pkg_dir / f"trcc_{size}x{size}.png"
        if icon_src.exists():
            icon_dir = home / ".local" / "share" / "icons" / "hicolor" / f"{size}x{size}" / "apps"
            icon_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(icon_src, icon_dir / "trcc.png")
            installed_icon = True

    if installed_icon:
        subprocess.run(
            ["gtk-update-icon-cache", str(home / ".local" / "share" / "icons" / "hicolor")],
            check=False, capture_output=True,
        )
    else:
        print("Warning: icons not found, menu entry will use a generic icon")

    print("\nTRCC should now appear in your application menu.")
    print("If it doesn't show up immediately, log out and back in.")
    return 0


def setup_polkit() -> int:
    """Install polkit policy for passwordless dmidecode/smartctl access."""
    if not is_root():
        return sudo_reexec("setup-polkit")

    pkg_root = Path(__file__).parent.parent.parent
    policy_src = pkg_root / "assets" / "com.github.lexonight1.trcc.policy"

    if not policy_src.exists():
        print(f"Policy file not found: {policy_src}")
        return 1

    policy_text = policy_src.read_text()
    for binary in ('dmidecode', 'smartctl'):
        found = shutil.which(binary)
        if found:
            real_path = os.path.realpath(found)
            policy_text = policy_text.replace(f'/usr/bin/{binary}', real_path)

    policy_dst = Path("/usr/share/polkit-1/actions/com.github.lexonight1.trcc.policy")
    policy_dst.parent.mkdir(parents=True, exist_ok=True)
    policy_dst.write_text(policy_text)

    invoking_user = os.environ.get('SUDO_USER', '')
    if invoking_user:
        rules_dst = Path("/etc/polkit-1/rules.d/50-trcc.rules")
        rules_dst.parent.mkdir(parents=True, exist_ok=True)
        rules_dst.write_text(
            '// TRCC Linux — passwordless dmidecode/smartctl for installing user\n'
            'polkit.addRule(function(action, subject) {\n'
            '    if ((action.id == "com.github.lexonight1.trcc.dmidecode" ||\n'
            '         action.id == "com.github.lexonight1.trcc.smartctl") &&\n'
            f'        subject.user == "{invoking_user}") {{\n'
            '        return polkit.Result.YES;\n'
            '    }\n'
            '});\n'
        )
        print(f"Installed {rules_dst} (user: {invoking_user})")

    restore_paths = [str(policy_dst)]
    if invoking_user:
        restore_paths.append(str(rules_dst))
    if shutil.which('restorecon'):
        subprocess.run(['restorecon', *restore_paths], check=False)
    print(f"Installed {policy_dst}")
    print(f"User '{invoking_user}' can now run dmidecode/smartctl without a password.")
    return 0


def get_memory_info() -> list[dict[str, str]]:
    """Get DRAM slot info via dmidecode. Falls back to psutil for totals."""
    log.debug("get_memory_info: querying dmidecode")
    slots: list[dict[str, str]] = []
    try:
        result = subprocess.run(
            _privileged_cmd('dmidecode', ['-t', 'memory']),
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            current: dict[str, str] = {}
            for line in result.stdout.splitlines():
                line = line.strip()
                if line.startswith('Memory Device'):
                    if current.get('size') and current['size'] != 'No Module Installed':
                        slots.append(current)
                    current = {}
                elif ':' in line:
                    key, _, val = line.partition(':')
                    val = val.strip()
                    key = key.strip().lower().replace(' ', '_')
                    if key in _DMI_MEMORY_FIELDS:
                        current[key] = val
            if current.get('size') and current['size'] != 'No Module Installed':
                slots.append(current)
    except SUBPROCESS_EXC as e:
        log.debug("dmidecode probe failed: %s", e)

    log.debug("get_memory_info: found %d populated slots", len(slots))
    if not slots:
        try:
            mem = psutil.virtual_memory()
            total_gb = f"{mem.total / (1024**3):.1f} GB"
            slots.append({'size': total_gb, 'type': 'Unknown',
                          'speed': 'Unknown', 'manufacturer': 'Unknown'})
        except PSUTIL_EXC as e:
            log.debug("psutil.virtual_memory fallback failed: %s", e)
    return slots


def get_disk_info() -> list[dict[str, str]]:
    """Get disk info via lsblk + smartctl."""
    log.debug("get_disk_info: querying lsblk")
    disks: list[dict[str, str]] = []
    try:
        import json as _json
        result = subprocess.run(
            ['lsblk', '-J', '-o', 'NAME,MODEL,SIZE,TYPE,ROTA'],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            data = _json.loads(result.stdout)
            for dev in data.get('blockdevices', []):
                if dev.get('type') != 'disk' or not dev.get('model'):
                    continue
                disk_type = 'HDD' if dev.get('rota') else 'SSD'
                disk = {
                    'name': dev.get('name', ''),
                    'model': dev.get('model', 'Unknown').strip(),
                    'size': dev.get('size', 'Unknown'),
                    'type': disk_type,
                }
                if (health := _get_smart_health(dev['name'])):
                    disk['health'] = health
                disks.append(disk)
    except SUBPROCESS_EXC as e:
        log.debug("lsblk probe failed: %s", e)
    log.debug("get_disk_info: found %d disks", len(disks))
    return disks


# =========================================================================
# SensorEnumerator — file-scoped, no OS prefix
# =========================================================================

class SensorEnumerator(SensorEnumeratorBase):
    """Linux hardware sensor discovery and reading.

    Sources: hwmon, pynvml, DRM sysfs, psutil, Intel RAPL.
    """

    def __init__(self) -> None:
        super().__init__()
        self._hwmon_paths: dict[str, str] = {}
        self._drm_paths: dict[str, str] = {}
        self._rapl_paths: dict[str, str] = {}
        self._rapl_prev: dict[str, tuple[float, float]] = {}

    def discover(self) -> list[SensorInfo]:
        self._sensors = []
        self._hwmon_paths = {}
        self._nvidia_handles = {}
        self._drm_paths = {}
        self._rapl_paths = {}

        self._discover_hwmon()
        self._discover_nvidia()
        self._discover_drm()
        self._discover_psutil()
        self._discover_rapl()
        self._discover_computed()

        return self._sensors

    def _discover_psutil(self) -> None:
        self._discover_psutil_base()
        self._sensors.append(
            SensorInfo('psutil:mem_available', 'Memory / Available', 'other', 'MB', 'psutil'),
        )

    def _discover_nvidia(self) -> None:
        if not _ensure_nvml() or pynvml is None:
            return
        try:
            count = pynvml.nvmlDeviceGetCount()
        except NVML_EXC as e:
            log.debug("nvmlDeviceGetCount failed: %s", e)
            return

        for i in range(count):
            try:
                handle = pynvml.nvmlDeviceGetHandleByIndex(i)
                gpu_name = pynvml.nvmlDeviceGetName(handle)
                if isinstance(gpu_name, bytes):
                    gpu_name = gpu_name.decode()
                gpu_name = str(gpu_name)
            except NVML_EXC as e:
                log.warning("NVIDIA GPU %d handle/name failed — skipping: %s", i, e)
                continue

            self._nvidia_handles[i] = handle
            prefix = f"nvidia:{i}"
            label = gpu_name if count == 1 else f"GPU {i} ({gpu_name})"

            for metric, name, cat, unit in [
                ('temp', f'{label} / Temperature', 'temperature', '°C'),
                ('gpu_util', f'{label} / GPU Utilization', 'usage', '%'),
                ('mem_util', f'{label} / Memory Utilization', 'usage', '%'),
                ('clock', f'{label} / Graphics Clock', 'clock', 'MHz'),
                ('mem_clock', f'{label} / Memory Clock', 'clock', 'MHz'),
                ('power', f'{label} / Power Draw', 'power', 'W'),
                ('vram_used', f'{label} / VRAM Used', 'other', 'MB'),
                ('vram_total', f'{label} / VRAM Total', 'other', 'MB'),
                ('fan', f'{label} / Fan Speed', 'fan', '%'),
            ]:
                self._sensors.append(SensorInfo(
                    id=f"{prefix}:{metric}", name=name,
                    category=cat, unit=unit, source='nvidia',
                ))

    def _discover_hwmon(self) -> None:
        hwmon_base = Path('/sys/class/hwmon')
        if not hwmon_base.exists():
            return

        driver_counts: dict[str, int] = {}

        for hwmon_dir in sorted(hwmon_base.iterdir()):
            driver_name = SysUtils.read_sysfs(str(hwmon_dir / 'name')) or hwmon_dir.name

            driver_counts[driver_name] = driver_counts.get(driver_name, 0) + 1
            if driver_counts[driver_name] > 1:
                driver_key = f"{driver_name}.{driver_counts[driver_name] - 1}"
            else:
                driver_key = driver_name

            for input_file in sorted(hwmon_dir.glob('*_input')):
                fname = input_file.name
                input_name = fname.replace('_input', '')

                prefix = None
                for pfx in _HWMON_TYPES:
                    if input_name.startswith(pfx):
                        prefix = pfx
                        break
                if prefix is None:
                    continue

                category, unit = _HWMON_TYPES[prefix]
                label_path = hwmon_dir / f'{input_name}_label'
                if (label := SysUtils.read_sysfs(str(label_path))):
                    name = f'{driver_key} / {label}'
                else:
                    name = f'{driver_key} / {input_name}'

                sensor_id = f'hwmon:{driver_key}:{input_name}'
                self._sensors.append(SensorInfo(
                    id=sensor_id, name=name,
                    category=category, unit=unit, source='hwmon',
                ))
                self._hwmon_paths[sensor_id] = str(input_file)

    def _discover_drm(self) -> None:
        drm_base = Path('/sys/class/drm')
        if not drm_base.exists():
            return

        for card_dir in sorted(drm_base.glob('card[0-9]*')):
            if '-' in card_dir.name:
                continue
            vendor_path = card_dir / 'device' / 'vendor'
            if not vendor_path.exists():
                continue
            try:
                vendor = vendor_path.read_text().strip().removeprefix('0x')
            except OSError:
                continue

            card = card_dir.name

            if vendor == _GPU_VENDOR_AMD:
                busy_path = card_dir / 'device' / 'gpu_busy_percent'
                if busy_path.exists():
                    sid = f"drm:{card}:gpu_busy"
                    self._sensors.append(SensorInfo(
                        id=sid, name=f"GPU / Utilization ({card})",
                        category='usage', unit='%', source='drm',
                    ))
                    self._drm_paths[sid] = str(busy_path)

            if vendor == _GPU_VENDOR_INTEL:
                freq_path = card_dir / 'gt_cur_freq_mhz'
                if freq_path.exists():
                    sid = f"drm:{card}:freq"
                    self._sensors.append(SensorInfo(
                        id=sid, name=f"GPU / Frequency ({card})",
                        category='clock', unit='MHz', source='drm',
                    ))
                    self._drm_paths[sid] = str(freq_path)

    def _discover_rapl(self) -> None:
        rapl_base = Path('/sys/class/powercap')
        try:
            if not rapl_base.exists():
                return
            rapl_dirs = sorted(rapl_base.glob('intel-rapl:*'))
        except OSError as e:
            # /sys/class/powercap exists but isn't traversable for this user
            # (pipx / non-root install w/o `trcc setup-rapl` run yet).
            # Skip RAPL silently — without the setup step the sensor
            # values would be unreadable anyway.  Issue #139.
            log.debug("RAPL discovery skipped: %s", e)
            return

        for rapl_dir in rapl_dirs:
            if ':' in rapl_dir.name.split('intel-rapl:')[1]:
                continue
            energy_path = rapl_dir / 'energy_uj'
            name_path = rapl_dir / 'name'
            try:
                if not energy_path.exists():
                    continue
            except OSError as e:
                # Same permission problem at the per-domain energy_uj
                # level — `Path.exists()` calls os.stat which raises
                # PermissionError when the file isn't readable.
                log.debug("RAPL %s skipped: %s", rapl_dir.name, e)
                continue

            domain_name = SysUtils.read_sysfs(str(name_path)) or rapl_dir.name
            sensor_id = f"rapl:{domain_name}"
            self._sensors.append(SensorInfo(
                id=sensor_id,
                name=f"RAPL / {domain_name.title()} Power",
                category='power', unit='W', source='rapl',
            ))
            self._rapl_paths[sensor_id] = str(energy_path)

    # ── Polling ───────────────────────────────────────────────────────

    def _poll_once(self) -> None:
        readings: dict[str, float] = {}

        for sid, path in self._hwmon_paths.items():
            if (val := SysUtils.read_sysfs(path)) is not None:
                try:
                    raw = float(val)
                    prefix = sid.split(':')[-1]
                    for pfx, div in _HWMON_DIVISORS.items():
                        if prefix.startswith(pfx):
                            readings[sid] = raw / div
                            break
                    else:
                        readings[sid] = raw
                except ValueError:
                    pass

        self._poll_nvidia(readings)
        self._poll_psutil(readings)
        self._poll_rapl(readings)

        for sid, path in self._drm_paths.items():
            if (val := SysUtils.read_sysfs(path)) is not None:
                try:
                    readings[sid] = float(val)
                except ValueError:
                    pass

        self._poll_computed_io(readings)
        self._poll_datetime(readings)

        with self._lock:
            self._readings = readings

    _cpu_freq_cache: float = 0.0
    _cpu_freq_time: float = 0.0
    _CPU_FREQ_TTL: float = 10.0

    def _poll_psutil(self, readings: dict[str, float]) -> None:
        try:
            readings['psutil:cpu_percent'] = psutil.cpu_percent(interval=None)
        except PSUTIL_EXC as e:
            log.debug("psutil.cpu_percent failed: %s", e)
        try:
            now = time.monotonic()
            if now - self._cpu_freq_time >= self._CPU_FREQ_TTL:
                if (freq := psutil.cpu_freq()):
                    self._cpu_freq_cache = freq.current
                else:
                    self._cpu_freq_cache = 0.0
                self._cpu_freq_time = now
            if self._cpu_freq_cache > 0:
                readings['psutil:cpu_freq'] = self._cpu_freq_cache
        except PSUTIL_EXC as e:
            log.debug("psutil.cpu_freq failed: %s", e)
        try:
            mem = psutil.virtual_memory()
            readings['psutil:mem_percent'] = mem.percent
            readings['psutil:mem_available'] = mem.available / (1024 * 1024)
        except PSUTIL_EXC as e:
            log.debug("psutil.virtual_memory failed: %s", e)

    def _poll_nvidia(self, readings: dict[str, float]) -> None:
        if not self._ensure_nvidia_ready() or pynvml is None:
            return
        for i, handle in self._nvidia_handles.items():
            prefix = f"nvidia:{i}"
            try:
                readings[f"{prefix}:temp"] = float(
                    pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU))
            except NVML_EXC as e:
                log.debug("%s:temp poll failed: %s", prefix, e)
            try:
                util = pynvml.nvmlDeviceGetUtilizationRates(handle)
                readings[f"{prefix}:gpu_util"] = float(util.gpu)
                readings[f"{prefix}:mem_util"] = float(util.memory)
            except NVML_EXC as e:
                log.debug("%s:util poll failed: %s", prefix, e)
            try:
                readings[f"{prefix}:clock"] = float(
                    pynvml.nvmlDeviceGetClockInfo(handle, pynvml.NVML_CLOCK_GRAPHICS))
            except NVML_EXC as e:
                log.debug("%s:clock poll failed: %s", prefix, e)
            try:
                readings[f"{prefix}:mem_clock"] = float(
                    pynvml.nvmlDeviceGetClockInfo(handle, pynvml.NVML_CLOCK_MEM))
            except NVML_EXC as e:
                log.debug("%s:mem_clock poll failed: %s", prefix, e)
            try:
                readings[f"{prefix}:power"] = pynvml.nvmlDeviceGetPowerUsage(handle) / 1000.0
            except NVML_EXC as e:
                log.debug("%s:power poll failed: %s", prefix, e)
            try:
                mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
                readings[f"{prefix}:vram_used"] = int(mem.used) / (1024 * 1024)
                readings[f"{prefix}:vram_total"] = int(mem.total) / (1024 * 1024)
            except NVML_EXC as e:
                log.debug("%s:vram poll failed: %s", prefix, e)
            try:
                readings[f"{prefix}:fan"] = float(pynvml.nvmlDeviceGetFanSpeed(handle))
            except NVML_EXC as e:
                log.debug("%s:fan poll failed: %s", prefix, e)

    def _poll_rapl(self, readings: dict[str, float]) -> None:
        now = time.monotonic()
        for sid, path in self._rapl_paths.items():
            val = SysUtils.read_sysfs(path)
            if val is None:
                continue
            try:
                energy_uj = float(val)
            except ValueError:
                continue
            if sid in self._rapl_prev:
                prev_energy, prev_time = self._rapl_prev[sid]
                dt = now - prev_time
                if dt > 0:
                    power_w = (energy_uj - prev_energy) / (dt * 1_000_000)
                    if power_w >= 0:
                        readings[sid] = power_w
            self._rapl_prev[sid] = (energy_uj, now)

    def read_one(self, sensor_id: str) -> float | None:
        if sensor_id in self._hwmon_paths:
            if (val := SysUtils.read_sysfs(self._hwmon_paths[sensor_id])) is not None:
                try:
                    raw = float(val)
                    prefix = sensor_id.split(':')[-1]
                    for pfx, div in _HWMON_DIVISORS.items():
                        if prefix.startswith(pfx):
                            return raw / div
                    return raw
                except ValueError:
                    return None

        if sensor_id in self._drm_paths:
            if (val := SysUtils.read_sysfs(self._drm_paths[sensor_id])) is not None:
                try:
                    return float(val)
                except ValueError:
                    return None

        readings = self.read_all()
        return readings.get(sensor_id)

    # ── Mapping ───────────────────────────────────────────────────────

    def _build_mapping(self) -> dict[str, str]:
        sensors = self._sensors
        _ff = self._find_first
        mapping: dict[str, str] = {}
        self._map_common(mapping)

        mapping['cpu_temp'] = (
            _ff(sensors, source='hwmon', name_contains='Package')
            or _ff(sensors, source='hwmon', name_contains='Tctl')
            or _ff(sensors, source='hwmon', name_contains='coretemp')
            or _ff(sensors, source='hwmon', name_contains='k10temp')
        )
        mapping['cpu_power'] = _ff(sensors, source='rapl')

        gpu = self._best_gpu()
        if gpu.get('vendor') == 'nvidia':
            prefix = f"nvidia:{gpu['nvidia_idx']}"
            mapping['gpu_temp'] = f"{prefix}:temp"
            mapping['gpu_usage'] = f"{prefix}:gpu_util"
            mapping['gpu_clock'] = f"{prefix}:clock"
            mapping['gpu_power'] = f"{prefix}:power"
        elif gpu.get('vendor') == 'amd':
            drv = gpu['hwmon_driver']
            card = gpu['drm_card']
            mapping['gpu_temp'] = _ff(sensors, source='hwmon', name_contains=drv, category='temperature')
            mapping['gpu_usage'] = _ff(sensors, source='drm', name_contains=card, category='usage')
            mapping['gpu_clock'] = _ff(sensors, source='hwmon', name_contains=drv, category='clock')
            mapping['gpu_power'] = _ff(sensors, source='hwmon', name_contains=drv, category='power')
        elif _GPU_VENDOR_INTEL in _detect_gpu_vendors():
            mapping['gpu_temp'] = _ff(sensors, source='hwmon', name_contains='i915', category='temperature')
            mapping['gpu_usage'] = ''
            mapping['gpu_clock'] = _ff(sensors, source='drm', category='clock')
            mapping['gpu_power'] = _ff(sensors, source='hwmon', name_contains='i915', category='power')
        else:
            mapping['gpu_temp'] = ''
            mapping['gpu_usage'] = ''
            mapping['gpu_clock'] = ''
            mapping['gpu_power'] = ''

        mapping['mem_temp'] = _ff(sensors, source='hwmon', name_contains='spd')
        mapping['mem_clock'] = ''

        mapping['disk_temp'] = (
            _ff(sensors, source='hwmon', name_contains='nvme')
            or _ff(sensors, source='hwmon', name_contains='drivetemp')
        )

        self._map_fans(mapping, fan_sources=('hwmon',))
        return mapping

    def get_gpu_list(self) -> list[tuple[str, str]]:
        gpus: list[tuple[str, str, int]] = []

        if _ensure_nvml() and pynvml is not None:
            for idx, handle in self._nvidia_handles.items():
                try:
                    name = pynvml.nvmlDeviceGetName(handle)
                    if isinstance(name, bytes):
                        name = name.decode()
                    name = str(name)
                except NVML_EXC as e:
                    log.debug("nvmlDeviceGetName(idx=%d) failed: %s", idx, e)
                    name = f'GPU {idx}'
                try:
                    mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
                    vram = int(mem.total)
                except NVML_EXC as e:
                    log.debug("nvmlDeviceGetMemoryInfo(idx=%d) failed: %s", idx, e)
                    vram = 0
                vram_mb = vram // (1024 * 1024)
                gpus.append((f'nvidia:{idx}', f'{name} ({vram_mb} MB)', vram))

        drm_base = Path('/sys/class/drm')
        if drm_base.exists():
            for card_dir in sorted(drm_base.glob('card[0-9]*')):
                if '-' in card_dir.name:
                    continue
                vendor_path = card_dir / 'device' / 'vendor'
                if not vendor_path.exists():
                    continue
                try:
                    vendor = vendor_path.read_text().strip().removeprefix('0x')
                except OSError:
                    continue
                if vendor not in (_GPU_VENDOR_AMD, _GPU_VENDOR_INTEL):
                    continue

                card = card_dir.name
                vendor_label = 'AMD' if vendor == _GPU_VENDOR_AMD else 'Intel'

                hwmon_driver = ''
                hwmon_path = card_dir / 'device' / 'hwmon'
                if hwmon_path.exists():
                    for hdir in hwmon_path.iterdir():
                        if (drv := SysUtils.read_sysfs(str(hdir / 'name'))):
                            hwmon_driver = drv
                            break

                vram = 0
                mem_path = card_dir / 'device' / 'mem_info_vram_total'
                if mem_path.exists():
                    if (val := SysUtils.read_sysfs(str(mem_path))):
                        try:
                            vram = int(val)
                        except ValueError:
                            pass

                vram_mb = vram // (1024 * 1024)
                driver_part = f' {hwmon_driver}' if hwmon_driver else ''
                label = (
                    f'{vendor_label}{driver_part} ({card}, {vram_mb} MB)' if vram_mb
                    else f'{vendor_label}{driver_part} ({card})'
                )
                key = f'{vendor_label.lower()}:{card}'
                gpus.append((key, label, vram))

        gpus.sort(key=lambda g: g[2], reverse=True)
        return [(key, name) for key, name, _ in gpus]

    def _best_gpu(self) -> dict:
        best: dict = {}

        if _ensure_nvml() and pynvml is not None:
            for idx, handle in self._nvidia_handles.items():
                try:
                    mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
                    vram = int(mem.total)
                except NVML_EXC as e:
                    log.debug("_best_gpu(idx=%d) memory probe failed: %s", idx, e)
                    vram = 0
                info = {'vendor': 'nvidia', 'nvidia_idx': idx,
                        'drm_card': '', 'hwmon_driver': '', 'vram': vram}
                if self._preferred_gpu == f'nvidia:{idx}':
                    return info
                if vram > best.get('vram', 0):
                    best = info

        drm_base = Path('/sys/class/drm')
        if drm_base.exists():
            for card_dir in sorted(drm_base.glob('card[0-9]*')):
                if '-' in card_dir.name:
                    continue
                vendor_path = card_dir / 'device' / 'vendor'
                if not vendor_path.exists():
                    continue
                try:
                    vendor = vendor_path.read_text().strip().removeprefix('0x')
                except OSError:
                    continue
                if vendor not in (_GPU_VENDOR_AMD, _GPU_VENDOR_INTEL):
                    continue

                mem_path = card_dir / 'device' / 'mem_info_vram_total'
                vram = 0
                if mem_path.exists():
                    if (val := SysUtils.read_sysfs(str(mem_path))):
                        try:
                            vram = int(val)
                        except ValueError:
                            pass

                hwmon_driver = ''
                hwmon_path = card_dir / 'device' / 'hwmon'
                if hwmon_path.exists():
                    for hdir in hwmon_path.iterdir():
                        if (name := SysUtils.read_sysfs(str(hdir / 'name'))):
                            hwmon_driver = name
                            break
                vendor_name = 'amd' if vendor == _GPU_VENDOR_AMD else 'intel'
                info = {'vendor': vendor_name, 'nvidia_idx': None,
                        'drm_card': card_dir.name, 'hwmon_driver': hwmon_driver,
                        'vram': vram}
                if self._preferred_gpu == f'{vendor_name}:{card_dir.name}':
                    return info
                if vram > best.get('vram', 0):
                    best = info

        return best


# =========================================================================
# LinuxPlatform — THE one class
# =========================================================================

class LinuxPlatform(Platform):
    """Linux Platform — all OS logic inline, no intermediaries."""

    def __init__(self) -> None:
        super().__init__()
        self._autostart: LinuxAutostartManager | None = None

    def _get_autostart(self) -> LinuxAutostartManager:
        if self._autostart is None:
            self._autostart = LinuxAutostartManager()
        return self._autostart

    # ── Hardware discovery ────────────────────────────────────

    def create_detect_fn(self):
        from trcc.adapters.device.detector import DeviceDetector
        from trcc.adapters.device.linux.detector import linux_scsi_resolver
        return DeviceDetector.make_detect_fn(scsi_resolver=linux_scsi_resolver)

    def _make_sensor_enumerator(self) -> SensorEnumerator:
        return SensorEnumerator()

    # ── Transport creation ────────────────────────────────────

    def create_scsi_transport(self, path: str,
                              vid: int = 0, pid: int = 0) -> Any:
        from trcc.adapters.device.linux.scsi import LinuxScsiTransport
        return LinuxScsiTransport(path)

    # ── Autostart (XDG .desktop) ──────────────────────────────

    def autostart_enable(self) -> None:
        self._get_autostart().enable()

    def autostart_disable(self) -> None:
        self._get_autostart().disable()

    def autostart_enabled(self) -> bool:
        return self._get_autostart().is_enabled()

    def acquire_instance_lock(self) -> object | None:
        return _posix_acquire_instance_lock(self.config_dir())

    def raise_existing_instance(self) -> None:
        _posix_raise_existing_instance(self.config_dir())

    def _screen_capture_format(self) -> str | None:
        return 'x11grab'

    def wire_ipc_raise(self, app: Any, window: Any) -> None:
        import signal
        import socket

        from PySide6.QtCore import QSocketNotifier

        rsock, wsock = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        rsock.setblocking(False)
        wsock.setblocking(False)

        def _on_sigusr1(signum: Any, frame: Any) -> None:
            try:
                wsock.send(b'\x01')
            except OSError:
                pass

        signal.signal(signal.SIGUSR1, _on_sigusr1)
        notifier = QSocketNotifier(rsock.fileno(), QSocketNotifier.Type.Read, app)

        def _raise_window() -> None:
            try:
                rsock.recv(1)
            except OSError:
                pass
            window.showNormal()
            window.raise_()
            window.activateWindow()

        notifier.activated.connect(_raise_window)

    # ── Administration ────────────────────────────────────────

    def get_pkg_manager(self) -> str | None:
        from trcc.adapters.infra.doctor import _detect_pkg_manager
        return _detect_pkg_manager()

    def check_deps(self) -> list:
        from trcc.adapters.infra.doctor import check_system_deps
        return check_system_deps(self.get_pkg_manager())

    def install_rules(self) -> int:
        return setup_udev()

    def check_permissions(self, devices: list) -> list[str]:
        from trcc.adapters.device.detector import check_udev_rules
        from trcc.core.models import PROTOCOL_TRAITS
        warnings = []
        for dev in devices:
            if not check_udev_rules(dev):
                traits = PROTOCOL_TRAITS.get(dev.protocol, PROTOCOL_TRAITS['scsi'])
                msg = (f"Device {dev.vid:04x}:{dev.pid:04x} needs updated udev rules.\n"
                       "Run:  sudo trcc setup-udev")
                if traits.requires_reboot:
                    msg += "\nThen reboot for the USB storage quirk to take effect."
                warnings.append(msg)
                break
        return warnings

    def get_system_files(self) -> list[str]:
        return [
            "/etc/udev/rules.d/99-trcc-lcd.rules",
            "/etc/modprobe.d/trcc-lcd.conf",
            "/etc/modules-load.d/trcc-sg.conf",
            "/usr/share/polkit-1/actions/com.github.lexonight1.trcc.policy",
            "/etc/polkit-1/rules.d/50-trcc.rules",
        ]

    def needs_setup(self) -> bool:
        return not os.path.isfile('/etc/udev/rules.d/99-trcc-lcd.rules')

    def auto_setup(self) -> None:
        print("\n[TRCC] First run — device permissions need to be configured.")
        print("       This requires your password (sudo) to install udev rules.")
        print("       [Y]es — set up now (will prompt for sudo password)")
        print("       [N]o  — skip, run 'trcc setup' later\n")
        if not _confirm("Set up now?", auto_yes=False):
            print("       Skipped. Run 'trcc setup' when ready.\n")
            return
        setup_udev()
        from trcc.adapters.infra.diagnostics import check_selinux
        se = check_selinux()
        if se.enforcing and not se.ok:
            setup_selinux()
        print("[TRCC] Setup complete.\n")

    # ── Identity ──────────────────────────────────────────────

    def distro_name(self) -> str:
        from trcc.adapters.infra.doctor import _read_os_release
        return _read_os_release().get('PRETTY_NAME', 'Unknown Linux')

    def no_devices_hint(self) -> str | None:
        return None

    def doctor_config(self) -> DoctorPlatformConfig:
        return DoctorPlatformConfig(
            distro_name=self.distro_name(),
            pkg_manager=self.get_pkg_manager(),
            check_libusb=True,
            extra_binaries=[('sg_raw', True, 'SCSI LCD devices')],
            run_gpu_check=True,
            run_udev_check=True,
            run_selinux_check=True,
            run_rapl_check=True,
            run_polkit_check=True,
            run_winusb_check=False,
            enable_ansi=False,
        )

    def report_config(self) -> ReportPlatformConfig:
        return ReportPlatformConfig(
            distro_name=self.distro_name(),
            collect_lsusb=True,
            collect_udev=True,
            collect_selinux=True,
            collect_rapl=True,
            collect_device_permissions=True,
        )

    # ── Setup operations ──────────────────────────────────────

    def run_setup(self, auto_yes: bool = False) -> int:
        from trcc.adapters.infra.doctor import (
            check_desktop_entry,
            check_gpu,
            check_polkit,
            check_rapl,
            check_selinux,
            check_system_deps,
            check_udev,
        )

        pm = self.get_pkg_manager()
        print(f"\n  TRCC Setup — {self.distro_name()}\n")
        actions: list[str] = []

        # Step 1/6: System dependencies
        print("  Step 1/6: System dependencies")
        deps = check_system_deps(pm)
        missing_required: list[str] = []
        missing_optional: list[str] = []
        for dep in deps:
            if dep.ok:
                ver = f" {dep.version}" if dep.version else ""
                print(f"    [OK]  {dep.name}{ver}")
            elif dep.required:
                note = f" ({dep.note})" if dep.note else ""
                print(f"    [!!]  {dep.name} — MISSING{note}")
                missing_required.append(dep.install_cmd)
            else:
                note = f" ({dep.note})" if dep.note else ""
                print(f"    [--]  {dep.name} — not installed{note}")
                missing_optional.append(dep.install_cmd)

        import shlex
        import sys as _sys

        def _run_install(cmd: str) -> bool:
            if cmd.startswith('pip install '):
                pkg = cmd[len('pip install '):]
                actual = [_sys.executable, '-m', 'pip', 'install', pkg]
            else:
                actual = shlex.split(cmd)
            print(f"    -> {cmd}")
            return subprocess.run(actual).returncode == 0

        for cmd in missing_required:
            if _confirm(f"Install? -> {cmd}", auto_yes):
                if _run_install(cmd):
                    actions.append(f"Installed: {cmd}")
                else:
                    print("    [!!] Command failed")
        for cmd in missing_optional:
            if _confirm(f"Install? -> {cmd}", auto_yes):
                if _run_install(cmd):
                    actions.append(f"Installed: {cmd}")
        print()

        # Step 2/6: GPU detection
        print("  Step 2/6: GPU detection")
        if not (gpus := check_gpu()):
            print("    [--]  No discrete GPU detected")
        for gpu in gpus:
            if gpu.package_installed:
                print(f"    [OK]  {gpu.label}")
            else:
                print(f"    [--]  {gpu.label} — {gpu.install_cmd}")
                if _confirm(f"Install? -> {gpu.install_cmd}", auto_yes):
                    print(f"    -> {gpu.install_cmd}")
                    result = subprocess.run(
                        [sys.executable, "-m", "pip", "install", *gpu.install_cmd.split()[-1:]],
                    )
                    if result.returncode == 0:
                        actions.append(f"Installed: {gpu.install_cmd}")
                    else:
                        print(f"    [!!] pip failed (exit {result.returncode})")
        print()

        # Step 3/6: USB device permissions (udev + RAPL)
        print("  Step 3/6: USB device permissions")
        udev = check_udev()
        if udev.ok:
            print(f"    [OK]  {udev.message}")
        else:
            print(f"    [!!]  {udev.message}")
            if _confirm("Install udev rules? (requires sudo)", auto_yes):
                rc = setup_udev()
                if rc == 0:
                    actions.append("Installed udev rules")
                else:
                    print("    [!!] udev setup failed")

        rapl = check_rapl()
        if rapl.applicable:
            if rapl.ok:
                print(f"    [OK]  {rapl.message}")
            else:
                print(f"    [--]  {rapl.message}")
                if _confirm("Fix RAPL permissions? (requires sudo)", auto_yes):
                    if not is_root():
                        rc = sudo_reexec("setup-udev")
                    else:
                        setup_rapl_permissions()
                        rc = 0
                    if rc == 0:
                        actions.append("Fixed RAPL power sensor permissions")
        print()

        # Step 4/6: SELinux policy
        se = check_selinux()
        if se.enforcing:
            print("  Step 4/6: SELinux policy")
            if se.ok:
                print(f"    [OK]  {se.message}")
            else:
                print(f"    [!!]  {se.message}")
                if _confirm("Install SELinux USB policy? (requires sudo)", auto_yes):
                    rc = setup_selinux()
                    if rc == 0:
                        actions.append("Installed SELinux policy")
                    else:
                        print("    [!!] SELinux setup failed")
            print()

        # Step 5/6: Polkit policy
        print("  Step 5/6: Hardware info access")
        pk = check_polkit()
        if pk.ok:
            print(f"    [OK]  {pk.message}")
        else:
            print(f"    [--]  {pk.message}")
            if _confirm("Install polkit policy for hardware info? (requires sudo)", auto_yes):
                rc = setup_polkit()
                if rc == 0:
                    actions.append("Installed polkit policy")
                else:
                    print("    [!!] polkit setup failed")
        print()

        # Step 6/6: Desktop integration
        print("  Step 6/6: Desktop integration")
        if check_desktop_entry():
            print("    [OK]  Application menu entry installed")
        else:
            print("    [--]  No application menu entry")
            if _confirm("Install application menu entry?", auto_yes):
                rc = install_desktop()
                if rc == 0:
                    actions.append("Installed desktop entry")
        print()

        _print_summary(actions, "Run 'trcc gui' to launch, or find TRCC in your app menu.")
        return 0

    def install_desktop(self) -> int:
        return install_desktop()

    # ── Help text ─────────────────────────────────────────────

    def archive_tool_install_help(self) -> str:
        from trcc.adapters.infra.doctor import _install_hint
        pm = self.get_pkg_manager()
        if (hint := _install_hint('7z', pm)):
            return f"7z not found. Install:\n  {hint}"
        return (
            "7z not found. Install p7zip for your distro:\n"
            "  Fedora/RHEL:    sudo dnf install p7zip p7zip-plugins\n"
            "  Ubuntu/Debian:  sudo apt install p7zip-full\n"
            "  Arch:           sudo pacman -S p7zip"
        )

    def ffmpeg_install_help(self) -> str:
        from trcc.adapters.infra.doctor import _detect_pkg_manager, _install_hint
        pm = _detect_pkg_manager()
        hint = _install_hint('ffmpeg', pm)
        return f"ffmpeg not found. Install:\n  {hint}" if hint else "ffmpeg not found"

    # ── Hardware info ─────────────────────────────────────────

    def get_memory_info(self) -> list[dict[str, str]]:
        return get_memory_info()

    def get_disk_info(self) -> list[dict[str, str]]:
        return get_disk_info()
