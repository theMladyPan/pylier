# Direct handoff precedence

Nested decorated calls use their active invocation as the authoritative handoff, while top-level calls use the trace root as their implicit orchestration caller. Repeated calls between the same function nodes aggregate visually but retain distinct invocation records for inspection; fingerprints remain internal provenance for `pylier.derive(...)` rather than default graph edges.

This keeps ordinary function flow truthful without losing provenance needed for explicit derived values and future lineage inspection.
