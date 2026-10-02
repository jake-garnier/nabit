"""Built-in result sinks.

A sink is just a callable `(VerificationResult) -> None` registered with
`nabit.add_sink(...)`. These ship in the box; anything fancier (OpenTelemetry,
Datadog, Kafka) is a three-line function you write and register — nabit never
imports those backends itself, so the core stays dependency-free.

OpenTelemetry example (you provide the dependency, not nabit):

    from opentelemetry import trace
    tracer = trace.get_tracer("nabit")

    def otel_sink(r):
        with tracer.start_as_current_span(f"nabit.verify.{r.name}") as span:
            span.set_attribute("nabit.passed", r.passed)
            span.set_attribute("nabit.attempts", r.attempts)
            if r.run_id:
                span.set_attribute("nabit.run_id", r.run_id)

    nabit.add_sink(otel_sink)
"""

from __future__ import annotations

import json
import threading
from typing import Callable


def jsonl_sink(path: str) -> Callable:
    """Return a sink that appends each VerificationResult as one JSON line to
    `path`. Thread-safe. Register with nabit.add_sink(jsonl_sink("hits.jsonl"))."""
    lock = threading.Lock()

    def _sink(result) -> None:
        line = json.dumps(result.to_dict(), default=str)
        with lock:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")

    return _sink
