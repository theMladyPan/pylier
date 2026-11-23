# Preserving multi-source lineage with `derive`

`pylier.derive(value, from_=[...])` is the one escape hatch in pylier's
edge-inference model. It returns the original value unchanged; its only job is
to keep branch lineage visible in the next decorated stage when ordinary Python
would otherwise wipe it.

## Why provenance is lost

pylier infers **Data Flow** edges by fingerprinting values (type + hash) as they
move between decorated functions. A fingerprint identifies a value, so when a
decorated consumer receives a matching non-empty value, pylier draws a
producer-to-consumer edge.

The problem: ordinary Python expressions produce **new** objects whose identity
has no link back to their sources:

```python
merged = text_vectors + image_vectors   # brand-new list, no provenance
```

By the time pylier fingerprints `merged`, the link to `text_vectors` and
`image_vectors` is gone — the `+` operator created a fresh object. The Data Flow
view will show `merged` feeding the next node, but **both branches' lineage is
lost**.

This is a deliberate, documented trade-off: fingerprinting misses
transformed/aggregated copies. `derive` is the supported workaround.

## When to use `derive`

Reach for `derive` whenever a **plain-Python transformation or join** produces a
new value that you want the next decorated stage to consume with its sources
still visible:

- concatenating lists / merging dicts
- summing or aggregating
- any `a + b`, `list(x) + list(y)`, `{**a, **b}` that collapses multiple sources

The canonical case is a **multi-source merge** in a branched pipeline.

## How it works

```python
vectors = pylier.derive(text_vectors + image_vectors, from_=[text_vectors, image_vectors])
```

- `derive` returns the original value **unchanged** — it is not a transform.
- `from_=[...]` declares the sources whose lineage should be preserved.
- pylier records **resolved, transitive** source lineage for the next decorated
  consumer of that value.
- Unknown sources (values pylier has no recorded producer for) warn and are
  ignored — they do not raise.

## With vs without `derive`

Without `derive`, the Data Flow view collapses both branches into one anonymous
value:

```
text_vectors ─┐
              ├─ (concatenated value, lineage lost) ─→ index
image_vectors ─┘
```

With `derive`, both branches appear as direct sources into the next decorated
consumer:

```
text_vectors ──────┐
                    ├─→ index
image_vectors ─────┘
```

## When NOT to use `derive`

!!! warning "Don't sprinkle `derive` everywhere"
    `derive` is **only** needed when a plain transformation or join wipes
    provenance. Pass-through and direct returns already preserve lineage —
    decorating a function and returning a value it received keeps the edge.

    Adding `derive` to every line adds noise without benefit. Use it at joins
    and aggregations, nowhere else.

!!! note "Known limitation"
    Fingerprinting missing transformed/aggregated copies is an accepted
    trade-off, not a bug to silently patch. `derive` is the documented,
    intentional way to preserve multi-source lineage when you need it.
