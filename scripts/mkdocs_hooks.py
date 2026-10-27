"""MkDocs hooks for the documentation site (see mkdocs.yml).

The docs link to files outside docs/, such as examples, the chart and the source,
and to folders. Those work on GitHub but are not pages of the site, so the links
are pointed at the repository. Pages without a description get one from their
first paragraph, for search results and link previews.
"""

from __future__ import annotations

import re
from pathlib import Path

REPOSITORY = "https://github.com/marshal-ledger/marshal"
ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
LINK = re.compile(r"(\]\()([^)\s#]+)(#[^)\s]*)?(\))")


def _repository_link(match: re.Match[str], page_dir: Path) -> str:
    target = match[2]
    if re.match(r"[a-z]+:", target):
        return match[0]
    path = (page_dir / target).resolve()
    if not path.is_relative_to(ROOT) or path.is_relative_to(DOCS) and not path.is_dir():
        return match[0]
    kind = "tree" if path.is_dir() else "blob"
    url = f"{REPOSITORY}/{kind}/main/{path.relative_to(ROOT).as_posix()}"
    return f"{match[1]}{url}{match[3] or ''}{match[4]}"


def _first_paragraph(markdown: str) -> str:
    for block in re.split(r"\n\s*\n", markdown):
        block = block.strip()
        if not block or block.startswith(("#", "<", "|", "```", "-", "*", ">", "!", "1.")):
            continue
        text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", block)
        text = re.sub(r"[`*_]", "", " ".join(text.split()))
        return text if len(text) <= 160 else text[:157].rsplit(" ", 1)[0] + "…"
    return ""


def on_page_markdown(markdown, page, config, files):
    page_dir = Path(page.file.abs_src_path).parent
    if not page.meta.get("description"):
        description = _first_paragraph(markdown)
        if description:
            page.meta["description"] = description
    return LINK.sub(lambda m: _repository_link(m, page_dir), markdown)
