# Capture levels

Real pipelines are noisy. pylier borrows logfire's `min_level` idea: a capture
level controls both **which nodes are recorded** and **how much metadata** each
captured node carries.

The level order is:

```
core < info < debug < trace
```

## Per-node level

Set the level on individual nodes:

```python
@pylier.node(level="debug")
def extract_text(document: dict) -> list[str]: ...
```

A node is recorded only when its declared level rank is `<=` the active global
level.

## Global level

Dial the whole trace:

```python
with pylier.set_level("debug"):
    ...  # everything at debug or below is captured

# or
pylier.level("trace")  # set globally for the process
```

## The invariant

!!! warning "Node level gates capture; global level gates metadata richness."
    These are two separate controls — do not conflate them.

    - **Node level** decides whether the node is captured at all.
    - **Global level** decides how much metadata a captured node carries.

Uncaptured nodes call the raw function with zero overhead and register nothing —
otherwise they would create phantom edges into captured nodes. Level filtering
runs **before** instrumentation.

## Metadata per level

| Level | Metadata captured |
|---|---|
| `core` | identity + bare edges |
| `info` | + type + size |
| `debug` | + preview + tags |
| `trace` | + detailed |

!!! note "The skeleton is always there"
    `core` nodes stay visible at the lowest verbosity, so the pipeline skeleton
    is always present even when you dial detail down.
