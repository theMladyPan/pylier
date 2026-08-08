# Application and data-flow perspectives

Application Flow uses direct invocation handoffs: nested decorated calls use their active caller and top-level calls use the trace root. Data Flow separately records fingerprint-inferred producer-to-consumer relations, including every decorated consumer of a matching value; its links exclude roots, unmatched values, `None`, exceptions, and control-only handoffs.

The renderer selects either perspective from one trace snapshot. Repeated function-pair links aggregate visually while retaining invocation-level handoffs for inspection; `pylier.derive(...)` preserves intentional multi-producer data provenance.
