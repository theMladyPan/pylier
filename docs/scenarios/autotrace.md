# Autotrace without decorators

`pylier.autotrace(...)` is the opt-in sibling of `@pylier.node` for tracing an
existing codebase with **zero decorators**. It uses stdlib `sys.monitoring` to
feed ordinary Python calls back through the same recorder path as a decorated
node, so Application Flow, Data Flow, levels, payload capture, sidecars, events,
and the viewer all behave identically.

## The one-liner

```python
import pylier
import myapp.pipeline

pylier.autotrace()  # infers the caller's package or source root

with pylier.trace("request"):
    myapp.pipeline.run()
```

pylier infers the scope from the caller's package or source root, so a single
call covers your application.

## Explicit scope

Limit it when your app spans multiple entry points:

```python
pylier.autotrace(
    modules=["myapp.pipeline", "myapp.services"],
    allow_empty=False,
    min_exec_time=0.050,
)
```

## Semantics

- `modules=[...]` matches exact module names **and their submodules**.
- `min_exec_time` is measured in seconds. A positive threshold omits warm-up
  calls until one qualifying call promotes that code object; **the first
  qualifying call is still omitted** (it is the trigger, not the first retained
  run).
- `allow_empty=False` buffers successful calls with no meaningful business input
  that return `None`, and drops **only the empty parent**. For autotrace,
  parameters whose runtime value equals their declared default are treated as
  omitted for this filter; use `allow_empty=True` if you need those calls
  retained. Committed children reconnect to the nearest retained caller or trace
  root when the call finishes.

## What is skipped

- Names starting with `_`
- Lambdas and comprehensions
- Module and class bodies
- C callables (autotrace is Python-defined-only)
- Code outside the selected scope

!!! note "Explicit decoration wins"
    An explicit `@pylier.node` always wins — autotrace will not double-record a
    function you already decorated.

!!! warning "Runtime requirements"
    `pylier.autotrace()` requires **Python 3.12+** with `sys.monitoring`
    available at runtime, and uses one global monitoring tool slot. Repeated
    identical activation is a no-op; a conflicting activation fails clearly
    rather than silently changing behavior.
