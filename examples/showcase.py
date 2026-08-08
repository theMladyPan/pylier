"""Runnable evaluator and developer guide for the pylier fulfillment showcase.

Quick start:

    uv run python -m examples.showcase html

The ``html`` command writes three artifacts in the working directory:

* ``pylier-fulfillment-showcase.html`` is the complete debug-level interactive
  graph.
* ``pylier-fulfillment-info.html`` records the same flow at ``INFO`` level;
  its debug-only ``audit_risk`` node is intentionally absent.
* ``pylier-fulfillment.jsonl`` contains resolved sidecar events for offline
  inspection.

Open the full HTML artifact in a browser. Click nodes to inspect their tags,
click edges to inspect their harmless synthetic captured payloads, choose tags
in the left filter pane, and compare the full graph with the INFO artifact.

What to look for in this fulfillment flow:

* Application Flow begins at one ``fulfill_order`` coordinator, then branches
  into assessment, inventory, and shipping work before reconverging for
  finalization. The concurrent inventory and shipping branches are visible
  independently in the live view.
* Decorated return values are passed directly into decorated consumers, so
  pylier infers every edge without explicit wiring.
* Payload edges cover ``int``, ``float``, ``str``, ``list``, ``dict``,
  ``set``, ``bytes``, and custom application objects; the viewer colors them
  by type. The approval tuple also carries its ``bool`` member type.
* ``validate_order → assemble_fulfillment`` carries a mixed
  ``tuple[bool, float, str]`` edge, rendered as a tuple gradient.
* ``rank_items → expand_rank → rank_items`` is a real inferred data-flow
  cycle. It demonstrates bounded repeated work without recursive calls.
* Tags such as ``order``, ``inventory``, ``shipping``, ``loop``, and ``async``
  power both the filter pane and node inspector.
* ``reserve_inventory``, ``quote_shipping``, and
  ``purchase_shipping_label`` are decorated async nodes. The concurrent
  inventory and shipping branches finish at different times in the live view.
* ``audit_risk`` is declared at ``level=\"debug\"`` to make the capture-level
  comparison visible.
* The showcase enables value capture only for these harmless synthetic
  payloads; pylier keeps captured values opt-in by default.
* Opening static HTML replays its recorded enter/exit execution timeline.
* The JSONL sidecar records already-resolved node and edge events and never
  re-fingerprints payloads.

Live SSE walkthrough:

    uv run python -m examples.showcase serve

The command starts ``pylier.serve()`` for the active in-memory trace, opens the
viewer, and autoplays each fulfillment stage with a 0.75-second pause. It also
writes ``pylier-fulfillment-live.jsonl`` and waits for Enter after completion
so the final graph remains inspectable. Use ``--stage-delay 0.4`` for a faster
tour or ``--output-dir demo-output`` to collect artifacts elsewhere.

The live viewer follows one in-memory trace only. The JSONL sidecar is an
offline resolved artifact, not a cross-process live viewer input.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pylier
from pylier.config import reload_settings
from pylier.model import Level, Trace


@dataclass(frozen=True)
class RiskAssessment:
    """Synthetic debug-only fraud assessment passed to fulfillment assembly."""

    score: float
    reason: str


@dataclass(frozen=True)
class FulfillmentPackage:
    """Custom payload proving that unknown application types stay visible."""

    order_id: str
    warehouse: str
    shipping_cost: float
    label: bytes


@dataclass(frozen=True)
class InventoryPlan:
    """Reservation and refined item count produced by the inventory branch."""

    reservation: dict[str, Any]
    item_count: int


@dataclass(frozen=True)
class ShowcaseArtifacts:
    """Files produced by the static showcase command."""

    full_html: Path
    info_html: Path
    sidecar: Path


@pylier.node(_tags=["order", "input"])
def create_order() -> dict[str, Any]:
    """Create one synthetic customer order."""
    return {
        "id": "ord-1042",
        "customer": "Ada Lovelace",
        "destination": "EU-CENTRAL",
        "items": [
            {"sku": "keyboard", "quantity": 1, "unit_price": 129.0},
            {"sku": "cable", "quantity": 2, "unit_price": 9.5},
        ],
    }


@pylier.node(_tags=["order", "validation"])
def validate_order(order: dict[str, Any]) -> tuple[bool, float, str]:
    """Return a mixed approval tuple for the tuple-gradient edge."""
    return True, 12.75, "approved"


@pylier.node(_tags=["order", "items"])
def extract_items(order: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract line items while preserving the direct order handoff."""
    return list(order["items"])


@pylier.node(_tags=["shipping", "zone"])
def shipping_zone(order: dict[str, Any]) -> str:
    """Read the destination zone for downstream shipping pricing."""
    return str(order["destination"])


@pylier.node(_tags=["shipping", "items"])
def count_shipping_items(order: dict[str, Any]) -> int:
    """Count ordered units independently for the shipping branch."""
    return sum(int(item["quantity"]) for item in order["items"])


