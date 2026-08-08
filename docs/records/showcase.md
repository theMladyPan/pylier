# Fulfillment showcase implementation

Added `examples/showcase.py`, a deterministic e-commerce demo that generates debug/info static graphs and a resolved sidecar, or autoplays the same trace through the existing live SSE viewer. It exists so evaluators and application developers can inspect shipped pylier behavior without implying cross-process live support.

## Topology
The showcase enters once through `fulfill_order`, then branches into assessment,
concurrent inventory and shipping work, and finalization. Inventory retains the
bounded `rank_items → expand_rank → rank_items` refinement while shipping takes
a shorter independent path; their results reconverge before publication.

## Why
A single realistic workflow makes the decorator-first, inferred-data-flow model easier to evaluate than isolated unit snippets. Nested domain orchestration also makes Application Flow demonstrate actual program structure instead of a trace-root star.
