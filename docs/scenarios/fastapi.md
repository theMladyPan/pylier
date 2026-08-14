# FastAPI integration

`pylier.instrument_fastapi(app)` installs a **pure-ASGI middleware** that opens
one `pylier.trace("<method> <path>")` per HTTP request and writes the response
status to `Trace.metadata["status_code"]`. Endpoints stay un-decorated; decorate
the service functions a handler calls and they appear in that request's graph.

## Install

```bash
uv add "pylier[fastapi]"
```

The integration module lives behind the `[fastapi]` extra.

## Example

Based on [`examples/fastapi_app.py`](https://github.com/theMladyPan/pylier/blob/master/examples/fastapi_app.py):

```python
from fastapi import FastAPI

import pylier
from pylier.integrations.fastapi import instrument_fastapi


@pylier.node
def lookup(item_id: int) -> dict[str, object]:
    return {"id": item_id, "name": f"widget-{item_id}"}


@pylier.node
def price(item: dict[str, object], qty: int) -> int:
    return 10 * qty


app = FastAPI()
instrument_fastapi(app)


@app.get("/items/{item_id}")
def items(item_id: int, qty: int = 1) -> dict[str, object]:
    item = lookup(item_id)          # appears as a node in this request's graph
    total = price(item, qty)        # ditto — its input flows from `item`
    return {"item": item, "total": total}
```

Run it and watch requests flow as a live graph:

```bash
uv run --group examples uvicorn examples.fastapi_app:app --port 8000
```

## What it does

- **Pure ASGI** — no route mutation, no `APIRoute.endpoint` wrapping. A manual
  edge API or route-mutation approach was deliberately rejected for v1 as
  version-fragile; the middleware alone gives the logfire "just decorate" feel.
- **Per-request traces** — each request is its own trace in the live viewer
  history, capped at 100 by `TraceHistory`.
- **Neutral metadata** — `Trace.metadata` is a primitive-value bag. Integrations
  own their keys; core never models an HTTP endpoint. FastAPI writes
  `status_code`; your integration can write its own keys.

!!! note "Lazy import"
    Importing `pylier` never pulls in FastAPI. `pylier.instrument_fastapi`
    delegates to `pylier.integrations.fastapi`, which is imported only when
    called and lives behind the `[fastapi]` extra.

!!! warning "Non-HTTP scopes pass through untraced"
    Lifespan and websocket scopes are not HTTP requests, so the middleware does
    not open a trace for them.
