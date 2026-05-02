"""Universal command bus.

UIs (CLI / API / GUI) translate their native input into a `Command`, hand it
to a `Dispatcher`, and translate the returned `OpResult` back into native
output. The same `Command` produces the same effect through any UI — the
daemon is a manifold, the UIs are interchangeable adapters.

Two directions:

  ``UI → Dispatcher.dispatch(Command) → OpResult``      (request / response)
  ``Dispatcher.subscribe(topic, handler) → Subscription`` (push notifications)

This module declares the protocol vocabulary plus one in-process
implementation. The IPC implementation lives in ``trcc.ipc``.

Result types intentionally reuse the existing ``OpResult`` hierarchy from
``core.results`` — no parallel type system, no adaptation layer.
"""
from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol

from .results import OpResult

if TYPE_CHECKING:
    from .events import EventBus
    from .trcc import Trcc

log = logging.getLogger(__name__)

# Shared empty-kwargs sentinel — avoids per-instance dict allocation and
# guarantees Command instances stay immutable even if a caller forgets to
# pass kwargs.
_EMPTY_KW: Mapping[str, Any] = MappingProxyType({})

# Roles whose facade methods take a device index as the first positional arg.
# `control_center` and `system` operate on app-wide state — no index.
_INDEXED_ROLES: frozenset[str] = frozenset({"lcd", "led"})


# =============================================================================
# Command — the unit of work over the wire.
# =============================================================================

@dataclass(frozen=True, slots=True)
class Command:
    """A typed device action serializable over any transport.

    Attributes:
        role:    Which facade owns the method — ``"lcd" | "led" |
                 "control_center" | "system"``.
        method:  Facade method name (e.g. ``"send_image"``,
                 ``"set_brightness"``).
        index:   Device slot within the role. Ignored for non-indexed
                 roles (``control_center``, ``system``).
        kwargs:  Forwarded verbatim to the resolved facade method.

    Wire format::

        {"role": "lcd", "method": "send_color", "index": 1,
         "kwargs": {"r": 255, "g": 0, "b": 0}}
    """
    role: str
    method: str
    index: int = 0
    kwargs: Mapping[str, Any] = _EMPTY_KW


# =============================================================================
# Event — the unit of push notification.
# =============================================================================

@dataclass(frozen=True, slots=True)
class Event:
    """A state-change notification published by the daemon's `EventBus`.

    Carried from the daemon to subscribed UIs (in-process callbacks, or
    JSON lines on a long-lived IPC connection).
    """
    topic: str
    data: Mapping[str, Any] = _EMPTY_KW
    timestamp: float = field(default_factory=time.monotonic)


class Subscription(Protocol):
    """Handle returned by :meth:`Dispatcher.subscribe`. Exists to be cancelled."""

    def unsubscribe(self) -> None: ...


# =============================================================================
# Dispatcher — port between UI and Trcc.
# =============================================================================

class Dispatcher(ABC):
    """Port: how a UI talks to a `Trcc`.

    Concrete dispatchers:
        - :class:`InProcessDispatcher` — direct calls (used inside the daemon).
        - :class:`trcc.ipc.IpcDispatcher` — Unix-socket transport (used by
          remote UIs talking to a running daemon).
    """

    @abstractmethod
    def dispatch(self, cmd: Command) -> OpResult:
        """Route the `Command` to the right facade method, return its result.

        Failures (unknown role, unknown method, raised exception) are
        funnelled into ``OpResult(success=False, error=...)`` rather than
        propagated, so every Command produces a Result.
        """

    @abstractmethod
    def subscribe(self, topic: str,
                  handler: Callable[[Event], None]) -> Subscription:
        """Subscribe to events on ``topic``. Returns a handle for cancellation."""


# =============================================================================
# InProcessDispatcher — operates on a Trcc directly. Lives in the daemon
# process; serves the daemon's own UIs (its embedded GUI / API / tests).
# =============================================================================

class InProcessDispatcher(Dispatcher):
    """Dispatcher that calls ``Trcc.{role}.{method}`` directly.

    Same process as the `Trcc`. The daemon constructs one of these for
    its own UIs; remote UIs use :class:`IpcDispatcher` instead.
    """

    __slots__ = ('_trcc',)

    def __init__(self, trcc: Trcc) -> None:
        self._trcc = trcc

    # ── dispatch ────────────────────────────────────────────────────────────

    def dispatch(self, cmd: Command) -> OpResult:
        target = self._role_target(cmd.role)
        if target is None:
            return OpResult(False, error=f"Unknown role: {cmd.role!r}")

        fn = getattr(target, cmd.method, None)
        if not callable(fn):
            return OpResult(
                False, error=f"Unknown method: {cmd.role}.{cmd.method}")

        try:
            result = (fn(cmd.index, **cmd.kwargs)
                      if cmd.role in _INDEXED_ROLES else fn(**cmd.kwargs))
        except Exception as e:
            log.exception("dispatch %s.%s[%d] failed",
                          cmd.role, cmd.method, cmd.index)
            return OpResult(False, error=f"{type(e).__name__}: {e}")
        if not isinstance(result, OpResult):
            return OpResult(
                False,
                error=f"{cmd.role}.{cmd.method} did not return an OpResult "
                      f"(got {type(result).__name__})")
        return result

    # ── subscribe ───────────────────────────────────────────────────────────

    def subscribe(self, topic: str,
                  handler: Callable[[Event], None]) -> Subscription:
        # Adapt EventBus's positional-payload contract into Event objects.
        def _adapter(*payload: Any) -> None:
            data: Mapping[str, Any]
            if len(payload) == 1 and isinstance(payload[0], Mapping):
                data = payload[0]
            else:
                data = MappingProxyType({"payload": payload})
            handler(Event(topic, data))

        sub_id = self._trcc.events.subscribe(topic, _adapter)
        return _SubscriptionHandle(self._trcc.events, sub_id)

    # ── role lookup ─────────────────────────────────────────────────────────

    def _role_target(self, role: str) -> Any:
        match role:
            case "lcd":
                return self._trcc.lcd
            case "led":
                return self._trcc.led
            case "control_center":
                return self._trcc.control_center
            case _:
                return None


@dataclass(frozen=True, slots=True)
class _SubscriptionHandle:
    """Concrete `Subscription` returned by :meth:`InProcessDispatcher.subscribe`."""

    _bus: EventBus
    _sub_id: int

    def unsubscribe(self) -> None:
        self._bus.unsubscribe(self._sub_id)
