"""The STT mode switch, and the page that sets it.

The two local STT providers are the same checkpoint on two transports:

* ``mlx-audio`` — one HTTP request per finished turn. It takes a
  language field, and it is the transport whose WER the README publishes.
* ``mlx-audio-realtime`` — one WebSocket per session; the server
  transcribes while the user speaks and returns the text on commit. The
  default, and the faster of the two.

Which one a session uses is a property of that session, so the choice
lives here rather than in the pipeline: `.env` sets the default, and this
switch overrides it for the life of the process. It takes effect when the
next client connects, because the service is built with the pipeline —
which is why the page's Start link opens a fresh session.

The page is served by the runner's own FastAPI app, the same one that
serves ``/client`` and ``/metrics``. No second server, no build step.
The prebuilt client page carries the same switch as a corner overlay,
because that is the URL the README and the shell script hand people —
a switch reachable only from ``/settings`` would be undiscoverable from
the page they are told to open.
"""

from __future__ import annotations

import importlib.util
import re
from functools import lru_cache
from html import escape
from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse

from bot.config import Settings
from bot.local_guard import mount_local_state_guard
from bot.services import resolve_models

BATCH_PROVIDER = "mlx-audio"
STREAMING_PROVIDER = "mlx-audio-realtime"
MANAGED_PROVIDERS = (BATCH_PROVIDER, STREAMING_PROVIDER)
LABELS = {BATCH_PROVIDER: "Batch", STREAMING_PROVIDER: "Streaming"}

# Where a form post may send the browser back to. Fixed on purpose: the
# value comes from the request, and a local page can still be used as an
# open redirect.
RETURN_PATHS = ("/settings", "/client/")

# The process-wide override. None means "whatever .env says". A single
# assignment is atomic, and both readers and writers run on the event
# loop, so no lock is needed.
_override: str | None = None


def set_provider(provider: str | None) -> None:
    """Override the STT provider, or clear the override with None."""
    global _override
    if provider is not None and provider not in MANAGED_PROVIDERS:
        raise ValueError(
            f"unknown stt provider {provider!r}; this page switches between "
            f"{' and '.join(MANAGED_PROVIDERS)}"
        )
    _override = provider


def effective_provider(settings: Settings) -> str:
    """The provider the next session will use."""
    return _override or settings.stt_provider


def apply_mode(settings: Settings) -> Settings:
    """Point a fresh Settings at the chosen provider, model pairing included.

    ``resolve_models`` runs here for the same reason the factories call
    it: the switch changes the provider, and a provider swap that leaves
    the other provider's checkpoint behind must be corrected once.
    """
    settings.stt_provider = effective_provider(settings)
    return resolve_models(settings)


def render_page(configured: str, effective: str, note: str = "") -> str:
    """The control page. Self-contained: no scripts, no external assets.

    ``note`` is trusted HTML built by ``mount_control`` (it carries the
    escaped error text), never user input.
    """

    def option(provider: str, title: str, detail: str) -> str:
        checked = " checked" if provider == effective else ""
        return (
            f'<label><input type="radio" name="provider" value="{provider}"{checked}>'
            f"<span><b>{title}</b><span>{detail}</span></span></label>"
        )

    state = f"Now: <b>{escape(LABELS.get(effective, effective))}</b>"
    if effective != configured:
        state += f' <span class="muted">(overrides .env: {escape(configured)})</span>'

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>voice-ai — speech to text mode</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font: 16px/1.5 -apple-system, system-ui, sans-serif; max-width: 36rem;
         margin: 4rem auto; padding: 0 1.5rem; }}
  h1 {{ font-size: 1.35rem; margin: 0 0 .25rem; }}
  p {{ margin: .5rem 0; }}
  .muted {{ opacity: .65; }}
  form {{ margin: 2rem 0 1.5rem; display: grid; gap: .75rem; }}
  label {{ display: grid; grid-template-columns: auto 1fr; gap: .75rem;
           align-items: start; padding: .9rem 1rem; border: 1px solid;
           border-radius: .6rem; cursor: pointer; }}
  label:has(input:checked) {{ box-shadow: 0 0 0 2px currentColor inset; }}
  label b {{ display: block; }}
  label span span {{ font-size: .9rem; opacity: .7; }}
  button {{ font: inherit; padding: .6rem 1.1rem; border-radius: .5rem;
            border: 1px solid; background: transparent; cursor: pointer;
            justify-self: start; }}
  .start {{ display: inline-block; margin-top: .5rem; font-weight: 600;
            font-size: 1.05rem; }}
  .note {{ border-left: 3px solid; padding-left: .9rem; }}
</style>
</head>
<body>
<h1>voice-ai</h1>
<p class="muted">English + Hindi, fully local.</p>

<p>{state}</p>
{note}

<form method="post" action="/settings/stt">
  {option(BATCH_PROVIDER, "Batch", "Each finished turn is one HTTP request to the local server. Slower to the transcript, but it takes a language field; the README's WER numbers were measured on it.")}
  {option(STREAMING_PROVIDER, "Streaming", "One WebSocket per session. The server transcribes while you speak, so the text lands right after the turn ends. The default, and the faster of the two. Auto language only.")}
  <button>Apply</button>
</form>

