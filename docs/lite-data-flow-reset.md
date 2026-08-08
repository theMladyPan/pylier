# Direct handoff precedence

Nested decorated calls now use their active invocation as the authoritative data handoff, so fingerprint lineage cannot draw a misleading producer-to-nested-callee edge. Repeated calls between the same function nodes are aggregated visually but retain distinct invocation records for edge inspection; fingerprints remain the fallback for consumers called outside a decorated parent.

This keeps ordinary function flow truthful while retaining lineage support for values reused through storage, queues, or other non-local paths.
