"""FastAPI integration: per-request trace via the ASGI middleware."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi import FastAPI
from fastapi.testclient import TestClient

import pylier


def test_instrument_fastapi_opens_one_trace_per_request():
    app = FastAPI()

    @pylier.node
    def upper(s: str) -> str:
        return s.upper()

    @app.get("/echo/{name}")
    def echo(name: str):
        return {"result": upper(name)}

    pylier.instrument_fastapi(app)

    with TestClient(app) as client:
        r1 = client.get("/echo/alice")
        r2 = client.get("/echo/bob")
    assert r1.json() == {"result": "ALICE"}
    assert r2.json() == {"result": "BOB"}

    from pylier.recorder import trace_history

    traces = trace_history().snapshot_traces()
    names = {t.name for t in traces}
    # One trace per request, named "<method> <path>".
    assert "GET /echo/alice" in names
    assert "GET /echo/bob" in names

    # The decorated service function became a node in each request's graph.
    # Node names are the full __qualname__, so a locally-defined helper shows up
    # as "...<locals>.upper" — match by suffix.
    alice = next(t for t in traces if t.name == "GET /echo/alice")
    assert any(n.name.endswith(".upper") or n.name == "upper" for n in alice.nodes.values())
    # The adapter owns this transport-specific key; the core metadata bag is
    # intentionally generic for every kind of trace.
    assert alice.metadata["status_code"] == 200


def test_instrument_fastapi_returns_app_for_chaining():
    app = FastAPI()
    assert pylier.instrument_fastapi(app) is app


def test_instrument_fastapi_rejects_non_starlette_app():
    with pytest.raises(AttributeError):
        pylier.instrument_fastapi(object())


def test_non_http_scopes_pass_through_untraced():
    # Lifespan startup must not create a trace; only http scopes do.
    from pylier.recorder import trace_history

    # History is a session-global singleton shared across tests, so only assert
    # about traces created during *this* test, not the whole history.
    before = {t.id for t in trace_history().snapshot_traces()}

    app = FastAPI()

    @app.get("/x")
    def x():
        return {}

    pylier.instrument_fastapi(app)

    with TestClient(app) as client:
        client.get("/x")
        client.get("/x")

    new = [t for t in trace_history().snapshot_traces() if t.id not in before]
    assert len(new) == 2
    assert all(t.name.startswith("GET ") for t in new)
    # No "lifespan" or startup trace leaked into history.
    assert not any("lifespan" in t.name for t in new)
