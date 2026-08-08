"""FastAPI + pylier example: each request becomes a trace in the live viewer.

Run::

    uv run --group examples uvicorn examples.fastapi_app:app --port 8000

In another shell, hit the API::

    curl 'http://localhost:8000/items/42?qty=3'

Decorated service functions (`lookup`, `price`) appear as nodes inside the
request's graph; the live viewer lists the newest 100 requests in its history.
"""

from __future__ import annotations

# FastAPI is an optional dependency (the [fastapi] extra). Importing it here is
# fine for a runnable example; the integration module itself lazy-imports it.
from fastapi import FastAPI

import pylier
from pylier.integrations.fastapi import instrument_fastapi


@pylier.node
def lookup(item_id: int) -> dict[str, object]:
    return {"id": item_id, "name": f"widget-{item_id}"}


@pylier.node
def price(item: dict[str, object], qty: int) -> int:
    base = 10
    return base * qty


app = FastAPI()
instrument_fastapi(app)


@app.get("/items/{item_id}")
@pylier.node(_tags=["API"])
def items(item_id: int, qty: int = 1) -> dict[str, object]:
    item = lookup(item_id)
    total = price(item, qty)
    return {"item": item, "total": total}


# To watch requests flow as a live graph, run pylier's viewer in a separate
# thread/process against this app's in-memory trace history, e.g. start the
# API with uvicorn (above) and, in the same program, call ``pylier.serve()``
# on another port from a background thread.


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    try:
        visualizer = pylier.serve(port=8765)
        uvicorn.run(app, host="0.0.0.0", port=8000)
    finally:
        visualizer.shutdown()
