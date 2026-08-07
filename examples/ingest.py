"""Example: a branched document-ingest pipeline visualized with pylier.

Run::

    uv run python -m examples.ingest serve   # live viewer at http://localhost:8765
    uv run python -m examples.ingest html     # writes ./pylier-ingest.html
"""

from __future__ import annotations

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


@pylier.node(payload_kind="trigger")
def extract_text(doc: dict) -> list[str]:
    time.sleep(_STEP_DELAY)
    return [p.upper() for p in doc["pages"]]


@pylier.node
def ocr_images(doc: dict) -> list[str]:
    time.sleep(_STEP_DELAY)
    return [f"ocr:{img}" for img in doc["images"]]


@pylier.node
def embed(chunks: list[str]) -> list[dict]:
    time.sleep(_STEP_DELAY)
    return [{"vec": [len(c), 0], "text": c} for c in chunks]


@pylier.node
def index(vectors: list[dict]) -> int:
    time.sleep(_STEP_DELAY)
    return len(vectors)


def run_pipeline(name: str) -> int:
    """Execute one full ingest run, returning the number of indexed vectors."""
    with pylier.trace(name):
        doc = load_document("report.pdf")
        text_chunks = extract_text(doc)
        image_chunks = ocr_images(doc)
        text_vecs = embed(text_chunks)
        image_vecs = embed(image_chunks)
        total = index(text_vecs + image_vecs)
    return total


def main(mode: str) -> None:
    global _STEP_DELAY
    if mode == "serve":
        # Live mode: slow each stage so the viewer can stream the graph as it
        # grows, node-by-node, via SSE. Each iteration re-enters the trace, so
        # repeated calls accumulate call-counts on the existing nodes and the
        # viewer pushes every change in real time.
        _STEP_DELAY = 0.6
        server = pylier.serve()
        print("viewer streaming — processing documents one at a time...")
        try:
            for i in range(1, 4):
                total = run_pipeline("doc-ingest")
                print(f"doc {i}: indexed {total} vectors")
                time.sleep(1.0)
            input("press enter to stop...")
        except EOFError:
            pass
        finally:
            server.shutdown()
    elif mode == "html":
        # One-shot: no delays, single run, write a self-contained HTML file.
        _STEP_DELAY = 0.0
        total = run_pipeline("doc-ingest")
        print(f"indexed {total} vectors")
        out = pylier.render("pylier-ingest.html")
        print(f"wrote {out}")
    else:
        print(f"unknown mode {mode!r}; use 'serve' or 'html'")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "html")
