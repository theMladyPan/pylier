"""Example: a branched document-ingest pipeline visualized with pylier.

Run::

    uv run python examples/ingest.py serve   # live viewer at http://localhost:8765
    uv run python examples/ingest.py html     # writes ./pylier-ingest.html
"""

from __future__ import annotations

import sys

import pylier


@pylier.node
def load_document(path: str) -> dict:
    return {"path": path, "pages": ["page1 text", "page2 text"], "images": ["img1.png"]}


@pylier.node(payload_kind="trigger")
def extract_text(doc: dict) -> list[str]:
    return [p.upper() for p in doc["pages"]]


@pylier.node
def ocr_images(doc: dict) -> list[str]:
    return [f"ocr:{img}" for img in doc["images"]]


@pylier.node
def embed(chunks: list[str]) -> list[dict]:
    return [{"vec": [len(c), 0], "text": c} for c in chunks]


@pylier.node
def index(vectors: list[dict]) -> int:
    return len(vectors)


def main(mode: str) -> None:
    with pylier.trace("doc-ingest"):
        doc = load_document("report.pdf")
        text_chunks = extract_text(doc)
        image_chunks = ocr_images(doc)
        text_vecs = embed(text_chunks)
        image_vecs = embed(image_chunks)
        total = index(text_vecs + image_vecs)

    print(f"indexed {total} vectors")
    if mode == "serve":
        server = pylier.serve()
        try:
            input("press enter to stop...")
        except EOFError:
            pass
        finally:
            server.shutdown()
    elif mode == "html":
        out = pylier.render("pylier-ingest.html")
        print(f"wrote {out}")
    else:
        print(f"unknown mode {mode!r}; use 'serve' or 'html'")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "html")
