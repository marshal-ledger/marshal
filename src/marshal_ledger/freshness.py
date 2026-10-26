"""How long a ledger record can be trusted without being confirmed again.

A record is only as current as the last time its definition was confirmed
against the source of truth. A pipeline's ``shipped`` event is authoritative at
deploy time. A scheduled export's ``observed`` event confirms what is live, and
scheduled exports are expected to run regularly. A manual import is a snapshot someone took, so it
ages fastest.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .model import Channel


@dataclass(frozen=True)
class FreshnessPolicy:
    manual_days: float = 30
    """A manual import is stale after this many days."""
    scheduled_days: float = 7
    """A scheduled observation is stale after this many days: the scheduled export has stopped running."""
    ci_days: Optional[float] = None
    """A pipeline event is stale after this many days. None: never by age alone,
    since drift detection covers changes made outside the pipeline."""

    def max_age(self, channel: Channel) -> Optional[float]:
        return {Channel.MANUAL: self.manual_days, Channel.SCHEDULED: self.scheduled_days, Channel.CI: self.ci_days}[channel]

    def is_stale(self, channel: Channel, age_days: float) -> bool:
        limit = self.max_age(channel)
        return limit is not None and age_days > limit
