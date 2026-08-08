<div align="center">

# pylier

### Decorate your functions. Watch the pipeline that actually ran appear.

<p>
  pylier infers data handoffs at runtime and renders an interactive graph of the
  pipeline that <em>actually executed</em> — no graph DSL, no manual edge
  wiring, no tracing framework to adopt. Watch it live while the pipeline runs,
  or export a standalone HTML file to share.
</p>

<p>
  <a href="https://github.com/theMladyPan/pylier"><img src="https://img.shields.io/badge/Python-3.14%2B-3776AB?logo=python&logoColor=white" alt="Python 3.14+"></a>
  <a href="https://github.com/theMladyPan/pylier"><img src="https://img.shields.io/github/stars/theMladyPan/pylier?style=social" alt="pylier GitHub stars"></a>
  <img src="https://img.shields.io/badge/graph%20DSL-none-2ea44f" alt="No graph DSL">
  <img src="https://img.shields.io/badge/tracing%20framework-none-2ea44f" alt="No tracing framework">
</p>

</div>

<table>
<tr>
<td width="50%" align="center">
  <a href="https://theMladyPan.github.io/pylier/fulfillment.html">
    <img src="https://raw.githubusercontent.com/theMladyPan/pylier/master/assets/app-flow-view.png" alt="pylier Application Flow view showing nested fulfillment branches" width="100%">
  </a>
</td>
<td width="50%" align="center">
  <a href="https://theMladyPan.github.io/pylier/fulfillment.html">
    <img src="https://raw.githubusercontent.com/theMladyPan/pylier/master/assets/data-flow-view.png" alt="pylier Data Flow view showing value provenance across fulfillment branches" width="100%">
  </a>
</td>
</tr>
<tr>
<td valign="top">
  <p align="center"><b>Application Flow</b></p>
  <ul>
    <li>Direct argument, return, and exception handoffs between callers and callees.</li>
    <li>Use it to understand how the application executes.</li>
  </ul>
</td>
<td valign="top">
  <p align="center"><b>Data Flow</b></p>
  <ul>
    <li>Producer-to-consumer value provenance.</li>
    <li>Links each returned value directly to every decorated consumer.</li>
    <li>Use it to understand where data goes and what moves through the pipeline.</li>
  </ul>
</td>
</tr>
</table>

## Why pylier?

Pipeline diagrams rot the moment code changes. DAG frameworks want you to
rewrite your code as a graph. pylier does neither: it reads the handoffs your
code already performs and renders them.

<table>
<tr>
<td width="33%" align="center">
<b>Decorate, don't rebuild</b>
<p>Mark ordinary sync or async functions with <code>@pylier.node</code>. Your application code remains the pipeline.</p>
</td>
<td width="33%" align="center">
<b>Follow real data</b>
<p>Edges are inferred from the values passed between stages, so the graph reflects execution instead of a hand-maintained diagram.</p>
</td>
<td width="33%" align="center">
<b>Watch it live</b>
<p>Open a live in-process viewer that streams the graph as work happens; export a self-contained HTML file when you need to share.</p>
</td>
</tr>
</table>

### How it compares

| | pylier | Prefect / Dagster | graphviz / diagrams | OpenTelemetry |
|---|---|---|---|---|
| What it shows | Data flow that actually ran | Scheduler DAG of declared tasks | Hand-drawn diagram | Span tree of call timing |
| Edges | Inferred at runtime | Declared in code | Drawn by hand | Inferred, but per-call timing |
| Adopt it | `@pylier.node` decorator | Adopt a scheduler + rewrite as tasks | Re-render on every change | Instrument with a backend + storage |
| Output | Live in-process viewer (+ portable HTML for sharing) | A server / orchestrator | Static image | A telemetry backend |
| Data provenance | Yes, per value fingerprint | No (task I/O only) | No | No |
| Runtime cost | Optional, level-gated | Always-on scheduler | None | Always-on export |

pylier is not a replacement for a scheduler or an observability backend — it's
the thing you reach for when you want to *see* a pipeline, not run or monitor
one.

## Try demos

