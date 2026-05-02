"""UI base — Template Method.

Concrete UIs (``CliUI``, ``ApiUI``, ``GuiUI``) provide a `run` lifecycle
and an output `ResultFormatter`. Everything between input and dispatch is
shared in :meth:`UI.handle`.

The control-flow shape is genuinely different per UI — argv-parse-and-exit
for CLI, ``uvicorn.run`` for API, ``qapp.exec()`` for GUI — so `run` stays
abstract. The data-flow shape (name + kwargs → Command → Result) is
identical, so it lives in the base.

This is the "UI service" port: any caller that can build a kwargs dict and
hand it to :meth:`UI.handle` can drive every TRCC capability the daemon
exposes.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..core.dispatch import Dispatcher
    from ..core.results import OpResult
    from ..core.router import CommandRouter


class ResultFormatter(ABC):
    """Translate an `OpResult` into a UI-native output.

    Concrete formatters:
        - ``TextFormatter`` (``trcc.ui.cli``)  → stdout text
        - ``JsonFormatter`` (``trcc.ui.api``)  → HTTP JSON body
        - ``WidgetFormatter`` (``trcc.ui.gui``) → Qt widget update
    """

    @abstractmethod
    def format(self, result: OpResult) -> Any: ...


class UI(ABC):
    """Read input → build `Command` via router → dispatch → format output.

    The shared surface is :meth:`handle`. The variable surface is
    :meth:`run` (lifecycle). Three concrete subclasses give us three UIs;
    every code path between input and dispatch is shared.
    """

    __slots__ = ('_dispatch', '_format', '_router')

    def __init__(self, dispatcher: Dispatcher, router: CommandRouter,
                 formatter: ResultFormatter) -> None:
        self._dispatch = dispatcher
        self._router = router
        self._format = formatter

    def handle(self, name: str, **kwargs: Any) -> OpResult:
        """Translate a named call into a `Command` and dispatch it.

        Every concrete UI funnels into this. CLI calls it from a Typer
        handler, API from a FastAPI route, GUI from a Qt slot — same
        signature, same effect, same `OpResult` shape returned.
        """
        return self._dispatch.dispatch(self._router.route(name, **kwargs))

    @abstractmethod
    def run(self) -> int:
        """Drive the UI until it exits. Returns a process exit code.

        Lifecycle differs per UI:
            - CLI: parse argv, dispatch one command, return.
            - API: ``uvicorn.run(...)`` — blocks on the HTTP server.
            - GUI: ``qapp.exec()`` — blocks on the Qt event loop.
        """
