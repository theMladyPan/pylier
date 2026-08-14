# Scenarios

Real-world patterns for tracing pipelines with pylier.

- [Document ingestion](ingestion.md) — a branched text/image pipeline that concurrently embeds both branches before converging.
- [Preserving multi-source lineage with `derive`](derive-lineage.md) — when a plain Python transformation wipes value provenance and how to keep it.
- [Autotrace without decorators](autotrace.md) — trace an existing codebase with zero decorators using `sys.monitoring`.
- [Capture levels](levels.md) — `core < info < debug < trace` to dial structure vs detail.
- [FastAPI integration](fastapi.md) — per-request traces from a pure-ASGI middleware.
- [Sharing traces](sharing.md) — static HTML, debug bundles, sidecar audit trails, and bounded value capture.
- [Live viewer](live-viewer.md) — the SSE push model behind `pylier.serve()`.
- [Demos](../demos.md) — published interactive graph replays.
