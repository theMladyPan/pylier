# Fulfillment showcase implementation

Added `examples/showcase.py`, a deterministic e-commerce demo that generates debug/info static graphs and a resolved sidecar, or autoplays the same trace through the existing live SSE viewer. It exists so evaluators and application developers can inspect shipped pylier behavior without implying cross-process live support.

## Why
A single realistic workflow makes the decorator-first, inferred-data-flow model easier to evaluate than isolated unit snippets.
