"""Shared scaffolding for the per-OS runtime smoke harnesses.

Each ``dev/smoke_<os>.py`` exercises the real OS-specific APIs of its
target Platform — sensors, USB enumeration, device discovery — and
prints a structured report a reporter can paste straight into a GitHub
issue.

The harnesses are designed to:
- Fail-soft: every probe catches its own exceptions and reports the
  failure rather than crashing the whole run.
- Be self-locating: each harness checks ``sys.platform`` first; if run
  on the wrong OS it exits cleanly with a clear "this is for X" message.
- Be hardware-optional: a smoke can pass even with no Thermalright
  device plugged in. Hardware-needing probes report SKIP, not FAIL.
- Stay honest: a probe is PASS only if the API call returned a sane
  value, WARN if it succeeded but with unexpected shape, FAIL if it
  raised, SKIP if pre-conditions weren't met.

Output layout (so reporters' paste-bombs are diff-able):

    TRCC Smoke — <OS> runtime
    =========================
    Python:  3.x.y
    OS:      <distro / version>
    Arch:    <arch>

    [section]
      [OK]    probe-name         — detail
      [WARN]  probe-name         — detail
      [SKIP]  probe-name         — reason
      [FAIL]  probe-name         — exception class + message

    Summary: N pass, M warn, K skip, X fail
"""
from __future__ import annotations

import platform
import sys
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class _Outcome(Enum):
    PASS = 'OK'
    WARN = 'WARN'
    SKIP = 'SKIP'
    FAIL = 'FAIL'


@dataclass(slots=True)
class _Probe:
    """One named check + its outcome + a one-line detail."""

    name: str
    outcome: _Outcome
    detail: str = ''


@dataclass(slots=True)
class Section:
    """A grouping of probes (e.g. 'Imports', 'Sensors', 'Devices')."""

    title: str
    probes: list[_Probe] = field(default_factory=list)

    def ok(self, name: str, detail: str = '') -> None:
        self.probes.append(_Probe(name, _Outcome.PASS, detail))

    def warn(self, name: str, detail: str = '') -> None:
        self.probes.append(_Probe(name, _Outcome.WARN, detail))

    def skip(self, name: str, reason: str) -> None:
        self.probes.append(_Probe(name, _Outcome.SKIP, reason))

    def fail(self, name: str, exc: BaseException) -> None:
        self.probes.append(
            _Probe(name, _Outcome.FAIL, f'{type(exc).__name__}: {exc}'),
        )

    def run(self, name: str, fn: Callable[[], str]) -> None:
        """Run ``fn``; on success its return value becomes the OK detail.

        On exception, record FAIL with the type + message.
        """
        try:
            self.ok(name, fn())
        except BaseException as exc:
            self.fail(name, exc)


def require_os(target_platform_substring: str) -> bool:
    """Return True if ``sys.platform`` contains ``target_platform_substring``.

    On mismatch, prints a polite "this is for X" line and the caller
    should ``return 0`` from main.
    """
    if target_platform_substring in sys.platform:
        return True
    print(
        f"  This smoke is for {target_platform_substring!r}; you're on "
        f"{sys.platform!r}. Skipping cleanly. (Run on the target OS to "
        f"exercise its real APIs.)",
    )
    return False


def print_header(os_label: str) -> None:
    """Print the top banner with Python / OS / arch context."""
    py = '.'.join(map(str, sys.version_info[:3]))
    distro = _safe_distro_label()
    arch = platform.machine()
    print(f"\n  TRCC Smoke — {os_label} runtime")
    print(f"  {'=' * (16 + len(os_label))}")
    print(f"  Python:  {py}")
    print(f"  OS:      {distro}")
    print(f"  Arch:    {arch}")
    print()


def print_section(section: Section) -> None:
    print(f"  [{section.title}]")
    for p in section.probes:
        marker = f"[{p.outcome.value}]"
        # Right-pad the marker so the probe names line up
        print(f"    {marker:<7}  {p.name:<32}  {p.detail}")
    print()


def print_summary_and_exit(sections: list[Section]) -> int:
    """Print PASS/WARN/SKIP/FAIL totals across all sections; return exit code.

    Exit code 0 when no FAIL probes; 1 otherwise.  WARN doesn't fail —
    it's a "look at this" signal, not a regression.
    """
    counts = {o: 0 for o in _Outcome}
    for s in sections:
        for p in s.probes:
            counts[p.outcome] += 1
    summary_line = (
        f"Summary: {counts[_Outcome.PASS]} pass, "
        f"{counts[_Outcome.WARN]} warn, "
        f"{counts[_Outcome.SKIP]} skip, "
        f"{counts[_Outcome.FAIL]} fail"
    )
    print(f"  {summary_line}")
    print()
    return 0 if counts[_Outcome.FAIL] == 0 else 1


def _safe_distro_label() -> str:
    """Return a one-line OS+version label.  Never raises."""
    try:
        # Use ``platform.platform()`` for a portable label.
        return platform.platform()
    except Exception:
        return f'{sys.platform}'


def safe_call(fn: Callable[[], Any]) -> tuple[bool, Any]:
    """Run ``fn`` returning (True, result) or (False, exception).  Never raises."""
    try:
        return True, fn()
    except BaseException as exc:
        return False, exc


def short_exc(exc: BaseException) -> str:
    """Format an exception for one-line probe output."""
    return f'{type(exc).__name__}: {exc}'


def trace_to_lines(exc: BaseException, max_lines: int = 6) -> list[str]:
    """Return the last ``max_lines`` of the traceback for verbose mode."""
    return traceback.format_exception(type(exc), exc, exc.__traceback__)[-max_lines:]


__all__ = [
    'Section',
    'print_header',
    'print_section',
    'print_summary_and_exit',
    'require_os',
    'safe_call',
    'short_exc',
    'trace_to_lines',
]
