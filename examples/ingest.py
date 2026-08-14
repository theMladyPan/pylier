"""Example: a branched document-ingest pipeline visualized with pylier.

This is the **autotrace** demo: no ``@pylier.node`` decorators. ``pylier.autotrace``
instruments every public Python callable in this module via ``sys.monitoring``,
so the graph emerges from ordinary function calls. (Trade-off: autotrace has no
per-node tag API, so the tag-based filters in the viewer are empty here. Tagged
nodes live in ``examples/showcase.py``.)

Run::

    uv run python -m examples.ingest serve   # live viewer at http://localhost:8765
    uv run python -m examples.ingest html     # writes ./pylier-ingest.html
"""

from __future__ import annotations

import asyncio
import random
import sys
import time

import pylier

try:
    from .showcase import _capture_synthetic_values
except ImportError:
    from showcase import _capture_synthetic_values  # script-mode fallback

# Each invocation gets its own wait so concurrent embedding branches visibly
# complete independently in the live viewer.
_WAIT_RANGE = (0.01, 0.05)


def _next_wait() -> float:
    """Return a fresh simulated processing duration for one pipeline call."""
    return random.uniform(*_WAIT_RANGE)


def load_document(path: str) -> dict:
    time.sleep(_next_wait())
    return {"path": path, "pages": ["page1 text", "page2 text"], "images": ["img1.png"]}


def extract_text(doc: dict) -> list[str]:
    time.sleep(_next_wait())
    return [p.upper() for p in doc["pages"]]


def ocr_images(doc: dict) -> list[str]:
    time.sleep(_next_wait())
    return [f"ocr:{img}" for img in doc["images"]]


async def embed(chunks: list[str]) -> list[dict]:
    await asyncio.sleep(_next_wait())
    return [{"vec": [len(chunk), 0], "text": chunk} for chunk in chunks]


async def index(document_text: list[str], image_text: list[str]) -> int:
    """Embed extracted document and image text concurrently, then index every vector."""
    await asyncio.sleep(_next_wait())
    document_vectors, image_vectors = await asyncio.gather(embed(document_text), embed(image_text))
    return len(document_vectors) + len(image_vectors)


async def run_pipeline() -> int:
    """Execute one full ingest run, returning the number of indexed vectors."""
    doc = load_document("report.pdf")
    text_chunks = extract_text(doc)
    image_chunks = ocr_images(doc)
    total = await index(text_chunks, image_chunks)
    return total


def main(mode: str) -> None:
    global _WAIT_RANGE
    # Process-global autotrace: every public callable in this module becomes a
    # node. Run via `python -m examples.ingest`, this module is loaded as
    # `__main__` — so scope it as such (the common case for a script-style entry
    # point). For a library module imported by name, use `modules="pkg.mod"`
    # instead. Called once, before any `pylier.trace` block — autotrace must be
    # active before the traced calls.
    pylier.autotrace(modules=("__main__", "examples.ingest", __name__))
    if mode == "serve":
        # Live mode: open ONE trace and keep it open so the viewer (which
        # resolves the active trace at serve time) watches the same object.
        # Each node call bumps the trace version and the SSE endpoint pushes
        # the change, so the graph grows node-by-node in the browser. Slow
        # each stage so the streaming is visible. Iterating re-enters nodes,
        # so call-counts accumulate and the badge counts climb live.
        _WAIT_RANGE = (0.8, 2.0)
        with pylier.trace("doc-ingest") as tr:  # noqa: F841 (tr held for the viewer)
            server = pylier.serve()  # picks up the active trace via _last_trace
            print("viewer streaming — processing documents one at a time...")
            try:
                for i in range(1, 10):
                    doc = load_document("report.pdf")
                    text_chunks = extract_text(doc)
                    image_chunks = ocr_images(doc)
                    total = asyncio.run(index(text_chunks, image_chunks))
                    print(f"doc {i}: indexed {total} vectors")
                    time.sleep(2.0)
                input("press enter to stop...")
            except EOFError:
                pass
            finally:
                server.shutdown()
    elif mode == "html":
        # One-shot: no delays, single run, write a self-contained HTML file.
        _WAIT_RANGE = (0.01, 0.05)
        with _capture_synthetic_values():
            with pylier.trace("doc-ingest"):
                total = asyncio.run(run_pipeline())
            print(f"indexed {total} vectors")
            out = pylier.render("pylier-ingest.html", embed_payloads=True)
            print(f"wrote private debug bundle {out}")
    else:
        print(f"unknown mode {mode!r}; use 'serve' or 'html'")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "html")
