"""LibreHardwareMonitor subprocess lifecycle.

TRCC reads Windows sensors via the ``root\\LibreHardwareMonitor`` WMI
namespace, populated by ``LibreHardwareMonitor.exe`` running in the
background. We bundle the binary at ``dist/trcc/lhm/`` and spawn it on
Platform init.

The C# Thermalright app does the equivalent for HWINFO (proprietary
nagware); we use the FOSS LHM (MPL-2.0) and read via WMI instead of
shared memory. Mirrors a documented pattern (Rainmeter, Hass.Agent,
Zabbix, smart-home tools).

Design:
- **Idempotent.** If LHM is already running (user-managed or another TRCC
  process), reuse it; don't spawn a duplicate.
- **Window-suppressed.** Bundled config is pre-seeded with start-minimized
  + minimize-to-tray; spawn also passes ``CREATE_NO_WINDOW``. Belt and
  suspenders — Forms windows can ignore one but not both.
- **Owned vs. found.** Track whether *we* spawned the process. On stop,
  only terminate processes we own; never kill a user's standalone LHM.
- **Graceful degradation.** If spawn fails (AV quarantine, missing exe,
  permissions) — log and return ``None``. Caller falls back to
  psutil/wmi/pynvml; sidebar/device just skip LHM-only sensors.
"""
from __future__ import annotations

import logging
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import psutil

log = logging.getLogger(__name__)

_LHM_PROCESS_NAME = "LibreHardwareMonitor.exe"
_LHM_NAMESPACE = "root\\LibreHardwareMonitor"
_WMI_READY_TIMEOUT_SEC = 30.0
_WMI_READY_INTERVAL_SEC = 0.2


class _LHMSubprocess:
    """Owns the bundled LHM process and provides the WMI handle.

    Single instance per Platform. Holds the spawned ``Popen`` (or ``None``
    if LHM was already running and we reused it). ``stop()`` terminates
    only what we own.
    """

    __slots__ = ("_namespace_handle", "_owned_process")

    def __init__(self) -> None:
        self._owned_process: subprocess.Popen[bytes] | None = None
        self._namespace_handle: Any = None

    @property
    def namespace(self) -> Any:
        """The WMI handle to ``root\\LibreHardwareMonitor`` or ``None``."""
        return self._namespace_handle

    def start(self) -> Any:
        """Ensure LHM is running and the WMI namespace is queryable.

        Returns the WMI handle, or ``None`` if LHM couldn't be started or
        the namespace never registered within the timeout. Idempotent —
        callers can invoke repeatedly; only the first start spawns.
        """
        if self._namespace_handle is not None:
            return self._namespace_handle

        if not _is_lhm_running():
            self._owned_process = _spawn_lhm()
            if self._owned_process is None:
                log.warning("LibreHardwareMonitor not bundled or spawn failed; "
                            "Windows temps/fans unavailable")
                return None
            log.info("Spawned LibreHardwareMonitor (pid=%d)",
                     self._owned_process.pid)
        else:
            log.info("LibreHardwareMonitor already running; reusing")

        self._namespace_handle = _wait_for_wmi_namespace()
        if self._namespace_handle is None:
            log.warning("LibreHardwareMonitor WMI namespace did not register "
                        "within %.0fs; Windows temps/fans unavailable",
                        _WMI_READY_TIMEOUT_SEC)
        return self._namespace_handle

    def stop(self) -> None:
        """Terminate the LHM subprocess if we spawned it.

        Never kills a user-managed instance; only what ``start()`` opened.
        """
        self._namespace_handle = None
        if self._owned_process is None:
            return
        try:
            self._owned_process.terminate()
            self._owned_process.wait(timeout=3)
        except (subprocess.TimeoutExpired, OSError) as e:
            log.debug("LHM terminate failed (%s); killing", e)
            try:
                self._owned_process.kill()
            except OSError:
                pass
        self._owned_process = None


def _lhm_exe_path() -> Path | None:
    """Locate the bundled LibreHardwareMonitor.exe.

    Looks in ``<exe-dir>/lhm/`` (PyInstaller dist layout). Returns
    ``None`` if the binary isn't present — graceful degradation rather
    than a hard error.
    """
    candidates = [
        Path(sys.executable).parent / "lhm" / _LHM_PROCESS_NAME,
        # Dev mode: assume someone dropped LHM here for testing.
        Path.cwd() / "lhm" / _LHM_PROCESS_NAME,
    ]
    for c in candidates:
        if c.is_file():
            return c
    return None


def _is_lhm_running() -> bool:
    """Check whether any ``LibreHardwareMonitor.exe`` process is alive."""
    try:
        for proc in psutil.process_iter(["name"]):
            try:
                if (proc.info.get("name") or "").lower() == _LHM_PROCESS_NAME.lower():
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except psutil.Error as e:
        log.debug("psutil process scan failed: %s", e)
    return False


def _spawn_lhm() -> subprocess.Popen[bytes] | None:
    """Launch the bundled LHM with hidden window. Returns the Popen handle."""
    exe = _lhm_exe_path()
    if exe is None:
        log.debug("LHM exe not found in expected locations")
        return None

    creationflags = 0
    startupinfo = None
    if sys.platform == "win32":
        # CREATE_NO_WINDOW (0x08000000) — no console window
        # DETACHED_PROCESS (0x00000008) — independent of TRCC's console
        creationflags = 0x08000000
        # SW_HIDE (0) — belt-and-suspenders for the Forms main window;
        # the seeded config also sets startMinMenuItem=true.
        startupinfo = subprocess.STARTUPINFO()  # pyright: ignore[reportAttributeAccessIssue]
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW  # pyright: ignore[reportAttributeAccessIssue]
        startupinfo.wShowWindow = 0  # SW_HIDE

    try:
        return subprocess.Popen(
            [str(exe)],
            cwd=str(exe.parent),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
            startupinfo=startupinfo,
            close_fds=True,
        )
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("Failed to spawn LibreHardwareMonitor: %s", e)
        return None


def _wait_for_wmi_namespace() -> Any:
    """Poll ``root\\LibreHardwareMonitor`` until it registers or we time out."""
    from trcc.adapters.system._windows_wmi import wmi_handle

    deadline = time.monotonic() + _WMI_READY_TIMEOUT_SEC
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            handle = wmi_handle(namespace=_LHM_NAMESPACE)
            # A handle is only useful if it can return at least one row.
            # Hardware() returns an empty list before LHM finishes its first
            # tick; we wait for any result to ensure the provider is live.
            if list(handle.Hardware()):
                return handle
        except Exception as e:
            last_err = e
        time.sleep(_WMI_READY_INTERVAL_SEC)

    if last_err is not None:
        log.debug("WMI namespace probe last error: %s", last_err)
    return None


