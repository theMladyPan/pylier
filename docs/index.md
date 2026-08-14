# pylier

[![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white)](https://github.com/theMladyPan/pylier)
[![GitHub stars](https://img.shields.io/github/stars/theMladyPan/pylier?style=social)](https://github.com/theMladyPan/pylier)
[![graph DSL: none](https://img.shields.io/badge/graph%20DSL-none-2ea44f)](https://github.com/theMladyPan/pylier)
[![tracing framework: none](https://img.shields.io/badge/tracing%20framework-none-2ea44f)](https://github.com/theMladyPan/pylier)

pylier is a Python library for **visualizing the flow of data between processes /
pipeline stages**. Decorate ordinary functions and pylier infers the runtime data
handoffs your code already performs, then renders an interactive force-directed
graph of the pipeline that *actually executed* — no graph DSL, no manual edge
wiring, no tracing framework to adopt.

The motivating scenario: upload a document, process it through a **branched
pipeline** of heterogeneous data types, and get a **force-directed node chart
built dynamically** — like Plotly builds a chart from data, but for data *flow*
not data *values*.

### Two UX north stars

1. **Plotly-style**: decorate functions/classes, get an interactive HTML render
   with UI components. You never build a graph by hand — you write code and the
   graph emerges.
2. **Logfire-style**: frictionless, decorator- and context-manager-driven,
   intuitive surface API. "Just decorate and it works."

### Minimal example

```python
import pylier


@pylier.node
def embed(chunks: list[str]) -> list[dict]: ...


with pylier.trace("ingest"):
    embed(load("report.pdf"))

pylier.serve()  # live viewer at http://localhost:8765
```

## Application Flow vs Data Flow

pylier renders two perspectives over the same execution:

| | Application Flow | Data Flow |
|---|---|---|
| **What it shows** | Direct invocation handoffs | Producer-to-consumer value provenance |
| **Edges** | Argument, return, empty, and exception handoffs between callers and callees | Links each returned value directly to every decorated consumer of a matching value |
| **Root** | An internal source/sink endpoint (drawn as a clickable circle) | Hidden — lineage uses direct producer-to-consumer links, never round trips through the trace root |
| **Use it to** | Understand how the application executes | Understand where data goes and what moves through the pipeline |

Continue with [Quickstart →](quickstart.md).
