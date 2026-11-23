# Live viewer

`pylier.serve()` streams the graph to an in-process viewer as work happens.
Open it at `http://localhost:8765` (default port) and watch nodes appear and
connect the moment data moves.

## Push, not poll

The viewer uses **Server-Sent Events** (SSE). There are two event streams:

| Event | When | Payload |
|---|---|---|
| `event: graph` | Topology change only — new trace, node, or edge (rare) | full graph snapshot |
| `event: exec` | Every enter/exit event (real time) | compact batch of activity |

Topology changes bump `graph_version`; call activity bumps `exec_version`. Call
counters do **not** bump `graph_version` — clients update the call-count badges
locally from exec batches, so a busy pipeline doesn't trigger constant full
snapshots.

## Persistent simulation

The client keeps a **persistent force simulation + D3 join**. It never tears
down and cold-restarts the graph on updates — that was the original 1.5s tearing
bug. Instead, new nodes spawn near an already-placed neighbor, not the center, so
the layout stays stable as the graph grows.

## Trace history

The left pane lists retained traces: name, node count, and start clock. There is
one workspace — a user selection stays active as live history grows, rather than
being hijacked by the newest trace.

!!! note "History is bounded"
    `TraceHistory` caps at 100 traces. Older traces drop off as new ones arrive.

## Export one trace

Right-click any trace tab and choose **Export**. The viewer downloads that one
run as a standalone HTML file: the graph data plus the retained invocation
payloads, produced by the same render core as
`pylier.render(..., embed_payloads=True)`. The saved file needs no server — open
it from `file://`, attach it to a bug report, or host it statically.

Payloads appear only when capture was on (`PYLIER_CAPTURE_VALUES=1`). Without
it the file carries node identity, payload types, sizes, and previews, which is
all the run held.

!!! warning "The export contains captured values"
    Anyone who opens the downloaded file can read the payloads it embedded.
    Export synthetic or debug runs freely; treat a real one like the data it
    carried.

!!! note "Static artifacts disable the item"
    A saved HTML file has no server to ask, and it is already a standalone file,
    so **Export** appears disabled there.

## Static vs live

Both the static `pylier.render()` and the live `pylier.serve()` use the same
`render/template.html` as the single source of truth for the graph look — no
drift between test artifacts and live preview. Static embeds the graph JSON and
replays the execution animation once on load; live subscribes to SSE and
re-renders in place.

!!! note "In-process only today"
    The live viewer pushes retained **in-memory** traces. Cross-process sidecar
    tailing and an external telemetry receiver are fast-follows, not yet
    shipped.