- [Document ingestion demo](https://theMladyPan.github.io/pylier/ingest.html)
- [Fulfillment workflow demo](https://theMladyPan.github.io/pylier/fulfillment.html)

## Quick start: document ingestion


```python
#! uv add pylier
import pylier


@pylier.node
def embed(chunks: list[str]) -> list[dict]: ...


with pylier.trace("ingest"):
    embed(load("report.pdf"))

pylier.serve()  # live viewer at http://localhost:8765
```


The [ingestion example](https://theMladyPan.github.io/pylier/ingest.html) is the fastest way to see pylier's
value: a document branches into text and image paths, then concurrently embeds
both branches before they converge at an indexing stage.

```bash
git clone https://github.com/theMladyPan/pylier.git
cd pylier
uv sync
uv run python -m examples.ingest serve
# viewer: http://localhost:8765
```

Open the viewer in a browser and watch the graph grow as work happens — a
document branches into text and image paths, then concurrently embeds both
branches before they converge at an indexing stage. To get a self-contained
file for sharing instead:

```bash
uv run python -m examples.ingest html
```

`pylier-ingest.html` is a single file — no server required.

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

## Notes

For a plain-Python transformation or join that loses value provenance, preserve
its sources explicitly:

```python
vectors = pylier.derive(text_vectors + image_vectors, from_=[text_vectors, image_vectors])
```

`derive()` returns the original value unchanged; its only job is to keep the
branch lineage visible in the next decorated stage. See
[`docs/records/derive-lineage.md`](docs/records/derive-lineage.md) for its exact behavior.

## Built for useful traces

- **Signal over noise** — use `core`, `info`, `debug`, and `trace` capture
  levels to control both captured nodes and metadata detail.
- **Useful inspection** — filter by node tags; click graph nodes and edges for
  payload type, size, preview, and optional captured values. Full values require
  `PYLIER_CAPTURE_VALUES=1` and remain bounded FIFO by count and bytes. To make
  an intentionally shareable debug bundle, use
  `pylier.render("debug.html", embed_payloads=True)`; the bundled data is
  readable by anyone with the HTML file, so the default static render remains
  metadata-only. The published examples use this opt-in only because their data
  is synthetic.
- **Stream or share** — `pylier.serve()` streams updates to a live in-process
  viewer with SSE; `pylier.render()` exports a portable HTML file for sharing.
- **Keep an audit trail** — `pylier.trace(..., sidecar="trace.jsonl")` writes
  already-resolved events to JSONL for offline consumers.

## No tracing framework to adopt

pylier has no OpenTelemetry or other tracer dependency. It records
its own local decorator traces and does not mutate ambient tracing context, so
another tracer can instrument the same process independently. The only runtime
dependency is `pydantic-settings` for configuration. pylier ships a `py.typed`
marker and full PEP 695 type annotations, so IDE autocompletion and type checkers
work out of the box.

## Development

```bash
uv run pytest
uv run ruff format src tests examples
uv run ruff check src tests examples
```

## Versioning

pylier follows [Semantic Versioning](https://semver.org/). Given a `MAJOR.MINOR.PATCH`
version: breaking API changes bump `MAJOR`, backward-compatible additions bump
`MINOR`, and fixes/patches bump `PATCH`. No silent breaking changes in minor
releases — if you pin a minor version, upgrades within it stay safe.

## Roadmap

pylier is a decorator-first local visualizer today. The direction, in priority
order:
- **Framework helpers** — optional extras that autotrace popular stacks:
  `uv add "pylier[fastapi]"` then `pylier.instrument_fastapi()` to trace HTTP
  endpoints, `pylier[pydantic-ai]` to autotrace / auto-spawn nodes for LLM agents,
  and so on. Helpers stay opt-in extras — the core stays dependency-light.
- **Live remote tracing** — a dedicated server for online tracing of cloud / VM
  applications, so pipelines running outside your machine stream their graphs to
  a shared viewer (today the live viewer is in-process only).
- **UI redesign** — a more polished, better-structured graph viewer and inspector.

None of these change the core contract: decorate a function, get the graph of
what actually ran.

## Contributing

Issues and pull requests are welcome at
[theMladyPan/pylier](https://github.com/theMladyPan/pylier).
