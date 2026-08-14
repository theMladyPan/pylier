# Sharing traces

pylier gives you three ways to get a trace out of a process, plus an opt-in for
capturing full values.

## Static HTML (metadata-only)

```python
pylier.render("out.html")
```

The default. A single self-contained HTML file with the graph JSON embedded —
open it from `file://`, email it, host it statically. By design it is
**metadata-only**: node identity, types, sizes, previews, tags, edges. No
captured invocation payloads.

## Shareable debug bundle

```python
pylier.render("debug.html", embed_payloads=True)
```

The explicit opt-in. Embeds the retained invocation payloads into the HTML so
the file is fully inspectable by anyone who opens it — no server, no viewer
connection.

!!! warning "Anyone with the file can read the payloads"
    `embed_payloads=True` bundles captured values into a shareable file. Only
    use it for synthetic or debug data. The default `render()` stays
    metadata-only precisely so a shared HTML never leaks business data.

## Audit trail (sidecar)

```python
with pylier.trace("ingest", sidecar="trace.jsonl"):
    ...
```

`sidecar=` writes already-resolved handoff events to a JSONL file for offline
replay and future remote viewers. The viewer does not yet reconstruct from a
sidecar across processes — that is a fast-follow.

## Live inspector values

```bash
PYLIER_CAPTURE_VALUES=1
```

Enables lazy full-value fetch in the **live** inspector: clicking a node or edge
expands and fetches its payload from the local viewer on demand. Captured values
are bounded FIFO:

| Bound | Default |
|---|---|
| `PYLIER_PAYLOAD_MAX_INVOCATIONS` | 100 |
| `PYLIER_PAYLOAD_MAX_BYTES` | 100 MiB |

Oldest payloads are evicted first. **Binary payloads are always summaries**,
never the raw bytes, even with capture enabled.

!!! note "Published examples"
    The published demos use `embed_payloads=True` only because their data is
    synthetic. Your real traces should stay metadata-only unless you
    deliberately opt in.
