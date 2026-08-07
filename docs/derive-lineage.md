# Derive lineage

`pylier.derive(value, from_=[...])` returns the original value while preserving resolved, transitive source lineage for a later decorated consumer; unknown sources warn and are ignored. It exists because ordinary Python expressions such as `a + b` discard provenance before pylier can fingerprint the result.
