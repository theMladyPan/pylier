# Quickstart

## Install

```bash
uv add pylier
# or
pip install pylier
```

Requires **Python 3.12+**. pylier ships a `py.typed` marker and full PEP 695 type
annotations, so IDE autocompletion and type checkers work out of the box.

## The whole API in one flow

```python
import pylier


@pylier.node
def load_document(path: str) -> dict: ...


@pylier.node(_tags=["document", "text"])
def extract_text(document: dict) -> list[str]: ...


@pylier.node
def embed(chunks: list[str]) -> list[dict]: ...


with pylier.trace("document-ingest"):
    document = load_document("report.pdf")
    vectors = embed(extract_text(document))

pylier.render("document-ingest.html")  # interactive, standalone HTML
```

That is the entire surface: `@pylier.node` marks a function as a node, edges are
inferred at runtime from the values passed between stages, and `pylier.trace(...)`
scopes one logical run. There is no edge-wiring API — you never declare edges.

## Watch it live

```python
pylier.serve()  # live viewer at http://localhost:8765
```

`pylier.serve()` streams the graph to an in-process viewer over SSE as work
happens — nodes appear and connect the moment data moves. See
[Live Viewer](scenarios/live-viewer.md) for the push model.

## Export to share

```python
pylier.render("out.html")  # single self-contained file, no server required
```

`out.html` is a single file with the graph JSON embedded — open it from
`file://`, email it, host it statically. By default it is **metadata-only**; see
[Sharing Traces](scenarios/sharing.md) for the explicit `embed_payloads=True`
debug bundle.

!!! tip "Published demos"
    Live, interactive demos are published on GitHub Pages:

    - [Document ingestion](/demos/ingest.html) — branched text and image extraction with concurrent embedding
    - [Fulfillment workflow](/demos/fulfillment.html) — nested assessment, inventory, shipping, and finalization branches

!!! note "Run the examples locally"
    ```bash
    git clone https://github.com/theMladyPan/pylier.git
    cd pylier
    uv sync
    uv run python -m examples.ingest serve
    # viewer: http://localhost:8765
    ```

    Or export a self-contained file: `uv run python -m examples.ingest html`.

## Where next

- [Scenarios](scenarios/index.md) — ingestion pipelines, autotrace, capture levels, FastAPI, and more.
- [Preserving multi-source lineage with `derive`](scenarios/derive-lineage.md) — when and why to keep branch provenance.
