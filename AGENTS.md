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
with deterministic straight/orthogonal paired lanes, stroke styles encoding
payload type, click-to-inspect details, particle flow animation. No build step,
no framework. This
stack is the reference renderer; do not introduce React/Vue/Tailwind/build
tooling without an explicit decision.

## Core design decisions (why over what)

### Decorator-first, edges inferred — never declared
- `@pylier.node` marks a function as a node. Application handoffs and Data Flow
  provenance are inferred at runtime; users never declare edges.
- **Why:** the whole value is "decorate, structure emerges." A manual edge API
  (`pipe.connect(A, B)`) was explicitly rejected: it duplicates actual runtime
  behavior, is verbose, and breaks the Plotly/logfire "just decorate" feel.
- Implication: there is no edge-wiring API. Don't add one.

### A node is a function; edges describe runtime movement
- `function == node`. Application Flow edges are argument, return, empty, or
  exception handoffs; Data Flow edges are value provenance.
- **Why:** 1:1 with the code is the natural mental model and captures both call
  boundaries and data lineage without pipeline-step boilerplate.

### Two graph perspectives: invocation and lineage
- **Application Flow** is direct invocation handoff: a nested decorated call
  receives data from its active caller, while a top-level call receives it from
  the trace root. It never draws a fingerprint bypass edge.
- **Data Flow** separately records fingerprint-inferred producer-to-consumer
  relations for every decorated consumer of a matching non-empty value,
  including a nested child return consumed by its decorated caller. It hides
  root/external handoffs: returns are direct producer-to-consumer links, never
  round trips through the trace root.
- Each decorated invocation has a runtime ID. Repeated function-pair links
  aggregate visually but retain individual handoffs for inspection in both
  perspectives.
- Transformed/aggregated copies require `pylier.derive(...)` to preserve
  intentional multi-source lineage.

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

### Transport: standalone, sidecar-first
- In-memory trace is the default (backing tests and `render()`).
- `pylier.trace(..., sidecar=...)` writes resolved handoff events to a JSONL
  sidecar for offline replay and future remote viewers.
- pylier has no OpenTelemetry, Logfire, FastAPI, or cloud dependency. It does
  not import or mutate another tracer's context, so those tools can run beside
  pylier independently.

### One render core, static + live
- `render/template.html` is the single source of truth for the graph look. Both
  static (`pylier.render()`) and live (`pylier.serve()`) use it. Static embeds
  the JSON (incl. the `events` timeline) and replays the execution animation
  once on load; live subscribes to SSE (`/events`) and re-renders in place.
  The live viewer retains every produced trace in a left root-trace history.
- **Why:** one look everywhere; no drift between test artifacts and live
  preview. The template also falls back to embedded JSON for `file://` opens.
- The client keeps a **persistent force simulation + D3 join** — never tear
  down and cold-restart on updates (that was the 1.5s tearing bug). New nodes
  spawn near an already-placed neighbor, not the center.
- Live updates are **push, not poll**: SSE sends a full `event: graph` snapshot
  only on topology change (new trace/node/edge, rare) and compact `event: exec`
  batches for enter/exit activity. History batches carry `trace_id`; clients
  animate only the selected trace and update call-count badges locally.

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
  model.py        # Node, Edge, Event, Trace, TraceHistory, Level — neutral core
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
examples/pseudo.py  # canonical nested-handoff example
```

### Load-bearing invariants (don't break these)
- **Fingerprinting happens only in `recorder.py`** (via `fingerprint.py`).
  Sinks and the viewer consume resolved handoff edges — never fingerprint.
- **Relation identity is `(source, target)`.** Entry arguments and exit values
  are handoffs in opposite directions; `Edge.metadata["phase"]` is only a UI
  hint for lane and stroke treatment.
- **Level filtering runs before instrumentation.** Uncaptured nodes call the
  raw function with zero overhead and register nothing — otherwise they'd
  create phantom edges into captured nodes.
- **Two trace versions**: `graph_version` bumps only on topology change (new
  node/edge — SSE pushes full graph rarely); `exec_version` bumps on every
  enter/exit event (SSE pushes exec batches in real time). Call-count
  increments must NOT bump `graph_version` (clients update badges locally
  from exec events).
- **`render/template.html` placeholders** replaced by `render/html.py`:
  `__PYLIER_GRAPH__` (JS object), `__PYLIER_GRAPH_JSON__` (embedded fallback),
  `{{NAME}}` (header). When editing the template, keep these exact tokens.
- **`_last_trace` reference:** `pylier.render()` with no explicit trace renders
  the most recently entered `with pylier.trace(...)` block, not the empty
  default. `pylier.serve()` with no explicit trace renders the retained history
  (newest trace selected); pass `trace=` to limit the live viewer to one run.

## Dev environment

- This is a **uv** project. Never use raw `pip`/`python` in a uv project.
  - Install/sync deps: `uv sync` (`uv sync --group examples` for runnable web examples)
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
- The live viewer pushes retained **in-memory** traces; the sidecar sink writes
  events but the viewer doesn't yet reconstruct from a sidecar across
  processes. (Fast-follow: viewer tails sidecar.)
- A live receiver for external/cross-process telemetry is not implemented.
  The shipped viewer observes retained in-process traces only.
- Decorated-but-never-called nodes are not rendered (only called nodes appear).
- Full invocation payload capture is opt-in (`PYLIER_CAPTURE_VALUES`). Live
  inspector expansion fetches it lazily from the local viewer; it is bounded by
  `PYLIER_PAYLOAD_MAX_INVOCATIONS` (100) and `PYLIER_PAYLOAD_MAX_BYTES` (100
  MiB), evicting oldest payloads first. Binary payloads are always summaries.

## Fast-follows (out of v0.1 scope — do only on request)

- Viewer server tailing the sidecar (cross-process live preview).
- Optional external telemetry receiver, without adding a core dependency.
- Richer visual expansion for the individual handoffs aggregated into one edge.
- Rendered nodes for declared-but-uncalled `@node`s (registry exists in
  `recorder.make_meta`; wiring to render is the gap).
