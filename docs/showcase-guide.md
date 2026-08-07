# Pylier fulfillment showcase

Run the evaluator/developer demonstration from the project root:

```bash
uv run python -m examples.showcase html
```

It writes three artifacts in the working directory:

- `pylier-fulfillment-showcase.html` — full debug-level interactive graph.
- `pylier-fulfillment-info.html` — the same flow at `INFO`; the debug-only `audit_risk` node is absent.
- `pylier-fulfillment.jsonl` — resolved sidecar events, ready for offline inspection.

Open the full HTML file in a browser. Click a node to inspect tags, click an edge to inspect its synthetic captured payload, choose tags in the left pane, and compare it with the info-level artifact.

## What the graph demonstrates

| Capability | Find it in the fulfillment flow |
| --- | --- |
| Zero-wiring inference | Every edge is created by passing a decorated function's return value to another decorated function. |
| Payload colors | The flow carries `bool`, `int`, `float`, `str`, `list`, `dict`, `set`, `bytes`, and custom application objects. |
| Tuple gradient | `validate_order → assemble_fulfillment` carries a `tuple[bool, float, str]`. |
| Node tags | Tags such as `order`, `inventory`, `shipping`, and `async` power the filter pane and node inspector. |
| Async nodes | `reserve_inventory`, `quote_shipping`, and `purchase_shipping_label` are decorated coroutines. |
| Capture levels | `audit_risk` is declared `level="debug"`, so it appears only in the full artifact. |
| Captured values | The demo explicitly enables capture for harmless synthetic values only; pylier remains opt-in by default. |
| Static execution replay | Opening either HTML artifact replays its recorded enter/exit timeline. |
| Sidecar | The JSONL file contains resolved node and edge events; it does not re-fingerprint payloads. |

## Live SSE walkthrough

```bash
uv run python -m examples.showcase serve
```

The command starts `pylier.serve()`, opens the existing in-memory viewer, and autoplays each fulfillment stage with a 0.75-second pause. It writes `pylier-fulfillment-live.jsonl`, then waits for Enter so you can inspect the final graph; use `--stage-delay 0.4` for a quicker tour or `--output-dir demo-output` to keep generated files together.

## Implementation boundary

The live viewer follows one in-memory trace. The JSONL sidecar is demonstrated as a resolved offline artifact; it is not a cross-process live viewer input, and OTel support remains planned rather than implemented.
