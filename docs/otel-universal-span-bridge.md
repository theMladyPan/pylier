# Universal OTel span bridge

Implemented `pylier.instrument_otel()`: an optional SDK `SpanProcessor` that imports every completed span belonging to a retained pylier trace, preserving raw OTel attributes, events, links, status, resource, and instrumentation metadata. Decorated calls create OTel child spans when an OTel context exists; the graph now supports simultaneous data/call/OTel-parent/return relations, renders returns dashed without return nodes, and prioritizes FastAPI server roots for `START`. FastAPI/ASGI transport send/receive spans remain captured with their raw OTel payload but are hidden from the primary graph unless the viewer's framework-internals toggle is enabled.

## Why

pylier is a debugger, so OTel operations such as SQLite must be visible without decorators and with the exact data their instrumentation emitted. The bridge remains in-process and optional, keeping decorator-only tracing intact while making integrations universal rather than FastAPI- or SQLite-specific.
