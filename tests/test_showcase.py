"""Deterministic coverage for the evaluator/developer fulfillment showcase."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

from pylier.model import Level

_SHOWCASE_PATH = Path(__file__).parents[1] / "examples" / "showcase.py"
_SHOWCASE_SPEC = importlib.util.spec_from_file_location("pylier_showcase", _SHOWCASE_PATH)
assert _SHOWCASE_SPEC is not None and _SHOWCASE_SPEC.loader is not None
showcase = importlib.util.module_from_spec(_SHOWCASE_SPEC)
sys.modules[_SHOWCASE_SPEC.name] = showcase
_SHOWCASE_SPEC.loader.exec_module(showcase)


def test_debug_showcase_exercises_supported_graph_semantics():
    trace = asyncio.run(showcase.run_fulfillment("showcase-debug", level=Level.DEBUG))
    graph = trace.to_graph_dict()

    node_names = {node["name"] for node in graph["nodes"]}
    payload_types = {edge["payload"] for edge in graph["links"]}
    tagged_nodes = {node["name"]: node["tags"] for node in graph["nodes"]}

    assert "audit_risk" in node_names
    assert {"bool", "int", "float", "str", "list", "dict", "set", "tuple", "bytes"} <= payload_types
    assert {"RiskAssessment", "FulfillmentPackage"} <= payload_types
    assert tagged_nodes["reserve_inventory"] == ["inventory", "async"]
    assert any(node["is_async"] for node in graph["nodes"])
    assert any(edge["payload_types"] == ["bool", "float", "str"] for edge in graph["links"])
    assert any(edge["value"] is not None for edge in graph["links"])


def test_info_showcase_omits_the_debug_only_risk_node():
    trace = asyncio.run(showcase.run_fulfillment("showcase-info", level=Level.INFO))

    assert all(node.name != "audit_risk" for node in trace.nodes.values())
    assert len(trace.nodes) < 13


def test_showcase_writes_static_artifacts_and_resolved_sidecar(tmp_path):
    artifacts = showcase.write_html_artifacts(tmp_path)

    assert artifacts.full_html.exists()
    assert artifacts.info_html.exists()
    assert artifacts.sidecar.exists()
    assert "fulfillment-showcase" in artifacts.full_html.read_text(encoding="utf-8")
    sidecar_events = [json.loads(line) for line in artifacts.sidecar.read_text(encoding="utf-8").splitlines()]
    assert any(event["tags"] == ["inventory", "async"] for event in sidecar_events)
    assert all("payload" in edge for event in sidecar_events for edge in event["edges"])
