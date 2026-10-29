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
* ``Host`` must name a literal loopback IP (127.0.0.0/8, ::1) or ``localhost``
  with the agent's actual bound port, so a rebound DNS name is rejected even
  when the browser thinks the request is same-origin.
"""

from __future__ import annotations

import ipaddress
import sys
from urllib.parse import urlsplit

from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from bot.config import Settings

STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def is_loopback_host(host: str) -> bool:
    """True if host is 'localhost' or a literal loopback IP address."""
    candidate = host.strip().lower()
    if not candidate:
        return False
    if candidate == "localhost":
        return True
    if candidate.startswith("[") and candidate.endswith("]"):
        candidate = candidate[1:-1]
    try:
        parsed = ipaddress.ip_address(candidate)
        return parsed.is_loopback
    except ValueError:
        return False


def parse_host_port(host_str: str) -> tuple[str, int | None]:
    """Parse host and optional port from a Host header or authority string."""
    host_str = host_str.strip()
    if not host_str:
        return "", None

    # Reject any userinfo, path, query, fragment, or whitespace injection
    if any(char in host_str for char in ("@", "/", "?", "#", " ", "\t")):
        return "", None

    if host_str.startswith("["):
        closing = host_str.find("]")
        if closing == -1:
            return "", None
        host = host_str[1:closing]
        rest = host_str[closing + 1:]
        if not rest:
            return host, None
        if rest.startswith(":"):
            try:
                port = int(rest[1:])
                if 1 <= port <= 65535:
                    return host, port
            except ValueError:
                pass
        return "", None

    if ":" in host_str:
        parts = host_str.split(":")
        if len(parts) == 2:
            try:
                port = int(parts[1])
                if 1 <= port <= 65535:
                    return parts[0], port
            except ValueError:
                pass
            return "", None
        # Bare unbracketed IPv6 literal without port
        try:
            ipaddress.ip_address(host_str)
            return host_str, None
        except ValueError:
            return "", None

    return host_str, None


def is_allowed_host(host_header: str, bound_port: int) -> bool:
    """Validate that the Host header names a loopback address with the agent's port."""
    try:
        host, port = parse_host_port(host_header)
        if not host or not is_loopback_host(host):
            return False
        if port is None:
            return bound_port == 80
        return port == bound_port
    except ValueError:
        return False


def is_allowed_origin(origin: str, allowed_ports: set[int]) -> bool:
    """Validate that the Origin header is a loopback origin on an allowed port."""
    origin = origin.strip()
    if not origin:
        return False
    try:
        parsed = urlsplit(origin)
        if parsed.scheme not in ("http", "https"):
            return False
        if parsed.username or parsed.password:
            return False
        if parsed.fragment or parsed.query:
            return False
        if parsed.path not in ("", "/"):
            return False
        host = parsed.hostname
        if not host or not is_loopback_host(host):
            return False
        port = parsed.port
        if port is None:
            port = 443 if parsed.scheme == "https" else 80
        return port in allowed_ports
    except ValueError:
        return False


def resolve_bound_port(request: Request, settings: Settings) -> int:
    """Determine the active port for the agent on this request.

    Priority:
    1. ASGI server endpoint in request.scope['server'] (uvicorn socket binding or
       TestClient with explicit base_url), unless it is the default synthetic
       TestClient host ('testserver', 80).
    2. Overrides on request.app.state (e.g. app.state.bound_port or app.state.port).
    3. pipecat.runner.run.RUNNER_PORT if runner was loaded and modified.
    4. Configured settings.port.
    """
    server = request.scope.get("server")
    if server and len(server) >= 2:
        server_host, server_port = server[0], server[1]
        if isinstance(server_port, int) and 1 <= server_port <= 65535:
            if server_host != "testserver" or server_port != 80:
                return server_port

    app = getattr(request, "app", None)
    if app is not None and hasattr(app, "state"):
        state_port = getattr(app.state, "bound_port", None) or getattr(app.state, "port", None)
        if isinstance(state_port, int) and 1 <= state_port <= 65535:
            return state_port

    runner = sys.modules.get("pipecat.runner.run")
    if runner is not None:
        runner_port = getattr(runner, "RUNNER_PORT", None)
        if isinstance(runner_port, int) and 1 <= runner_port <= 65535 and runner_port != 7860:
            return runner_port

    return settings.port


def allowed_origins(settings: Settings, port: int | None = None) -> set[str]:
    """Browser origins that may send a state-changing request for common loopback literals."""
    effective_port = port if port is not None else settings.port
    hosts = {"localhost", "127.0.0.1", "[::1]"}
    if settings.host and is_loopback_host(settings.host):
        h = settings.host.strip()
        if ":" in h and not h.startswith("["):
            h = f"[{h}]"
        hosts.add(h)
    ports = {effective_port, settings.web_port}
    return {f"http://{host}:{p}" for host in hosts for p in ports}


def allowed_hosts(settings: Settings, port: int | None = None) -> set[str]:
    """Host header values that name this agent on loopback for common loopback literals."""
    effective_port = port if port is not None else settings.port
    hosts = {"localhost", "127.0.0.1", "[::1]"}
    if settings.host and is_loopback_host(settings.host):
        h = settings.host.strip()
        if ":" in h and not h.startswith("["):
            h = f"[{h}]"
        hosts.add(h)
    return {f"{host}:{effective_port}" for host in hosts}


def mount_local_state_guard(
    app: FastAPI,
    settings: Settings,
    *,
    port: int | None = None,
) -> None:
    """Install the guard once per app."""
    if port is not None:
        app.state.bound_port = port

    if getattr(app.state, "local_state_guard_mounted", False):
        return
    app.state.local_state_guard_mounted = True

    class LocalStateGuardMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            if request.method.upper() in STATE_CHANGING_METHODS:
                bound_port = resolve_bound_port(request, settings)
                host = request.headers.get("host", "")
                if not is_allowed_host(host, bound_port):
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
                if origin is not None:
                    allowed_ports = {bound_port, settings.web_port}
                    if not is_allowed_origin(origin, allowed_ports):
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
