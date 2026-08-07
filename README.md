# pylier

Decorator-driven data process & pipeline visualization. Decorate functions as
pipeline nodes; pylier infers edges from the data that flows between them and
renders a force-directed graph (D3 v7, single-file HTML) or a live in-process
viewer.

## Install

```bash
uv sync
```

## Usage

```python
import pylier

@pylier.node
def load(path: str) -> dict: ...

@pylier.node(payload_kind="trigger")
def extract(doc: dict) -> list[str]: ...

@pylier.node
def embed(chunks: list[str]) -> list[dict]: ...

with pylier.trace("ingest"):
    vecs = embed(extract(load("doc.pdf")))

pylier.render("out.html")   # self-contained HTML, open from file://
pylier.serve()              # live viewer at http://localhost:8765
```

Edges are inferred by value fingerprinting: a node's return value is hashed and
remembered; when a later node receives a matching argument, an edge is drawn.
No manual wiring.

### Capture levels

```python
@pylier.node(level="debug")
def detail(): ...

with pylier.set_level("debug"):   # core < info < debug < trace
    ...
```

Levels (`core` < `info` < `debug` < `trace`) control both which nodes are
captured and how much payload metadata (type, size, preview, tags) is recorded.

## Transport (logfire-style)

- **In-memory** (default): backs `pylier.trace()` and `pylier.render()`.
- **Sidecar**: `pylier.trace(..., sidecar="trace.jsonl")` writes resolved events
  to JSONL for offline replay / cross-process consumers.
- **Live viewer**: `pylier.serve()` tails the active trace in-process.
- **OTel receiver** (planned): consume logfire spans/logs and render them.

## Example

```bash
uv run python examples/ingest.py html    # write pylier-ingest.html
uv run python examples/ingest.py serve   # live viewer
```

## Tests

```bash
uv run pytest
```
