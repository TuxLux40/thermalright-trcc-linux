# TRCC Linux — OS Smoke-Testing Law

**Purpose.** This file is the law for proving the app works on every supported OS *before* a release ships or an issue gets a "try vX.Y.Z" reply. It complements [CLAUDE.md](CLAUDE.md): CLAUDE.md says *how to write* the code, this file says *how to prove* it runs.

Read it like the law. If a smoke harness is the gate for an action (post a reply, cut a tag), do not skip the gate.

## The Harness Map

Seven harnesses live under `dev/`. Each has one purpose; they do **not** overlap. Pick the one that matches what changed.

| Harness | One-line purpose | When to run | Exit gate |
|---|---|---|---|
| [smoke_platforms.py](dev/smoke_platforms.py) | OS conformance — 42 assertions across all 4 `Platform` subclasses + the three-factory chain | After **any** architecture change (rename, ABC tweak, factory edit) | `42/42` |
| [smoke_anything.py](dev/smoke_anything.py) | Parametrized stress — pick `--os` + `--device`, runs the full probe battery (rotation/cache, geometry, video target, sensor discovery, lifecycle) | After fixing a cross-cutting bug; before posting any cross-OS reply | All probes `PASS` (yellow `BAD` rows are bugs) |
| [smoke_device_matrix.py](dev/smoke_device_matrix.py) | Per-VID:PID lifecycle — handshake → idempotent handshake → frame send → sleep/resume → send-after-close | Before a release; after touching any protocol code | All rows `PASS` |
| [smoke_reported_bugs.py](dev/smoke_reported_bugs.py) | Per-issue repro replay — `REPRODUCED` vs `NOT-REPRODUCED` for every open ticket | Before posting "try vX.Y.Z" on a specific issue | Target issue rows `NOT-REPRODUCED` |
| [smoke_rotation_mask.py](dev/smoke_rotation_mask.py) | GUI rotation + portrait web-mask path (the #137 canary) | Any rotation / orientation / mask / portrait code edit | Exit 0 |
| [smoke_sleep_cycle.py](dev/smoke_sleep_cycle.py) | Sleep/resume cycle — the #144 Tee86 contract (events fire, cache clears, devices repopulate) | Any code touching power events, daemon lifecycle, factory cache | Exit 0 |
| [smoke_daemon_gui.py](dev/smoke_daemon_gui.py) | Daemon IPC — descriptors + FRAME events round-trip through `TrccProxy` over the socket | Any change to IPC wire format, `_dispatch_meta`, `TrccProxy`, `_boot.trcc()` | Exit 0 |

Plus [mock_gui.py](dev/mock_gui.py) — not a smoke *test* but the runtime canary. After any refactor, launch it and grep `dev/.trcc/trcc.log` for `error|traceback|warning`. CLAUDE.md's "Run the app, not just tests" rule lives here.

## Verification Reality

We do not own every OS. The harnesses above run on the Linux dev box and exercise the other 3 Platforms *structurally* — they prove the code shape holds, not that real hardware works.

| OS | Self-verify path | What we can prove without a reporter | What we cannot |
|---|---|---|---|
| Linux | Native dev box | Everything: code, real hardware (the donor matrix is here) | — |
| Windows | VM on Linux host | Imports, factory dispatch, structural conformance, repro of reported tracebacks | Real-hardware handshake on a Windows-only device |
| macOS | **No VM** (Apple licensing + hardware lock) | Imports + structural conformance via `smoke_platforms.py` | Anything runtime — depends on a reporter cycle |
| FreeBSD / OpenBSD | Lightweight VM possible, not set up yet | Imports + structural conformance | Anything runtime — depends on a reporter cycle for now |

**Implications, in order of consequence:**

1. **macOS replies cost a reporter cycle.** No "try v9.X.Y" post until a single reporter has confirmed on real hardware. Use the `awaiting-reporter` label; do not generalize to the release notes until one confirmation lands.
2. **Windows replies need a VM repro first.** `smoke_anything.py --os windows --device <vid:pid>` should reproduce the reported failure, the fix should make it green, *then* reply.
3. **Linux is the only OS where green CI = ready to ship.** Everywhere else, green CI is necessary but not sufficient.

## The Protocol

When you finish a change, pick the row that matches the change and run the listed harnesses **in order**. Stop at the first red.

| Change shape | Order |
|---|---|
| Architecture refactor (factory, ABC, rename) | `ruff` → `pyright` → `smoke_platforms.py` → `pytest -n 8` → `mock_gui.py` |
| Protocol-layer fix (SCSI/HID/Bulk/LY/LED) | `ruff` → `pyright` → `smoke_device_matrix.py` → `smoke_platforms.py` → `pytest` |
| Power / daemon lifecycle | `ruff` → `pyright` → `smoke_sleep_cycle.py` → `smoke_daemon_gui.py` → `pytest` |
| Rotation / mask / portrait | `ruff` → `pyright` → `smoke_rotation_mask.py` → `mock_gui.py` |
| IPC / `TrccProxy` / wire format | `ruff` → `pyright` → `smoke_daemon_gui.py` → `pytest` |
| Cross-OS bug fix from a report | `smoke_anything.py --from-report <path>` → ruff + pyright → re-run `--from-report` for green → full release protocol |
| Pre-tag release | `ruff check .` (full repo, not scoped) → `pyright` → `pytest -n 8` → `smoke_platforms.py` → `smoke_device_matrix.py` → `smoke_reported_bugs.py` → `mock_gui.py` |

**Why scoped vs. full repo for ruff:** release CI runs `ruff check .`, not `ruff check src/ tests/`. Pre-tag must match CI scope or `dev/*.py` files trip new rules and packaging fails (v9.5.8 lesson — see [feedback_lint_scope_full_repo.md](.claude/memory/feedback_lint_scope_full_repo.md)).

## The Bar for "Verified"

A change is **verified** when *all three* gates are green:

1. **Static** — `ruff check .` and `pyright src/trcc/` both report 0 errors / 0 warnings / 0 informations.
2. **Smoke** — every harness in the relevant row of "The Protocol" exits with the listed gate.
3. **Runtime** — `mock_gui.py` runs for 8+ seconds without producing any `ERROR|Traceback` in `dev/.trcc/trcc.log`. (`WARNING` may be acceptable; read the message.)

A change is **shipped-verified** only when in addition:

4. **Reporter confirmation** — one reporter on the target OS has confirmed the fix on real hardware. Required for macOS and BSD. Strongly preferred (but not blocking) for Windows once VM repro is green.

Anything less than `verified` is a draft. Do not post upgrade instructions. Do not tag a release. State the gap explicitly per [feedback_no_bs.md](.claude/memory/feedback_no_bs.md).

## Per-OS Smoke Specifics

| OS | Path / config | Detect entry point | Sensor source(s) | Known smoke pitfalls |
|---|---|---|---|---|
| Linux | `~/.trcc/` | `/dev/sgN` via SG_IO + udev (`USB_VENDOR_ID`/`USB_DEVICE_PATH`) | hwmon + psutil + pynvml | RAPL `energy_uj` perm (#143 — `kernel-native over userspace`, see memory) |
| Windows | `%APPDATA%\trcc` | WMI + DeviceIoControl + hidapi | LibreHardwareMonitor via WMI namespace `root\LibreHardwareMonitor` | `os.O_NONBLOCK` does not exist; stderr encoding (StreamHandler.emit chain) |
| macOS | `~/Library/Application Support/trcc` | IOKit | **Fragmented** — see [project_macos_metrics_fragmented.md](.claude/memory/project_macos_metrics_fragmented.md). Intel SMC ≠ Apple Silicon SMC | No VM — every fix is reporter-cycle. Do NOT assume Intel SMC keys work on Apple Silicon |
| FreeBSD / OpenBSD | `~/.trcc/` (XDG fallback) | `usbconfig` + sysctl | `hw.sensors`, `dev.cpu.N.temperature` | sysctl names differ per BSD distro — verify the exact name on the reporter's `uname -r` |

Each Platform class lives at [src/trcc/adapters/system/](src/trcc/adapters/system/). Its smoke counterpart is the `PlatformFactory`-registered subclass plus the matching row in `smoke_platforms.py`. If a new OS gets added (`@PlatformFactory.register('haiku')`), add ~10 assertions to `smoke_platforms.py` mirroring the existing Linux block.

## How to Extend

**Adding a probe to `smoke_anything.py`:**

```python
def probe_my_new_check(platform, device) -> ProbeResult:
    """One-line description that shows up in --list-probes."""
    if not condition_holds:
        return _bad("specific symptom and what it implies")
    return _ok("what passed, with the relevant value")

PROBES.append(Probe(name="my.new.check", fn=probe_my_new_check))
```

Each probe is a (real-bug-class, current-code) pair. If a probe never reproduces, delete it — green probes that can never go red are dead test code.

**Adding a per-OS assertion to `smoke_platforms.py`:**

Find the section under "Chain integrity" or per-platform block, add the assert with the `[ OK ] / [FAIL]` shape the existing assertions use. Smoke is text-output, not pytest — keep the report scannable.

**Adding a new harness:**

Don't, unless the change class doesn't fit any of the 7. Smoke files multiply faster than they get retired. Before adding `dev/smoke_<thing>.py`, check whether `smoke_anything.py` could absorb it as a `--probe`.

## What This Doc Is Not

- **Not a pytest replacement.** Pytest verifies units of code; smoke harnesses verify systems integrate. Both gates must pass.
- **Not a substitute for reading the log.** `mock_gui.py` is the canary; `~/.trcc/trcc.log` is the canary's voice. Per CLAUDE.md "Look at the Log Before the Code", grep first.
- **Not a substitute for reporter confirmation on non-Linux OSes.** Smoke + VM repro tighten the loop; they do not close it for macOS / BSD.
