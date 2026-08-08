"""Regression coverage for the static GitHub Pages entry point."""

from pathlib import Path

PAGES_INDEX = Path(__file__).parents[1] / "pages" / "index.html"


def test_pages_landing_links_to_both_published_demo_artifacts():
    """Keep the landing page aligned with the artifact names deployed by CI."""
    page = PAGES_INDEX.read_text(encoding="utf-8")

    assert 'href="ingest.html"' in page
    assert 'href="fulfillment.html"' in page
