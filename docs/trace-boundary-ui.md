# Generic trace metadata and boundary UI

`Trace.metadata` replaces HTTP-shaped endpoint state; the FastAPI adapter stores `status_code` there, while the right inspector renders every metadata key/value generically. Managed traces now serialize start/end timestamps, the Application Flow root is a clickable circle port, and the viewer uses sticky single-workspace trace history without tabs.

This change distinguishes external trace boundaries from algorithm nodes and keeps future integrations from forcing transport-specific fields into the neutral core. Future work: dedicated SSE updates for changed trace attributes/metadata and optional persistent workspace tabs.