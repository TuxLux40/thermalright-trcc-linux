"""CommandRouter — translate a UI-level call into a typed `Command`.

UIs invoke commands by **name** (a dotted ``"role.method"`` string) plus a
flat kwargs dict. The router turns that into a fully-typed
:class:`~trcc.core.dispatch.Command` ready for a `Dispatcher`.

When constructed from a live `Trcc` (or fed one via :meth:`register`), the
router introspects each facade and validates names at route-time — typos
raise :class:`ValueError` instead of silently producing a Command that
would only fail later inside the dispatcher. The empty-router form keeps
working for tests / one-off usage where no Trcc is on hand.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .dispatch import Command

if TYPE_CHECKING:
    from .trcc import Trcc


class CommandRouter:
    """Build typed `Command`\\ s from named UI calls; validate against `Trcc`.

    Usage::

        router = CommandRouter(trcc)               # validate-on-route
        cmd    = router.route("lcd.send_image", index=0, path=p)

    Without a `Trcc`, the router is a pure parser — useful for tests or
    when the daemon hasn't bootstrapped yet::

        router = CommandRouter()                    # no validation
    """

    __slots__ = ('_methods',)

    def __init__(self, trcc: Trcc | None = None) -> None:
        # Empty registry by default → Phase 1 pass-through behaviour.
        # `register()` populates it from a live Trcc.
        self._methods: dict[str, frozenset[str]] = {}
        if trcc is not None:
            self.register(trcc)

    # ── Population ──────────────────────────────────────────────────────────

    def register(self, trcc: Trcc) -> None:
        """Snapshot the public facade surface from a live `Trcc`.

        Idempotent — call again after a re-scan to refresh the registry.
        """
        self._methods = {
            'lcd':            self._public_methods(trcc.lcd),
            'led':            self._public_methods(trcc.led),
            'control_center': self._public_methods(trcc.control_center),
        }

    @staticmethod
    def _public_methods(facade: Any) -> frozenset[str]:
        """Return the names of every public callable on ``facade``."""
        return frozenset(
            name for name in dir(facade)
            if not name.startswith('_')
            and callable(getattr(facade, name, None))
        )

    # ── Introspection (consumed by UI registration code in later phases) ────

    def roles(self) -> list[str]:
        """Registered role names, sorted."""
        return sorted(self._methods)

    def methods(self, role: str) -> list[str]:
        """Methods for ``role``, sorted. Empty list if role unknown."""
        return sorted(self._methods.get(role, ()))

    # ── Routing ─────────────────────────────────────────────────────────────

    def route(self, name: str, **kwargs: Any) -> Command:
        """Turn ``"role.method"`` + kwargs into a `Command`.

        ``index`` is pulled out of kwargs into the typed slot; everything
        else is forwarded as :attr:`Command.kwargs`. When a `Trcc` has been
        registered, an unknown role or method raises ``ValueError`` at this
        point rather than silently building a Command that the dispatcher
        would later reject.
        """
        try:
            role, method = name.split(".", 1)
        except ValueError as e:
            raise ValueError(
                f"Command name must be 'role.method', got {name!r}") from e

        if self._methods:
            registered = self._methods.get(role)
            if registered is None:
                raise ValueError(
                    f"Unknown role {role!r}. Valid roles: {self.roles()}")
            if method not in registered:
                raise ValueError(
                    f"Unknown method {role}.{method!r}. "
                    f"Valid methods (sample): "
                    f"{sorted(registered)[:8]}{'...' if len(registered) > 8 else ''}")

        index = kwargs.pop("index", 0)
        return Command(role=role, method=method, index=index, kwargs=kwargs)
