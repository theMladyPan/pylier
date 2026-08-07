# Nested decorated nodes and trace tabs

## Goal

Render decorated calls made inside another decorated call as expandable parent-node bubbles. Keep traces independent and show every in-process trace in automatic live-viewer tabs.

## Decisions

- `trace()` remains an independent run; no trace nesting or merged lineage.
- A parent node expands into only its directly nested decorated calls.
- Boundary paths use actual fingerprints and `derive()` lineage only.
- Repeated nested child calls render as separate call cards.
- The inner force layout determines bubble bounds; that live size participates in outer force collision.
- `serve()` automatically receives subsequently created traces as tabs.
- Ingest creates one trace per document and uses `process_document()` as its nested parent.

## Affected files

- `model.py`, `recorder.py`: call hierarchy, call-instance metadata, boundary lineage, and trace registry.
- `server.py`, `render/template.html`: tab SSE and expandable force-laid-out parent bubbles.
- `examples/ingest.py`: one trace per document and the nested parent demo.
- `tests/`: hierarchy, provenance, tab registration, and renderer coverage.

## Simplification

Reuse the existing trace event stream, D3 renderer, ContextVars, and in-memory server. Do not add a separate tracing API, a graph DSL, or a dependency.
