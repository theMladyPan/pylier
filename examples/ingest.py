"""Example: a branched document-ingest pipeline visualized with pylier.

Run::

    uv run python -m examples.ingest serve   # live viewer at http://localhost:8765
    uv run python -m examples.ingest html     # writes ./pylier-ingest.html
"""

from __future__ import annotations

import asyncio
import sys
import time

import pylier

# Per-step delay used in ``serve`` mode so the live viewer (SSE push) actually
# has time to stream each node as it appears. Zero in ``html`` mode (one-shot).
_STEP_DELAY = 0.0


@pylier.node
def load_document(path: str) -> dict:
    time.sleep(_STEP_DELAY)
    return {"path": path, "pages": ["page1 text", "page2 text"], "images": ["img1.png"]}


@pylier.node(_tags=["document", "text"])
def extract_text(doc: dict) -> list[str]:
    time.sleep(_STEP_DELAY)
    return [p.upper() for p in doc["pages"]]


@pylier.node(_tags=["document", "images"])
def ocr_images(doc: dict) -> list[str]:
    time.sleep(_STEP_DELAY)
    return [f"ocr:{img}" for img in doc["images"]]


@pylier.node(_tags=["embedding"])
async def embed(chunks: list[str]) -> list[dict]:
    await asyncio.sleep(_STEP_DELAY)
    return [{"vec": [len(chunk), 0], "text": chunk} for chunk in chunks]


@pylier.node(_tags=["indexing"])
async def index(document_text: list[str], image_text: list[str]) -> int:
    """Embed extracted document and image text concurrently, then index every vector."""
    await asyncio.sleep(_STEP_DELAY)
    document_vectors, image_vectors = await asyncio.gather(embed(document_text), embed(image_text))
    return len(document_vectors) + len(image_vectors)


async def run_pipeline(name: str) -> int:
    """Execute one full ingest run, returning the number of indexed vectors."""
    with pylier.trace(name):
        doc = load_document("report.pdf")
        text_chunks = extract_text(doc)
        image_chunks = ocr_images(doc)
        total = await index(text_chunks, image_chunks)
    return total


def main(mode: str) -> None:
    global _STEP_DELAY
    if mode == "serve":
        # Live mode: open ONE trace and keep it open so the viewer (which
        # resolves the active trace at serve time) watches the same object.
        # Each node call bumps the trace version and the SSE endpoint pushes
        # the change, so the graph grows node-by-node in the browser. Slow
        # each stage so the streaming is visible. Iterating re-enters nodes,
        # so call-counts accumulate and the badge counts climb live.
        _STEP_DELAY = 1.5
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
        _STEP_DELAY = 0.0
        total = asyncio.run(run_pipeline("doc-ingest"))
        print(f"indexed {total} vectors")
        out = pylier.render("pylier-ingest.html")
        print(f"wrote {out}")
    else:
        print(f"unknown mode {mode!r}; use 'serve' or 'html'")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "html")
