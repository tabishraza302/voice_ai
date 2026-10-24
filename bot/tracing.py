"""OpenTelemetry tracing into the local collector.

**One call, once, at process start.** ``setup_tracing`` installs the
global tracer provider and the OTLP exporter. It must run *before* the
pipeline is constructed, because Pipecat's tracing decorators capture the
tracer at decoration time — a provider installed afterwards is not picked
up by the spans that were already created.

The endpoint is the local collector's gRPC receiver on port 4319, avoiding
default 4317 collisions on developer workstations.
"""

from __future__ import annotations

from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from pipecat.utils.tracing.setup import setup_tracing as pipecat_setup_tracing

from bot.config import Settings

# The service name the traces appear under in Phoenix.
SERVICE_NAME = "voice-ai"


def setup_tracing(settings: Settings, *, console_export: bool = False) -> None:
    """Install the OTLP exporter and the Pipecat tracing setup.

    Args:
        settings: The runtime settings; ``otlp_endpoint`` is the target.
        console_export: Also print spans to stdout. Off by default: it is
            noisy and slows the pipeline down.
    """
    exporter = OTLPSpanExporter(endpoint=settings.otlp_endpoint, insecure=True)
    pipecat_setup_tracing(
        service_name=SERVICE_NAME,
        exporter=exporter,
        console_export=console_export,
    )
