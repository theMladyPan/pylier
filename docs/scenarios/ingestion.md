# Document ingestion pipeline

The canonical pylier scenario: an uploaded document branches into text and image
paths, each path is processed concurrently, and the branches converge at an
indexing stage. This is the example shipped in
[`examples/ingest.py`](https://github.com/theMladyPan/pylier/blob/master/examples/ingest.py)
and published as the [ingestion demo](https://themladypan.github.io/pylier/demos/ingest.html).

## What the graph reveals

- **Branching** — a single loaded document splits into text extraction and image
  extraction, each a decorated node.
- **Concurrency** — the two branches embed independently; the Application Flow
  view shows the fork, the Data Flow view shows each branch's values flowing
  forward.
- **Convergence** — both branches' embeddings feed a final indexing stage.

## Running it

```bash
git clone https://github.com/theMladyPan/pylier.git
cd pylier
uv sync
uv run python -m examples.ingest serve   # live viewer: http://localhost:8765
uv run python -m examples.ingest html    # self-contained pylier-ingest.html
```

!!! tip "Open the published demo"
    The [ingestion demo](https://themladypan.github.io/pylier/demos/ingest.html) is a static replay of exactly this
    run — no server required.

## Structure

The real example uses `pylier.autotrace(...)` rather than hand-placing
`@pylier.node` on every function, so the graph emerges from the application's
own call structure. See [Autotrace without decorators](autotrace.md) for how that
scope filtering works.

The shipped example converges by passing the two branches to `index` as
separate arguments, so each branch's value provenance is preserved by the
argument handoff itself. [`pylier.derive`](derive-lineage.md) is only needed
when a plain Python expression actually **merges** contributing values into one
new value and would otherwise lose that lineage.
