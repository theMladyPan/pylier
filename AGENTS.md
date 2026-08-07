# AGENTS.md

Agent-focused context for `pylier`. This complements `README.md` (human-facing
quick start). It records the **vision, the design decisions and why they were
made**, plus the build/test conventions agents must follow. Read this before
changing architecture or arguing with the API shape — the *why* is load-bearing.

## Vision (the original idea, don't lose it)

`pylier` is a library for **visualizing the flow of data between processes /
pipeline stages**. Two UX north stars, in priority order:

1. **Plotly-style**: decorate functions/classes, get an interactive HTML render
   with UI components. The user should not build a graph by hand — they write
   code and the graph emerges.
2. **Logfire-style**: frictionless, decorator- and context-manager-driven,
   intuitive surface API. "Just decorate and it works."

The motivating scenario: upload a document, process it through a **branched
pipeline** of heterogeneous data types, and get a **force-directed node chart
built dynamically** — like Plotly builds a chart from data, but for data *flow*
not data *values*.

The proven visual stack (from the PoC this library codifies): single-file HTML,
**D3.js v7 from CDN**, vanilla JS + inline SVG, plain CSS, force-directed graph
with curved parallel edges, stroke styles encoding payload type, click-to-
inspect details, particle flow animation. No build step, no framework. This
stack is the reference renderer; do not introduce React/Vue/Tailwind/build
tooling without an explicit decision.

## Core design decisions (why over what)

### Decorator-first, edges inferred — never declared
- `@pylier.node` marks a function as a node. Edges are **inferred from the data
  that flows between nodes**, not declared.
- **Why:** the whole value is "decorate, structure emerges." A manual edge API
  (`pipe.connect(A, B)`) was explicitly rejected: it duplicates the actual call
  graph, is verbose, and breaks the Plotly/logfire "just decorate" feel.
- Implication: there is no edge-wiring API. Don't add one. The only way data
  links two nodes is by being returned from one and received by another.

### A node is a function; an edge is a data payload
- `function == node`, `edge == the data payload` passed between them.
- **Why:** 1:1 with the code is the natural mental model and captures the real
  pipeline. Pipeline-step-as-node / context-manager-scopes-as-node were
  rejected as either too boilerplate-y or too complex for nested/branching cases.

### Edge inference = value fingerprinting (content hash)
- On node exit, the return value is fingerprinted (`<type>:<hash>`) and
  registered `fingerprint -> source_node`. On the next node's entry, each
  argument is fingerprinted; a match draws an edge.
- **Why chosen over a call-stack contextvar:** edges should represent *data
  movement*, not call nesting. A contextvar keyed on the "current source" misses
  data passed via storage/queues and mislinks unrelated nested calls.
