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

import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, HTTPException, Query
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.sqlite3 import SQLite3Instrumentor
from opentelemetry.sdk.trace import TracerProvider
from pydantic import BaseModel

import pylier

DATABASE_PATH = Path(__file__).with_name("fastapi_time.sqlite3")


class LocationCreate(BaseModel):
    """JSON body accepted by the location creation endpoint."""

    location: str


trace.set_tracer_provider(TracerProvider())
SQLite3Instrumentor().instrument()
app = FastAPI(title="pylier FastAPI time example")

# Install pylier first. FastAPI's OTel middleware is then outermost, so the
# request server span is active when pylier begins recording the endpoint.
pylier.instrument_fastapi(app)
FastAPIInstrumentor.instrument_app(app)


def _create_locations_table() -> None:
    with sqlite3.connect(DATABASE_PATH) as connection:
        cursor = connection.cursor()
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS locations ("
            "id INTEGER PRIMARY KEY, location TEXT NOT NULL, created_at TEXT NOT NULL)"
        )


@pylier.node
def insert_location(location: str) -> dict[str, str | int]:
    """Write a location row through instrumented sqlite3."""
    created_at = datetime.now(ZoneInfo("UTC")).isoformat()
    with sqlite3.connect(DATABASE_PATH) as connection:
        cursor = connection.cursor()
        cursor.execute("INSERT INTO locations (location, created_at) VALUES (?, ?)", (location, created_at))
        location_id = cursor.lastrowid
    return {"id": location_id, "location": location, "created_at": created_at}


@app.post("/locations", status_code=201)
@pylier.node
async def create_location(body: LocationCreate) -> dict[str, str | int]:
    """Persist a location and return its identifier."""
    return insert_location(body.location)


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

    _create_locations_table()
    pylier.serve(port=8765)
    uvicorn.run(app, host="127.0.0.1", port=8000)
