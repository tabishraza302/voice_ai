"""Reject cross-site and DNS-rebinding writes to the local management API.

The management routes are unauthenticated because they are loopback-only.
That is only true if the *request* is loopback too: a page on another site
can POST a form to ``http://localhost:7860/settings/stt``, and a DNS
rebinding attack can make ``evil.example`` resolve to 127.0.0.1 so the
browser sends a same-origin request with ``Host: evil.example:7860``.

This guard is deliberately narrow:

* only state-changing methods are checked, so ordinary GET/HEAD/OPTIONS
  traffic (including CORS preflight) is untouched;
* an absent ``Origin`` is allowed, because curl and other non-browser
  clients do not send one;
* a present ``Origin`` must be the agent or Studio origin on a loopback
  host;
* ``Host`` must name ``localhost``, ``127.0.0.1`` or ``[::1]`` with the
  agent's own port, so a rebound DNS name is rejected even when the
  browser thinks the request is same-origin.
"""

from __future__ import annotations

from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from bot.config import Settings

STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def allowed_origins(settings: Settings) -> set[str]:
    """Browser origins that may send a state-changing request."""
    hosts = ("localhost", "127.0.0.1", "[::1]")
    ports = {settings.port, settings.web_port}
    return {f"http://{host}:{port}" for host in hosts for port in ports}


def allowed_hosts(settings: Settings) -> set[str]:
    """Host header values that name this agent on loopback."""
    return {
        f"localhost:{settings.port}",
        f"127.0.0.1:{settings.port}",
        f"[::1]:{settings.port}",
    }


def mount_local_state_guard(app: FastAPI, settings: Settings) -> None:
    """Install the guard once per app."""
    if getattr(app.state, "local_state_guard_mounted", False):
        return
    app.state.local_state_guard_mounted = True

    origins = allowed_origins(settings)
    hosts = allowed_hosts(settings)

    class LocalStateGuardMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            if request.method.upper() in STATE_CHANGING_METHODS:
                host = request.headers.get("host", "")
                if host.lower() not in hosts:
                    return JSONResponse(
                        status_code=403,
                        content={
                            "detail": (
                                "Request Host is not a loopback address for this agent. "
                                "Use localhost, 127.0.0.1 or [::1] with the agent port."
                            )
                        },
                    )
                origin = request.headers.get("origin")
                if origin is not None and origin not in origins:
                    return JSONResponse(
                        status_code=403,
                        content={
                            "detail": (
                                "Cross-origin state-changing requests are not allowed. "
                                "Use the local Studio or the agent's own page."
                            )
                        },
                    )
            return await call_next(request)

    app.add_middleware(LocalStateGuardMiddleware)
