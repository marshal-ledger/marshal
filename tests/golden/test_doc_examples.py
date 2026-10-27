"""Example outputs in the docs are what Marshal prints for them, so none is written by hand."""

import importlib.util
import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / "tests" / "fixtures" / ".cache"


def doc_examples():
    spec = importlib.util.spec_from_file_location("doc_examples", ROOT / "scripts" / "doc_examples.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.skipif(not CACHE.is_dir(), reason="fixtures not fetched; run python scripts/fetch_fixtures.py")
@pytest.mark.skipif(bool(os.environ.get("MARSHAL_FIXTURE_CACHE")), reason="the docs show the pinned fixtures")
def test_doc_examples_are_current():
    stale = doc_examples().update(check=True)
    assert stale == [], "run python scripts/doc_examples.py to regenerate: " + ", ".join(stale)


def test_every_example_marker_has_a_block():
    marker = re.compile(r"<!-- example: [\w-]+ -->\n(?!```)")
    pages = [ROOT / "README.md", *(ROOT / "docs").rglob("*.md")]
    orphans = [str(p.relative_to(ROOT)) for p in pages if marker.search(p.read_text())]
    assert orphans == []
