"""Secret redaction for text that readers copy into a record.

Readers copy only an allowlist of fields from source artifacts, so values such
as environment variables never reach a record. Free text that is copied, such as
instructions and descriptions, passes through :func:`redact` as a second line of
defense against credentials pasted into prompts.
"""

from __future__ import annotations

import re

REDACTED = "[REDACTED]"

_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bdapi[0-9a-f]{32}(?:-\d)?\b"),  # Databricks personal access token
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),  # AWS access key ID
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),  # GitHub token
    re.compile(r"\bxox[abpors]-[A-Za-z0-9-]{10,}\b"),  # Slack token
    re.compile(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}\b"),  # OpenAI / Anthropic style keys
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),  # Google API key
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),  # JWT
]


def redact(text: str) -> tuple[str, int]:
    """Return ``text`` with known credential patterns replaced, and the number replaced."""
    count = 0
    for pattern in _PATTERNS:
        text, n = pattern.subn(REDACTED, text)
        count += n
    return text, count
