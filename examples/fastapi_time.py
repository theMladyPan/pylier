"""Minimal OTel-compatible FastAPI example for pylier.

Run with::

    uv sync --group examples
    uv run --group examples examples/fastapi_time.py

Then request::

    curl 'http://127.0.0.1:8000/time?location=Europe/Bratislava'

OpenTelemetry owns the incoming HTTP server span. pylier reads that active span
and renders the decorated endpoint as the request's algorithm node.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, HTTPException, Query
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.trace import TracerProvider

import pylier

trace.set_tracer_provider(TracerProvider())
app = FastAPI(title="pylier FastAPI time example")

# Install pylier first. FastAPI's OTel middleware is then outermost, so the
# request server span is active when pylier begins recording the endpoint.
pylier.instrument_fastapi(app)
FastAPIInstrumentor.instrument_app(app)


@app.get("/time")
@pylier.node
async def current_time(location: str = Query(description="IANA timezone, e.g. Europe/Bratislava")) -> dict[str, str]:
    """Return the current local time for an IANA timezone.

    Args:
        location: IANA timezone identifier such as ``Europe/Bratislava``.

    Returns:
        The requested timezone and its current ISO 8601 timestamp.

    Raises:
        HTTPException: If ``location`` is not a valid IANA timezone.
    """
    try:
        local_time = datetime.now(ZoneInfo(location))
    except ZoneInfoNotFoundError as error:
        raise HTTPException(status_code=400, detail=f"Unknown IANA timezone: {location}") from error
    return {"location": location, "time": local_time.isoformat()}


if __name__ == "__main__":
    import uvicorn

    pylier.serve(port=8765)
    uvicorn.run(app, host="127.0.0.1", port=8000)
