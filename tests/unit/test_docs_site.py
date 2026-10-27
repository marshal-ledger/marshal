"""The documentation site lists every page, and its hooks point links outside docs/ at the repository."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import yaml

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"


def hooks():
    spec = importlib.util.spec_from_file_location("mkdocs_hooks", ROOT / "scripts" / "mkdocs_hooks.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def nav_pages(items):
    for item in items:
        value = next(iter(item.values())) if isinstance(item, dict) else item
        if isinstance(value, list):
            yield from nav_pages(value)
        elif not value.startswith("http"):
            yield value


def test_every_page_is_in_the_navigation():
    config = yaml.safe_load((ROOT / "mkdocs.yml").read_text())
    listed = set(nav_pages(config["nav"]))
    pages = {p.relative_to(DOCS).as_posix() for p in DOCS.rglob("*.md")}
    assert pages == listed


def test_links_outside_docs_point_at_the_repository():
    page = SimpleNamespace(file=SimpleNamespace(abs_src_path=str(DOCS / "deployment.md")), meta={})
    markdown = ("Marshal runs as a container.\n\nSee [the chart](../charts/marshal), [values](../charts/marshal/values.yaml#L3), "
                "[Azure](azure.md#store), [formats](formats/) and [PyPI](https://pypi.org/project/marshal-ledger/).")
    out = hooks().on_page_markdown(markdown, page, None, None)
    assert "[the chart](https://github.com/marshal-ledger/marshal/tree/main/charts/marshal)" in out
    assert "[values](https://github.com/marshal-ledger/marshal/blob/main/charts/marshal/values.yaml#L3)" in out
    assert "[formats](https://github.com/marshal-ledger/marshal/tree/main/docs/formats)" in out
    assert "[Azure](azure.md#store)" in out and "(https://pypi.org/project/marshal-ledger/)" in out
    assert page.meta["description"] == "Marshal runs as a container."
