"""CommandRouter — translate a UI-level call into a typed `Command`.

UIs invoke commands by **name** (a dotted ``"role.method"`` string) plus a
flat kwargs dict. The router turns that into a fully-typed
:class:`~trcc.core.dispatch.Command` ready for a `Dispatcher`.

Phase 1 ships the shape and the simple split-and-pack route. Phase 3 will
populate a registry by introspecting `Trcc` so unknown names fail fast at
startup rather than at call time.
"""
from __future__ import annotations

from typing import Any

from .dispatch import Command


class CommandRouter:
    """Build typed `Command`\\ s from named UI calls.

    Today the only translation is splitting ``"role.method"`` and pulling
    ``index`` out of the kwargs into the typed slot. Tomorrow this is
    where we'll validate the name against `Trcc`\\ 's actual facade
    methods at startup (Phase 3).
    """

    __slots__ = ()

    def route(self, name: str, **kwargs: Any) -> Command:
        """Turn ``"role.method"`` + kwargs into a `Command`.

        ``index`` (if present) becomes :attr:`Command.index`; everything
        else is forwarded as :attr:`Command.kwargs`. Non-indexed roles
        ignore ``index`` silently.
        """
        try:
            role, method = name.split(".", 1)
        except ValueError as e:
            raise ValueError(
                f"Command name must be 'role.method', got {name!r}") from e

        index = kwargs.pop("index", 0)
        return Command(role=role, method=method, index=index, kwargs=kwargs)