@pylier.node(_tags=["inventory", "warehouses"])
def choose_warehouses(items: list[dict[str, Any]]) -> set[str]:
    """Select candidate warehouses for the required item set."""
    return {"brno-01", "prague-02"} if items else set()


@pylier.node(_tags=["inventory", "loop"])
def rank_items(items: list[dict[str, Any]]) -> int:
    """Reduce items to a synthetic rank used by the bounded refinement loop."""
    return sum(int(item["quantity"]) for item in items)


@pylier.node(_tags=["inventory", "loop"])
def expand_rank(rank: int) -> list[dict[str, Any]]:
    """Expand a rank back into items so the next rank call closes the cycle."""
    return [{"sku": "refined", "quantity": 1} for _ in range(rank)]


@pylier.node(level="debug", _tags=["risk", "debug"])
def audit_risk(order: dict[str, Any]) -> RiskAssessment:
    """Produce a debug-only custom payload for the level comparison artifact."""
    return RiskAssessment(score=0.08, reason=f"synthetic check for {order['id']}")


@pylier.node(_tags=["inventory", "async"])
async def reserve_inventory(items: list[dict[str, Any]], warehouses: set[str]) -> dict[str, Any]:
    """Reserve stock asynchronously from the selected warehouses."""
    await asyncio.sleep(0)
    return {
        "reservation": "res-771",
        "warehouse": sorted(warehouses)[0],
        "sku_count": len(items),
    }


@pylier.node(_tags=["shipping", "async"])
async def quote_shipping(zone: str, item_count: int) -> float:
    """Quote shipping asynchronously from a zone and item count."""
    await asyncio.sleep(0)
    return round(4.99 + item_count * 1.25 + (1.5 if zone == "EU-CENTRAL" else 0), 2)


@pylier.node(_tags=["shipping", "label", "async"])
async def purchase_shipping_label(order: dict[str, Any], reservation: dict[str, Any]) -> bytes:
    """Produce a binary carrier label asynchronously."""
    await asyncio.sleep(0)
    return f"LABEL:{order['id']}:{reservation['reservation']}".encode()


@pylier.node(_tags=["fulfillment", "assembly"])
def assemble_fulfillment(
    approval: tuple[bool, float, str],
    risk: RiskAssessment,
    reservation: dict[str, Any],
    shipping_cost: float,
    label: bytes,
) -> FulfillmentPackage:
    """Combine direct handoffs into the custom fulfillment package."""
    approved, _tax, status = approval
    if not approved or status != "approved" or risk.score > 0.5:
        raise RuntimeError("synthetic order was not approved")
    return FulfillmentPackage(
        order_id=reservation["reservation"].replace("res", "ord"),
        warehouse=str(reservation["warehouse"]),
        shipping_cost=shipping_cost,
        label=label,
    )


@pylier.node(_tags=["fulfillment", "output"])
def publish_fulfillment(package: FulfillmentPackage) -> str:
    """Publish a synthetic fulfillment receipt."""
    return f"published:{package.order_id}:{package.warehouse}"


@pylier.node(_tags=["fulfillment", "assessment"])
def assess_order(order: dict[str, Any]) -> tuple[tuple[bool, float, str], RiskAssessment]:
    """Run the independent approval and risk checks for one order."""
    return validate_order(order), audit_risk(order)


@pylier.node(_tags=["fulfillment", "inventory", "async"])
async def prepare_inventory(order: dict[str, Any], stage_delay: float) -> InventoryPlan:
    """Build the deeper inventory branch with bounded repeated refinement."""
    items = extract_items(order)
    await _pause(stage_delay)
    initial_rank = rank_items(items)
    await _pause(stage_delay)
    refined_items = expand_rank(initial_rank)
    await _pause(stage_delay)
    refined_item_count = rank_items(refined_items)
    await _pause(stage_delay)
    warehouses = choose_warehouses(items)
    await _pause(stage_delay)
    reservation = await reserve_inventory(items, warehouses)
    return InventoryPlan(reservation=reservation, item_count=refined_item_count)


@pylier.node(_tags=["fulfillment", "shipping", "async"])
async def prepare_shipping(order: dict[str, Any], stage_delay: float) -> float:
    """Build the shorter shipping branch independently of inventory work."""
    zone = shipping_zone(order)
    await _pause(stage_delay)
    item_count = count_shipping_items(order)
    await _pause(stage_delay)
    return await quote_shipping(zone, item_count)


@pylier.node(_tags=["fulfillment", "finalize", "async"])
async def finalize_fulfillment(
    order: dict[str, Any],
    approval: tuple[bool, float, str],
    risk: RiskAssessment,
    inventory: InventoryPlan,
    shipping_cost: float,
    stage_delay: float,
) -> str:
    """Recombine branch results, create a label, and publish the receipt."""
    label = await purchase_shipping_label(order, inventory.reservation)
    await _pause(stage_delay)
    package = assemble_fulfillment(approval, risk, inventory.reservation, shipping_cost, label)
    await _pause(stage_delay)
    return publish_fulfillment(package)


