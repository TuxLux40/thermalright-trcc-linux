"""Daemon-control endpoints under ``/trcc/``.

Currently just one: ``POST /trcc/kill`` to stop the running daemon. The
existing ``/system`` and ``/devices`` namespaces are device- and
metrics-shaped; daemon-control is conceptually different (lifecycle of
the singleton process itself), so it lives under its own prefix.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter

log = logging.getLogger(__name__)

router = APIRouter(prefix="/trcc", tags=["trcc"])


@router.post("/kill")
def kill() -> dict:
    """Stop the running TRCC daemon.

    Returns ``{"success": true}`` once the daemon has shut down (or
    ``{"success": false}`` on timeout). The API server itself keeps
    running — only the daemon process the API was proxying to dies.
    Subsequent UI calls will auto-spawn a fresh daemon on demand.
    """
    from trcc.daemon import kill_daemon
    return {"success": kill_daemon()}
