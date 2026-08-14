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
- `@pylier.node` marks a function as a node. `pylier.autotrace(...)` is the
  opt-in sibling for public Python callables when users want the same graph
  without decorators. Application handoffs and Data Flow provenance are still
  inferred at runtime; users never declare edges.
- **Why:** the whole value is "decorate, structure emerges." A manual edge API
  (`pipe.connect(A, B)`) was explicitly rejected: it duplicates actual runtime
  behavior, is verbose, and breaks the Plotly/logfire "just decorate" feel.
- Implication: there is no edge-wiring API. Don't add one.

### Autotrace is recorder sugar, not a second tracer
- `pylier.autotrace(...)` uses stdlib `sys.monitoring` to feed ordinary Python
  calls back through the same recorder enter/exit path as `@pylier.node`.
- **Why:** Application Flow, Data Flow, levels, payload capture, sidecars,
  events, and viewer behavior stay identical when the code path is the same.
- Product rules: explicit `@pylier.node` wins over autotrace, positive
  `min_exec_time` is Logfire-style warm-up promotion, and `allow_empty=False`
  buffers only successful calls with no meaningful business inputs, treating
  values equal to declared defaults as omitted for that filter.

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
  Static HTML remains metadata-only unless the explicit
  `pylier.render(..., embed_payloads=True)` debug-bundle opt-in embeds the
  retained invocation payloads. The live viewer retains every produced trace
  in a left root-trace history.
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
- `@pylier.node`, `pylier.autotrace()`, `pylier.trace()`, `pylier.render()`,
  `pylier.serve()`, `pylier.set_level()`, `pylier.instrument_fastapi()`. No
  `Tracer()` instances.
- **Why:** mirrors logfire's flat, decoration-first feel. Multiple independent
  tracers (class-based) were rejected for v0.1 as extra boilerplate; revisit
  only if real multi-tracer needs appear before publishing.
- `instrument_fastapi` is lazy: importing `pylier` never pulls in FastAPI. It
  delegates to the `pylier.integrations.fastapi` module, which lives behind the
  `[fastapi]` extra (`uv add pylier[fastapi]`).

### Sync + async now, personal lib, publishable later
- `@node` auto-detects coroutine functions and wraps them correctly.
  `autotrace()` treats sync functions, coroutines, generators, and async
  generators as one logical invocation across yield/resume boundaries.
- **Why:** async pipelines (e.g. uploaded-doc processing) are common; deferring
  async would force a rewrite. Packaging stays light (src layout, hatchling,
  pydantic-settings) so publishing later is a flip, not a migration.

### Framework integrations are self-contained adapters, behind extras
- `pylier.instrument_fastapi(app)` installs a **pure-ASGI middleware** that opens
  one `pylier.trace("<method> <path>")` per HTTP request and writes its response
  status to `Trace.metadata["status_code"]`. `Trace.metadata` is a neutral,
  primitive-value bag: integrations own their keys; core never models an HTTP
  endpoint. Endpoints stay un-decorated; decorate service functions called from
  a handler and they appear in that request's graph.
- **Why:** keeps the core dependency-free and the adapter replaceable. A manual
  edge API or route-mutation (wrapping `APIRoute.endpoint`) was rejected for v1
  as version-fragile; the middleware alone gives the logfire "just decorate"
  feel and the per-request history in the live viewer (capped at 100 by
  `TraceHistory`). Non-HTTP scopes (lifespan, websocket) pass through untraced.
- Fast-forward only on request: richer capture (request/response payloads as
  edge metadata, auto-wrapping endpoints as nodes) is a deliberate v1 omission.

### Trace lifecycle and boundary rendering
- `Trace.started_at` is captured on trace construction; `Trace.ended_at` is set
  exactly once when a managed `pylier.trace()` context exits. Default traces are
  intentionally open-ended. The graph serializes both timestamps and generic
  metadata; clicking the trace boundary exposes every metadata key/value in the
  inspector.
