"""Rendering ingest reports as terminal text and as pull request comments."""

from __future__ import annotations

from .service import AgentReport

COMMENT_MARKER = "<!-- marshal-ledger:report -->"


def _portability(report: AgentReport) -> str:
    if not report.portability:
        return ""
    return " · Portability: " + " · ".join(f"{name} {score}" for name, score in report.portability.items())


def _transition(report: AgentReport) -> str:
    if report.baseline is None:
        return f"new → {report.label}"
    return f"{report.baseline} → {report.label}"


def text(reports: list[AgentReport]) -> str:
    blocks = []
    for report in reports:
        lines = [f"{report.agent} ({report.platform})  {report.environment}: {_transition(report)}", ""]
        if report.drift:
            lines.insert(1, f"  DRIFT: the live definition differs from shipped {report.baseline}")
        if report.changes:
            lines += [f"  {c.line()}" for c in report.changes]
        else:
            lines.append("  no changes from what is shipped")
        lines += ["", f"  Completeness {report.completeness:.0%}" + _portability(report)]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def markdown(reports: list[AgentReport]) -> str:
    """A pull request comment. The marker lets the GitHub Action update its own comment in place."""
    environments = sorted({r.environment for r in reports})
    title = "Marshal: agent changes"
    if environments:
        title += " for " + ", ".join(f"`{e}`" for e in environments)
    parts = [COMMENT_MARKER, f"### {title}", ""]

    changed = [r for r in reports if r.changes]
    unchanged = [r for r in reports if not r.changes]
    if not reports:
        parts.append("No agents found in the collected artifacts.")
    for report in changed:
        high = sum(1 for c in report.changes if c.severity.label == "HIGH")
        summary = f"{len(report.changes)} change{'s' * (len(report.changes) != 1)}"
        if high:
            summary += f", {high} high risk"
        if report.drift:
            summary += f" · **drift**: live differs from shipped {report.baseline}"
        parts += [
            f"**{report.agent}** ({report.platform}) · {_transition(report)} · {summary}",
            "",
            "```",
            *(c.line() for c in report.changes),
            "```",
            f"Completeness {report.completeness:.0%}" + _portability(report),
            "",
        ]
    if unchanged:
        names = ", ".join(f"`{r.agent}`" for r in unchanged)
        parts += [f"Unchanged from what is shipped: {names}", ""]
    redactions = sum(r.redactions for r in reports)
    if redactions:
        parts += [f"{redactions} credential-like value(s) were redacted before storage.", ""]
    return "\n".join(parts).rstrip() + "\n"
