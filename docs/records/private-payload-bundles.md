# Private payload bundles

`pylier.render(..., embed_payloads=True)` embeds the existing bounded retained invocation Inputs/Outputs into a static debug HTML bundle; normal static renders and JSONL sidecars remain metadata-only. The template receives an explicit live/static flag so HTTP-hosted artifacts never call local endpoints, and the synthetic ingest and fulfillment examples opt in so their public GitHub Pages demos support full inspection.