<p><a class="start" href="/client/">Start talking &#8594;</a></p>
<p class="muted">A session keeps the mode it started with. Apply, then
start (or reconnect) the client.</p>
</body>
</html>
"""


def mount_control(app, settings: Settings) -> None:
    """Serve the switch at ``/``, ``/settings`` and on ``/client``.

    The ``/`` and ``/settings`` routes are registered before the runner
    mounts its own ``/`` -> ``/client/`` redirect, so this page is the
    landing page and the link to the client is on it. The ``/client``
    routes are registered before the runner mounts the prebuilt UI, so
    they shadow its index and the rest of its assets keep being served
    by the mount underneath.
    """
    mount_local_state_guard(app, settings)

    def page(note: str = "", status_code: int = 200) -> HTMLResponse:
        effective = effective_provider(settings)
        if not note and effective not in MANAGED_PROVIDERS:
            # .env points at a provider this page does not switch (today
            # that is mlx-whisper). Neither radio is checked, and Apply
            # would post nothing, so say why instead.
            note = (
                f'<p class="note"><code>{escape(effective)}</code> comes from '
                "the environment and is not one of the two modes below. "
                "Choosing a mode overrides it for this process.</p>"
            )
        return HTMLResponse(
            render_page(
                configured=settings.stt_provider,
                effective=effective,
                note=note,
            ),
            status_code=status_code,
        )

    @app.get("/", include_in_schema=False)
    @app.get("/settings", include_in_schema=False)
    async def control_page() -> HTMLResponse:
        return page()

    @app.get("/client", include_in_schema=False)
    async def client_redirect() -> RedirectResponse:
        # The prebuilt page loads its assets relative to its own URL, so
        # served at "/client" without the slash they resolve to "/" and
        # the app comes up empty. The runner's mount redirects here too;
        # this route shadows it, so it has to keep that behaviour.
        return RedirectResponse("/client/", status_code=307)

    @app.get("/client/", include_in_schema=False)
    async def client_page():
        index = _client_index_html()
        if index is None:
            # The prebuilt UI is not installed. The switch page says so
            # better than a bare 404 from a static mount would.
            return RedirectResponse("/settings", status_code=307)
        return HTMLResponse(inject_client_bar(index, effective_provider(settings)))

    @app.post("/settings/stt", include_in_schema=False)
    async def set_stt(request: Request):
        provider, target = await _requested_change(request)
        try:
            set_provider(provider)
        except ValueError as exc:
            return page(note=f'<p class="note">{escape(str(exc))}</p>', status_code=400)
        return RedirectResponse(target, status_code=303)


_BAR_BUTTON = (
    "font:inherit;padding:.25rem .6rem;border-radius:.4rem;"
    "border:1px solid currentColor;background:transparent;color:inherit;"
    "cursor:pointer"
)


def render_client_bar(effective: str) -> str:
    """The switch as an overlay on the prebuilt client page.

    Inline styles only: this element is injected into a page that ships
    its own stylesheet, so it must neither inherit from it nor depend on
    a class name that could collide. The active mode is a disabled
    button — it is the current value, and posting it would change
    nothing.
    """
    buttons = []
    for provider, label in LABELS.items():
        if provider == effective:
            buttons.append(
                f'<button type="button" disabled style="{_BAR_BUTTON};opacity:.55">'
                f"{label} · active</button>"
            )
        else:
            buttons.append(
                f'<button name="provider" value="{provider}" style="{_BAR_BUTTON}">'
                f"{label}</button>"
            )
    return (
        '<div id="voice-ai-stt-bar" style="position:fixed;right:1rem;bottom:1rem;'
        "z-index:2147483647;font:13px/1.4 -apple-system,system-ui,sans-serif;"
        "color:CanvasText;background:Canvas;border:1px solid GrayText;"
        'border-radius:.6rem;padding:.45rem .6rem;box-shadow:0 2px 10px rgba(0,0,0,.18)">'
        '<form method="post" action="/settings/stt" '
        'style="display:flex;align-items:center;gap:.45rem;margin:0">'
        '<input type="hidden" name="next" value="/client/">'
        '<span style="opacity:.7">STT</span>'
        f'{"".join(buttons)}'
        '<span style="opacity:.7">next connection</span>'
        "</form></div>"
    )


def inject_client_bar(index_html: str, effective: str) -> str:
    """Put the switch on the prebuilt client page, before ``</body>``."""
    bar = render_client_bar(effective)
    match = re.search(r"</body\s*>", index_html, re.IGNORECASE)
    if match is None:
        return index_html + bar
    return index_html[: match.start()] + bar + index_html[match.start() :]


@lru_cache(maxsize=1)
def _client_index_html() -> str | None:
    """The prebuilt client's ``index.html``, or None when it is absent.

    Found through the installed package instead of importing its
    ``frontend`` module: that module prints its search paths on import,
    and this module is imported by the tests.
    """
    spec = importlib.util.find_spec("pipecat_ai_prebuilt")
    if spec is None or not spec.submodule_search_locations:
        return None
    root = Path(next(iter(spec.submodule_search_locations)))
    try:
        return (root / "client" / "dist" / "index.html").read_text(encoding="utf-8")
    except OSError:
        return None


async def _requested_change(request: Request) -> tuple[str | None, str]:
    """Read the provider and the page to return to, from form or JSON."""
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("application/json"):
        body = await request.json()
        body = body if isinstance(body, dict) else {}
        provider = body.get("provider")
        target = body.get("next")
    else:
        form = await request.form()
        provider = form.get("provider")
        target = form.get("next")
    if provider in ("", "default"):
        provider = None
    if target not in RETURN_PATHS:
        target = "/settings"
    return provider, target
