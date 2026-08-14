"""Regression coverage for the MkDocs documentation site."""

from pathlib import Path

import yaml

REPO = Path(__file__).parents[1]
MKDOCS_YML = REPO / "mkdocs.yml"
DOCS_DIR = REPO / "docs"

REQUIRED_FILES = [
    "index.md",
    "quickstart.md",
    "scenarios/derive-lineage.md",
    "scenarios/autotrace.md",
    "scenarios/levels.md",
    "scenarios/fastapi.md",
]


def _nav_md_paths(nav):
    """Yield every .md path referenced in the mkdocs nav tree."""
    for item in nav or []:
        if isinstance(item, str):
            if item.endswith(".md"):
                yield item
        elif isinstance(item, dict):
            for value in item.values():
                if isinstance(value, str) and value.endswith(".md"):
                    yield value
                elif isinstance(value, list):
                    yield from _nav_md_paths(value)


def test_mkdocs_config_exists():
    assert MKDOCS_YML.is_file(), "mkdocs.yml missing at repo root"


def test_every_nav_file_exists_on_disk():
    config = yaml.safe_load(MKDOCS_YML.read_text(encoding="utf-8"))
    for rel in _nav_md_paths(config.get("nav", [])):
        assert (DOCS_DIR / rel).is_file(), f"nav references missing file: docs/{rel}"


def test_required_doc_files_exist():
    for rel in REQUIRED_FILES:
        assert (DOCS_DIR / rel).is_file(), f"required doc missing: docs/{rel}"


def test_derive_article_explains_provenance_loss():
    """The most important article must explain WHY provenance is lost."""
    text = (DOCS_DIR / "scenarios" / "derive-lineage.md").read_text(encoding="utf-8")
    # Guards the why-provenance-is-lost explanation: plain Python expressions
    # produce new objects, wiping the fingerprint link to source values.
    assert "provenance is lost" in text.lower(), "derive article must explain provenance loss"
    assert "new" in text and "object" in text, "derive article must explain new-object identity loss"


def test_no_broken_admonition_syntax():
    """MkDocs admonitions need three marks; `!! ` is a typo that renders as text."""
    offenders: list[str] = []
    for md in DOCS_DIR.rglob("*.md"):
        for line in md.read_text(encoding="utf-8").splitlines():
            if line.startswith("!! "):
                offenders.append(f"{md.relative_to(REPO)}: {line.strip()}")
    assert not offenders, "broken admonitions (use `!!! ` not `!! `):\n" + "\n".join(offenders)
