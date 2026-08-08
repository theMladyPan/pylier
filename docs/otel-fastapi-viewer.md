# FastAPI OTel request viewer

Implemented an optional ASGI/FastAPI adapter that reads an already-active OpenTelemetry server span, captures decorated algorithms under a synthetic FastAPI request root, and retains all runs in the shared live viewer history. The shared renderer now provides root-trace and endpoint tabs plus a FastAPI-only `START` callout; ordinary decorator traces remain supported without OTel.

## Why

FastAPI's OTel span owns the request boundary, while pylier owns inferred algorithm data flow. This keeps pylier OTel-compatible rather than OTel-only, and makes completed requests available for debugging in one viewer.
