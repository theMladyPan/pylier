# FastAPI OTel request viewer

Implemented an optional ASGI/FastAPI adapter that reads an already-active OpenTelemetry server span, captures decorated algorithms under a synthetic FastAPI request root, and enables the universal in-process OTel span bridge. The shared renderer retains all runs in root-trace history, prioritizes the FastAPI root for `START`, and shows imported child spans; ordinary decorator traces remain supported without OTel.

## Why

FastAPI's OTel span owns the request boundary, while pylier owns inferred algorithm data flow. This keeps pylier OTel-compatible rather than OTel-only, and makes completed requests available for debugging in one viewer.