@pylier.node(_tags=["fulfillment", "workflow", "async"])
async def fulfill_order(stage_delay: float = 0.0) -> str:
    """Coordinate one complete fulfillment run with concurrent work streams."""
    order = create_order()
    await _pause(stage_delay)
    approval, risk = assess_order(order)
    await _pause(stage_delay)
    inventory, shipping_cost = await asyncio.gather(
        prepare_inventory(order, stage_delay), prepare_shipping(order, stage_delay)
    )
    await _pause(stage_delay)
    return await finalize_fulfillment(order, approval, risk, inventory, shipping_cost, stage_delay)


async def run_fulfillment(
    name: str,
    *,
    level: Level = Level.DEBUG,
    sidecar: Path | None = None,
    stage_delay: float = 0.0,
) -> Trace:
    """Record one fulfillment run at the requested capture level.

    Args:
        name: Trace name shown in the generated viewer.
        level: Active pylier capture level for the run.
        sidecar: Optional file receiving already-resolved JSONL events.
        stage_delay: Seconds to wait after each visible workflow stage.

    Returns:
        The completed in-memory trace.
    """
    with _capture_synthetic_values(), pylier.set_level(level), pylier.trace(name, sidecar=sidecar or False) as trace:
        await _execute_fulfillment(stage_delay)
    return trace


def write_html_artifacts(output_dir: Path = Path(".")) -> ShowcaseArtifacts:
    """Generate the full, level-comparison, and sidecar showcase artifacts.

    Args:
        output_dir: Directory receiving generated HTML and JSONL files.

    Returns:
        Paths to the generated showcase files.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    full_html = output_dir / "pylier-fulfillment-showcase.html"
    info_html = output_dir / "pylier-fulfillment-info.html"
    sidecar = output_dir / "pylier-fulfillment.jsonl"
    sidecar.unlink(missing_ok=True)

    full_trace = asyncio.run(run_fulfillment("fulfillment-showcase", sidecar=sidecar))
    info_trace = asyncio.run(run_fulfillment("fulfillment-info", level=Level.INFO))
    pylier.render(full_html, trace=full_trace, embed_payloads=True)
    pylier.render(info_html, trace=info_trace, embed_payloads=True)
    return ShowcaseArtifacts(full_html=full_html, info_html=info_html, sidecar=sidecar)


async def serve_fulfillment(output_dir: Path = Path("."), stage_delay: float = 0.75) -> None:
    """Run the fulfillment trace in the existing live SSE viewer.

    Args:
        output_dir: Directory receiving the resolved live JSONL sidecar.
        stage_delay: Seconds between stages so graph evolution is observable.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    sidecar = output_dir / "pylier-fulfillment-live.jsonl"
    sidecar.unlink(missing_ok=True)
    with (
        _capture_synthetic_values(),
        pylier.set_level("debug"),
        pylier.trace("fulfillment-live", sidecar=sidecar) as trace,
    ):
        server = pylier.serve(trace=trace)
        print("viewer open — streaming fulfillment stages...")
        try:
            await _execute_fulfillment(stage_delay)
            print(f"live run complete; resolved sidecar: {sidecar}")
            input("press Enter to stop the viewer...")
        except EOFError:
            pass
        finally:
            server.shutdown()


async def _execute_fulfillment(stage_delay: float) -> str:
    """Execute one nested fulfillment workflow in whichever trace is active."""
    return await fulfill_order(stage_delay)


@contextlib.contextmanager
def _capture_synthetic_values():
    """Enable edge-value capture only while the demo records harmless data."""
    prior_value = os.environ.get("PYLIER_CAPTURE_VALUES")
    os.environ["PYLIER_CAPTURE_VALUES"] = "true"
    reload_settings()
    try:
        yield
    finally:
        if prior_value is None:
            os.environ.pop("PYLIER_CAPTURE_VALUES", None)
        else:
            os.environ["PYLIER_CAPTURE_VALUES"] = prior_value
        reload_settings()


async def _pause(stage_delay: float) -> None:
    """Wait only in live mode; static and test runs stay immediate."""
    if stage_delay > 0:
        await asyncio.sleep(stage_delay)


def main() -> None:
    """Parse CLI arguments and run the requested showcase mode."""
    parser = argparse.ArgumentParser(description="pylier e-commerce fulfillment showcase")
    parser.add_argument("mode", choices=("html", "serve"), nargs="?", default="html")
    parser.add_argument("--output-dir", type=Path, default=Path("."))
    parser.add_argument("--stage-delay", type=float, default=0.75)
    args = parser.parse_args()

    if args.mode == "html":
        artifacts = write_html_artifacts(args.output_dir)
        print(f"full showcase: {artifacts.full_html}")
        print(f"level comparison: {artifacts.info_html}")
        print(f"resolved sidecar: {artifacts.sidecar}")
    else:
        asyncio.run(serve_fulfillment(args.output_dir, args.stage_delay))


if __name__ == "__main__":
    main()