- The Application Flow root is an internal source/sink endpoint, **not an
  algorithm node**. The renderer keeps it in simulation/routing but draws only
  a clickable circle, with diameter equal to a regular node card's height. Data
  Flow still hides it because lineage has direct producer-to-consumer edges.
- The left pane is trace history (`name`, node count, start clock). There is one
  workspace: a user selection stays active as live history grows, rather than
  being hijacked by the newest trace.

## Rules for this file
- if anything changes you are obligated to edit the paragraph/section so it match the implementation (prevent stale information at all costs)
- reference instead of duplicate: if there is a section in code which describes it perfectly, reference the code instead of duplicating information here

## Architecture map

```
src/pylier/
  model.py        # Node, Edge, Event, Trace, TraceHistory, Level — neutral core
  fingerprint.py  # content fingerprint (type+hash) — only place values are hashed
  recorder.py     # active-trace contextvar, level gating, edge inference, @node core
  autotrace.py    # sys.monitoring hook, scope filtering, warm-up promotion, empty-call buffering
  config.py       # pydantic-settings (PYLIER_*, .env): level, sidecar path
  tracing/
    sidecar.py    # JSONL event sink (offline replay); edges already resolved
  render/
    template.html # THE renderer (D3 v7). Placeholders consumed by html.py
    html.py       # injects graph JSON into template; build_html / render_to_file
  integrations/
    __init__.py    # lazy-imported framework adapters; never imported by `import pylier`
    fastapi.py     # `pylier.instrument_fastapi`: pure-ASGI per-request trace middleware
  server.py       # stdlib threaded viewer: GET / (html) + GET /graph (json)
tests/test_core.py
tests/test_autotrace.py
tests/test_fastapi.py
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
  create phantom edges into captured nodes. Autotraced INFO nodes must obey the
  same gate before any timing/buffering state is created.
- **Two trace versions**: `graph_version` bumps only on topology change (new
  node/edge — SSE pushes full graph rarely); `exec_version` bumps on every
  enter/exit event (SSE pushes exec batches in real time). Call-count
  increments must NOT bump `graph_version` (clients update badges locally
  from exec events).
- **`render/template.html` placeholders** replaced by `render/html.py`:
  `__PYLIER_GRAPH__` (JS object), `__PYLIER_GRAPH_JSON__` (embedded fallback),
  `__PYLIER_LIVE__` (server-mode flag), `__PYLIER_INVOCATION_PAYLOADS__`
  (explicit static debug bundle), `{{NAME}}` (header). When editing the
  template, keep these exact tokens.
- **`_last_trace` reference:** `pylier.render()` with no explicit trace renders
  the most recently entered `with pylier.trace(...)` block, not the empty
  default. `pylier.serve()` with no explicit trace renders the retained history
  (newest trace selected); pass `trace=` to limit the live viewer to one run.
- **Autotrace uses one global monitoring tool slot.** Repeated identical
  activation is a no-op, conflicting activation fails, and explicit decorated
  code objects are skipped so `@pylier.node` remains authoritative.
- **Buffered empty-call filtering records into an unregistered temporary
  trace.** Only the completed merge touches the retained trace, sinks, payload
  FIFO, versions, and history; discarded parents must never leak to HTML, SSE,
  sidecars, or `TraceHistory`.

## Dev environment

- This is a **uv** project. Never use raw `pip`/`python` in a uv project.
  - Install/sync deps: `uv sync` (`uv sync --group examples` for runnable web examples)
  - Run anything: `uv run <cmd>` (e.g. `uv run pytest`), never bare `python`
  - Add a dep: `uv add <pkg>` (runtime) or under `[dependency-groups].dev`
  - **`uv.lock` is committed and CI runs `uv run --locked`** (publish.yml) and
    `uv sync --locked` (pages.yml). Any change to `pyproject.toml` — including a
    `[project].version` bump — MUST be followed by `uv lock` and the lockfile
    committed in the same change. A stale lockfile fails CI with
    `error: The lockfile ... needs to be updated`. Verify before pushing with
    `uv lock --check` (fast, no install) or the exact CI gate
    `uv run --locked pytest`.
- Python target is **3.12+** (`requires-python = ">=3.12"`; `target-version = "py312"`
  for ruff). PEP 695 type parameters are in use (e.g. `def record_call[T](...)`);
  PEP 695 is supported from 3.12 onward, so backward compatibility to 3.12/3.13
  requires no architecture change — do not add 3.14-only syntax (e.g. the bare
  `except A, B:` comma form, reintroduced in 3.14; use parenthesized `except (A, B):`).
  Don't revert to `TypeVar`/`Union`/`Optional`.
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

## Lint / format / type-check (before every commit)

- Format: `uv run ruff format src tests examples` — **don't hand-format**.
- Lint: `uv run ruff check src tests examples` — must be clean. Config in
  `pyproject.toml` (`select = ["E","F","I","UP","B","SIM"]`, line-length 120).
  Auto-fix where safe: `uv run ruff check --fix src tests examples`.
- Type-check: `uv run ty check src` — must be clean. Config in `pyproject.toml`
  under `[tool.ty]`. `ty` (Astral, beta) is the type checker; pin its version in
  `[dependency-groups].dev` to control beta churn. Inherent-`Any` diagnostics
  from the dynamic JSON graph shapes (`dict[str, Any]`) and arbitrary captured
  values are expected; tune rule severities under `[tool.ty.rules]` if a future
  `ty` release promotes one to error.
- Public API must stay fully typed: `@pylier.node` preserves the wrapped
  callable's signature via PEP 695 `ParamSpec` overloads — don't regress it to
  `-> Any`. Don't add bare `dict`/`list`/`Token` annotations; parameterize them.
- Commit must pass `ruff check`, `ty check`, `pytest`, and `uv lock --check`
  (lockfile in sync with `pyproject.toml`).

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
- **Autotrace is Python-defined-only.** It ignores C callables and requires a
  runtime with `sys.monitoring` and `sys._getframe`; calling the API on an
  unsupported runtime fails clearly instead of changing import-time behavior.
- **Autotrace empty-call filtering is value-based, not caller-intent-based.**
  Parameters whose runtime value equals their declared default are treated as
  omitted for `allow_empty=False`; users who need those calls retained should
  set `allow_empty=True`.
- The live viewer pushes retained **in-memory** traces; the sidecar sink writes
  events but the viewer doesn't yet reconstruct from a sidecar across
  processes. (Fast-follow: viewer tails sidecar.)
- A live receiver for external/cross-process telemetry is not implemented.
  The shipped viewer observes retained in-process traces only.
- Decorated-but-never-called nodes are not rendered (only called nodes appear).
- Full invocation payload capture is opt-in (`PYLIER_CAPTURE_VALUES`). Live
  inspector expansion fetches it lazily from the local viewer; an explicit
  `pylier.render(..., embed_payloads=True)` bundle can embed the same retained
  values for intentionally shareable synthetic/debug runs. Both are bounded by
  `PYLIER_PAYLOAD_MAX_INVOCATIONS` (100) and `PYLIER_PAYLOAD_MAX_BYTES` (100
  MiB), evicting oldest payloads first. Binary payloads are always summaries.
- Trace metadata and lifecycle changes made after a graph snapshot are not
  pushed as dedicated live SSE updates yet; the inspector reflects them on the
  next full graph snapshot/render.

## Fast-follows (out of v0.1 scope — do only on request)

- Viewer server tailing the sidecar (cross-process live preview).
- Optional external telemetry receiver, without adding a core dependency.
- Richer visual expansion for the individual handoffs aggregated into one edge.
- Rendered nodes for declared-but-uncalled `@node`s (registry exists in
  `recorder.make_meta`; wiring to render is the gap).
- Dedicated SSE event for trace attribute/metadata changes, so an open live
  workspace updates status/lifecycle inspector fields without a topology push.
- Optional persistent, closable workspace tabs; deliberately removed from the
  initial trace-history UI until there is a real multi-workspace need.
