# Call-stack lens handoff

## Current decision

Do **not** use bubbles, nested trace scopes, parent-child re-layout, or expanded-node containment. All nodes stay in the same force-directed data-flow graph. Parent/child OTel span hierarchy is a temporary inspection lens: parent cards show a direct-call count, child cards show a parent breadcrumb, and selecting either shows faint dashed call-thread overlays while unrelated nodes dim.

## Approved mockup candidate

`mockups/10-call-stack-lens.html` is an interactive standalone prototype. Click `process_document`, a child card, and Reset lens to compare the intended focus states. It has not yet been applied to `render/template.html`.

## Existing implementation state

- `9998a74 feat(otel): capture standard nested spans`: pylier creates/captures standard OTel spans without Logfire.
- `f332714 feat(viewer): add live trace tabs`: root traces register as separate live viewer tabs.
- `940fe8a fix(otel): stream spans at start`: OTel spans enter pylier while running and update on end.
- `1d02b0f feat(render): organize expanded child spans`: current bubble renderer implementation; this is now rejected and should be removed/replaced by the call-stack lens, not extended.
- `examples/ingest.py` demonstrates `process_document()` with nested decorated calls and distinct OTel child spans.

## Next implementation

1. Remove/revert the bubble-specific renderer projection from `render/template.html` while retaining OTel span capture and live trace tabs.
2. Keep all raw function/OTel span cards at the force-graph level; repeated child invocations must remain distinct cards.
3. Add parent/child span metadata to cards and selected-node details.
4. Implement the transient dashed hierarchy overlay plus focus/dim behavior from mockup 10.
5. Update renderer tests, render the ingest example, and browser-test parent/child/reset interactions.

## Constraints

- No `logfire` dependency.
- OTel `trace_id` identifies a root viewer tab; `span_id` / `parent_span_id` provide call nesting.
- Payload edges remain pylier fingerprint/`derive()` lineage; call-thread overlays must never be presented as payload edges.
- Keep the existing persistent outer D3 force simulation and SSE push model.
