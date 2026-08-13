# Autotrace implementation record

`pylier.autotrace()` now installs one process-global `sys.monitoring` hook that reuses the recorder for public Python callables, honors `allow_empty` / `min_exec_time` / `filter_prefix` / `modules`, skips explicit `@pylier.node` targets, and keeps sync, async, generator, and async-generator lifecycles as single invocations. The feature exists because users wanted the same Plotly/logfire "just trace my app" experience without decorating every function, while preserving the existing flat API, edge semantics, payload rules, and sidecar/viewer behavior.
