"""Export agent traces over OTLP/HTTP, e.g. to the Phoenix service in compose."""

from openinference.instrumentation.pydantic_ai import OpenInferenceSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from pydantic_ai import Agent
from pydantic_ai.models.instrumented import InstrumentationSettings


def enable_tracing(endpoint: str) -> TracerProvider:
    """Instrument every agent and send spans to ``endpoint``; call ``shutdown`` at exit."""
    provider = TracerProvider()
    provider.add_span_processor(OpenInferenceSpanProcessor())
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    Agent.instrument_all(
        InstrumentationSettings(tracer_provider=provider, include_binary_content=False)
    )
    return provider
