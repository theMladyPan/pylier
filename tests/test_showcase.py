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


def test_showcase_embeds_its_user_guide_in_the_module_docstring():
    guide = showcase.__doc__ or ""

    assert "uv run python -m examples.showcase html" in guide
    assert "pylier-fulfillment-showcase.html" in guide
    assert "pylier-fulfillment-info.html" in guide
    assert "pylier-fulfillment.jsonl" in guide
    assert "uv run python -m examples.showcase serve" in guide
    assert "0.75-second pause" in guide
    assert "concurrent inventory and shipping branches" in guide
    assert "cross-process live viewer input" in guide
    assert not (Path(__file__).parents[1] / "docs" / "showcase-guide.md").exists()


def test_debug_showcase_exercises_supported_graph_semantics():
    trace = asyncio.run(showcase.run_fulfillment("showcase-debug", level=Level.DEBUG))
    graph = trace.to_graph_dict()

    node_names = {node["name"] for node in graph["nodes"]}
    payload_types = {edge["payload"] for edge in graph["links"]}
    tagged_nodes = {node["name"]: node["tags"] for node in graph["nodes"]}

    edge_pairs = {(edge["source"], edge["target"]) for edge in graph["links"]}
    data_edge_pairs = {(edge["source"], edge["target"]) for edge in graph["perspectives"]["data"]["links"]}

    assert "audit_risk" in node_names
    assert "is_priority_order" not in node_names
    assert {"int", "float", "str", "list", "dict", "set", "tuple", "bytes"} <= payload_types
    assert {"RiskAssessment", "FulfillmentPackage"} <= payload_types

    def showcase_node(name: str) -> str:
        return f"pylier_showcase.{name}"

    root_entries = {target for source, target in edge_pairs if source == trace.root_node_id}
    assert root_entries == {showcase_node("fulfill_order")}
    assert (showcase_node("fulfill_order"), showcase_node("assess_order")) in edge_pairs
    assert (showcase_node("fulfill_order"), showcase_node("prepare_inventory")) in edge_pairs
    assert (showcase_node("fulfill_order"), showcase_node("prepare_shipping")) in edge_pairs
    assert (showcase_node("fulfill_order"), showcase_node("finalize_fulfillment")) in edge_pairs
    assert (showcase_node("prepare_inventory"), showcase_node("rank_items")) in edge_pairs
    assert (showcase_node("prepare_inventory"), showcase_node("expand_rank")) in edge_pairs
    assert (showcase_node("prepare_shipping"), showcase_node("shipping_zone")) in edge_pairs
    assert (showcase_node("finalize_fulfillment"), showcase_node("assemble_fulfillment")) in edge_pairs
    assert (showcase_node("prepare_inventory"), showcase_node("finalize_fulfillment")) in data_edge_pairs
    assert (showcase_node("prepare_shipping"), showcase_node("finalize_fulfillment")) in data_edge_pairs
    invocations_by_node = {invocation.node_id: invocation for invocation in trace.invocations.values()}
    workflow_invocation = invocations_by_node[showcase_node("fulfill_order")]
    inventory_invocation = invocations_by_node[showcase_node("prepare_inventory")]
    shipping_invocation = invocations_by_node[showcase_node("prepare_shipping")]
    finalization_invocation = invocations_by_node[showcase_node("finalize_fulfillment")]
    assert inventory_invocation.parent_invocation_id == workflow_invocation.id
    assert shipping_invocation.parent_invocation_id == workflow_invocation.id
    assert finalization_invocation.parent_invocation_id == workflow_invocation.id
    assert (
        len(
            [
                invocation
                for invocation in trace.invocations.values()
                if invocation.node_id == showcase_node("rank_items")
            ]
        )
        == 2
    )
    assert tagged_nodes["rank_items"] == ["inventory", "loop"]
    assert tagged_nodes["reserve_inventory"] == ["inventory", "async"]
    assert any(node["is_async"] for node in graph["nodes"])
    assert "tuple" in payload_types
    assert any(edge["value"] is not None for edge in graph["links"])


def test_info_showcase_omits_the_debug_only_risk_node():
    trace = asyncio.run(showcase.run_fulfillment("showcase-info", level=Level.INFO))

    node_names = {node.name for node in trace.nodes.values()}

    assert "audit_risk" not in node_names
    assert {"fulfill_order", "prepare_inventory", "prepare_shipping"} <= node_names
    assert len(trace.nodes) == 18


def test_showcase_writes_static_artifacts_and_resolved_sidecar(tmp_path):
    artifacts = showcase.write_html_artifacts(tmp_path)

    assert artifacts.full_html.exists()
    assert artifacts.info_html.exists()
    assert artifacts.sidecar.exists()
    assert "fulfillment-showcase" in artifacts.full_html.read_text(encoding="utf-8")
    sidecar_events = [json.loads(line) for line in artifacts.sidecar.read_text(encoding="utf-8").splitlines()]
    assert any(event["tags"] == ["inventory", "async"] for event in sidecar_events)
    assert all("payload" in edge for event in sidecar_events for edge in event["edges"])