- **Accepted trade-off (know it, don't "fix" it silently):** fingerprinting is
  content-based, so **transformed/aggregated copies don't link** (e.g.
  `index(vecs_a + vecs_b)` won't edge from `embed`). This is intentional for
  v0.1 to keep the API at zero. A fingerprint+contextvar *hybrid* is the planned
  fast-follow — only add it deliberately, not as a drive-by.

### Capture levels: `core < info < debug < trace`
- Modeled on logfire's `min_level`. A node is recorded only when its declared
  level rank <= the active global level. Metadata richness also follows the
  level: `core`=identity+bare edges, `info`=+type+size, `debug`=+preview+tags,
  `trace`=+detailed.
- Set per-node (`@pylier.node(level="debug")`) and globally
  (`with pylier.set_level("debug"):` / `pylier.level(...)`).
- **Why:** real pipelines are noisy; levels let users dial structure vs detail.
  `core` nodes stay visible at the lowest verbosity so the skeleton is always
  there.
- Invariant: **node level gates capture; global level gates metadata richness.**
  Don't conflate them.

### Transport: logfire-style, sidecar-first, OTel-ready
- In-memory trace is the default (backing tests and `render()`).
- `pylier.trace(..., sidecar=...)` writes **already-edge-resolved** events to a
  JSONL sidecar for offline replay / cross-process consumers.
- An **OTel receiver** that consumes logfire spans/logs is the planned
  transport for live, cross-process tracing. It is **not built yet** — stub/plan
  only. Don't pretend it exists.
- **Why:** "mimic logfire" means offline file tracing first (simple, works across
  processes/subprocesses, no server), with the live OTel path as the real-time
  option. Resolved edges are emitted so sinks never fingerprint values.

### One render core, static + live
- `render/template.html` is the single source of truth for the graph look. Both
  static (`pylier.render()`) and live (`pylier.serve()`) use it. Static embeds
  the JSON; live polls `/graph` every 1.5s and re-renders in place.
- **Why:** one look everywhere; no drift between test artifacts and live
  preview. The template also falls back to embedded JSON for `file://` opens.

### Flat top-level API
- `@pylier.node`, `pylier.trace()`, `pylier.render()`, `pylier.serve()`,
  `pylier.set_level()`. No `Tracer()` instances.
- **Why:** mirrors logfire's flat, decoration-first feel. Multiple independent
  tracers (class-based) were rejected for v0.1 as extra boilerplate; revisit
  only if real multi-tracer needs appear before publishing.

### Sync + async now, personal lib, publishable later
- `@node` auto-detects coroutine functions and wraps them correctly.
- **Why:** async pipelines (e.g. uploaded-doc processing) are common; deferring
  async would force a rewrite. Packaging stays light (src layout, hatchling,
  pydantic-settings) so publishing later is a flip, not a migration.

## Rules for this file
- if anything changes you are obligated to edit the paragraph/section so it match the implementation (prevent stale information at all costs)
- reference instead of duplicate: if there is a section in code which describes it perfectly, reference the code instead of duplicating information here

## Architecture map

```
src/pylier/
  model.py        # Node, Edge, Event, Trace, Level — single source of truth
  fingerprint.py  # content fingerprint (type+hash) — only place values are hashed
  recorder.py     # active-trace contextvar, level gating, edge inference, @node core
  config.py       # pydantic-settings (PYLIER_*, .env): level, sidecar path, port
  tracing/
    sidecar.py    # JSONL event sink (offline replay); edges already resolved
  render/
    template.html # THE renderer (D3 v7). Placeholders consumed by html.py
    html.py       # injects graph JSON into template; build_html / render_to_file
  server.py       # stdlib threaded viewer: GET / (html) + GET /graph (json)
tests/test_core.py
examples/ingest.py
```

### Load-bearing invariants (don't break these)
- **Fingerprinting happens only in `recorder.py`** (via `fingerprint.py`). Sinks,
  the viewer, and OTel consume *resolved* edges — never re-fingerprint.
- **Level filtering runs before instrumentation.** Uncaptured nodes call the
  raw function with zero overhead and register nothing — otherwise they'd
  create phantom edges into captured nodes.
- **`render/template.html` placeholders** replaced by `render/html.py`:
  `__PYLIER_GRAPH__` (JS object), `__PYLIER_GRAPH_JSON__` (embedded fallback),
  `{{NAME}}` (header). When editing the template, keep these exact tokens.
- **`_last_trace` reference:** `pylier.render()` / `serve()` with no explicit
  trace render the most recently entered `with pylier.trace(...)` block, not
  the empty default. This is why post-block `render()` "just works."

## Dev environment

- This is a **uv** project. Never use raw `pip`/`python` in a uv project.
  - Install/sync deps: `uv sync`
  - Run anything: `uv run <cmd>` (e.g. `uv run pytest`), never bare `python`
  - Add a dep: `uv add <pkg>` (runtime) or under `[dependency-groups].dev`
- Python target is **3.14** (`requires-python = ">=3.14"`). PEP 695 type
  parameters are in use (e.g. `def record_call[T](...)`); don't revert to
  `TypeVar`/`Union`/`Optional`.
- Layout is **src/** — imports are `from pylier...`, not `from src.pylier...`.

## Testing instructions

- Run the suite: `uv run pytest` (config in `pyproject.toml`: `testpaths =
  ["tests"]`, `asyncio_mode = "auto"`).
- One test: `uv run pytest tests/test_core.py::test_name` or `-k <pattern>`.
- Async tests just use `async def` + `asyncio.run(...)` — no manual
  `@pytest.mark.asyncio` needed (auto mode).
- **Tests must be deterministic and fast** (per the project python-best-practices
  convention): no network, no external services, no real LLM/API/DB calls. Core
  graph tests use the in-memory recorder and `tmp_path` for any file output.
- The canonical test pattern is `with pylier.trace(...) as tr: ...` then assert
  on `tr.nodes` / `tr.edges`, and `pylier.render(tmp_path/"x.html", trace=tr)`
  for render checks. Follow it for new tests.
- Add or update tests for any recorder/edge/level behavior you change.

## Lint / format (before every commit)

- Format: `uv run ruff format src tests examples` — **don't hand-format**.
- Check: `uv run ruff check src tests examples` — must be clean. Config in
  `pyproject.toml` (`select = ["E","F","I","UP","B","SIM"]`, line-length 100).
- Auto-fix where safe: `uv run ruff check --fix src tests examples`.
- Commit must pass both `ruff check` and `pytest`.

## Code conventions (project standard)

- Google-style docstrings with `Args`/`Returns`/`Raises` on public APIs.
- Modern type hints: `X | None`, not `Optional[X]`.
- Descriptive names; comments explain **why**, not **what**. Add cross-impact
  comments when a change affects another runtime path
  (`# this method is used to determine X in method Y and Z during runtime`).
- Keep `model.py` free of tracing/render imports so it stays the neutral core.

## Known limitations / accepted trade-offs

- **Fingerprinting misses transformed/aggregated copies** (see edge-inference
  decision above). Document, don't silently patch.
- The live viewer currently tails the **in-memory** trace; the sidecar sink
  writes events but the viewer doesn't yet reconstruct from a sidecar across
  processes. (Fast-follow: viewer tails sidecar.)
- OTel/logfire receiver: planned, **not implemented**.
- Decorated-but-never-called nodes are not rendered (only called nodes appear).

## Fast-follows (out of v0.1 scope — do only on request)

- `tracing/otel.py`: OTel receiver consuming logfire spans/logs → graph.
- Viewer server tailing the sidecar (cross-process live preview).
- Fingerprint + contextvar hybrid edge inference for transformed copies.
- Rendered nodes for declared-but-uncalled `@node`s (registry exists in
  `recorder.make_meta`; wiring to render is the gap).
