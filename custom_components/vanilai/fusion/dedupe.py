"""Drop sources that are exact copies of another source.

Outside its home region a "regional" model is often just a global model under
a different name. Counting it twice would fake agreement, so members whose
numbers are identical to one already kept are discarded.
"""
from __future__ import annotations

from ..providers.base import MemberForecast

MIN_POINTS = 24  # need this many values to call two series "identical"
WINDOW_HOURS = 48


def _fingerprint(member: MemberForecast) -> tuple | None:
    window = member.hours[:WINDOW_HOURS]
    temps = tuple(
        None if h.temperature is None else round(h.temperature, 1) for h in window
    )
    if sum(t is not None for t in temps) < MIN_POINTS:
        return None  # too little data to judge
    precip = tuple(
        None if h.precipitation is None else round(h.precipitation, 1)
        for h in window
    )
    return temps + precip


def drop_duplicates(
    members: list[MemberForecast],
) -> tuple[list[MemberForecast], list[str]]:
    """Return (kept members, names of dropped duplicates). Order is preserved."""
    seen: dict[tuple, str] = {}
    kept: list[MemberForecast] = []
    dropped: list[str] = []
    for member in members:
        fingerprint = _fingerprint(member)
        if fingerprint is not None and fingerprint in seen:
            dropped.append(f"{member.member} (copy of {seen[fingerprint]})")
            continue
        if fingerprint is not None:
            seen[fingerprint] = member.member
        kept.append(member)
    return kept, dropped
