"""Family-weighted fusion with optional learned skill weights.

Models from the same family (e.g. several ICON variants) are correlated, so
each family gets one vote, split evenly among its members. A `skill` callback
(learned from observations) can scale any member up or down by lead time.
"""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Callable
from datetime import datetime

from ..providers.base import HourPoint, MemberForecast

# Fused with a weighted median (robust to one wild model).
_MEDIAN = (
    "temperature",
    "humidity",
    "pressure",
    "wind_speed",
    "precipitation",
    "cloud_coverage",
)

WET_MM = 0.1  # mm/h at which a model counts as "raining" that hour
MIN_PROB_FAMILIES = 3  # families that must report probability to trust the mean

# Higher = worse. Used to break ties toward the cautious outcome.
_SEVERITY = [
    "clear-night", "sunny", "partlycloudy", "cloudy", "windy", "fog",
    "rainy", "snowy-rainy", "snowy", "pouring", "lightning", "lightning-rainy",
    "hail", "exceptional",
]
_SEVERITY_RANK = {c: i for i, c in enumerate(_SEVERITY)}

SkillFn = Callable[[str, float], float]  # (member, lead_hours) -> weight multiplier


def weighted_median(pairs: list[tuple[float, float]]) -> float:
    """Median of (value, weight) pairs; exact ties return the midpoint."""
    ordered = sorted(pairs)
    total = sum(w for _, w in ordered)
    half = total / 2
    cumulative = 0.0
    for i, (value, weight) in enumerate(ordered):
        cumulative += weight
        if math.isclose(cumulative, half, rel_tol=1e-9):
            return (value + ordered[i + 1][0]) / 2 if i + 1 < len(ordered) else value
        if cumulative > half:
            return value
    return ordered[-1][0]


def weighted_mean(pairs: list[tuple[float, float]]) -> float:
    total = sum(w for _, w in pairs)
    return sum(v * w for v, w in pairs) / total


def weighted_circular_mean(pairs: list[tuple[float, float]]) -> float:
    x = sum(w * math.cos(math.radians(d)) for d, w in pairs)
    y = sum(w * math.sin(math.radians(d)) for d, w in pairs)
    return math.degrees(math.atan2(y, x)) % 360


def family_weights(members: list[MemberForecast]) -> dict[str, float]:
    """member name -> weight, so every family totals 1."""
    sizes = Counter(m.family for m in members)
    return {m.member: 1.0 / sizes[m.family] for m in members}


def wet_fraction(entries: list[tuple[float, str, HourPoint]]) -> float | None:
    """Weighted share of families whose models expect rain this hour."""
    per_family: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for weight, family, point in entries:
        if point.precipitation is not None:
            per_family[family].append((1.0 if point.precipitation >= WET_MM else 0.0, weight))
    if not per_family:
        return None
    votes = [weighted_mean(v) for v in per_family.values()]  # each family 0..1
    return sum(votes) / len(votes)


def fuse(
    members: list[MemberForecast],
    *,
    now: datetime | None = None,
    skill: SkillFn | None = None,
) -> list[HourPoint]:
    """Combine members hour by hour."""
    base = family_weights(members)
    by_time: dict = defaultdict(list)
    for member in members:
        for point in member.hours:
            weight = base[member.member]
            if skill is not None and now is not None:
                lead = (point.time - now).total_seconds() / 3600
                weight *= skill(member.member, lead)
            by_time[point.time].append((weight, member.family, point))

    fused: list[HourPoint] = []
    for time in sorted(by_time):
        entries = by_time[time]
        out = HourPoint(time=time)

        def pairs(name: str) -> list[tuple[float, float]]:
            return [
                (getattr(p, name), w)
                for w, _, p in entries
                if getattr(p, name) is not None and w > 0
            ]

        for name in _MEDIAN:
            if vals := pairs(name):
                setattr(out, name, round(weighted_median(vals), 2))
        if bearings := pairs("wind_bearing"):
            out.wind_bearing = round(weighted_circular_mean(bearings), 1)

        # Rain probability: models that publish one are averaged, but only
        # when enough independent families do. Otherwise fall back to how
        # many families expect measurable rain, which every model provides.
        probs = pairs("precipitation_probability")
        prob_families = {f for w, f, p in entries
                         if p.precipitation_probability is not None and w > 0}
        if probs and len(prob_families) >= MIN_PROB_FAMILIES:
            out.precipitation_probability = round(weighted_mean(probs), 1)
        elif (share := wet_fraction([e for e in entries if e[0] > 0])) is not None:
            out.precipitation_probability = round(100 * share, 1)

        votes: dict[str, float] = defaultdict(float)
        for w, _, p in entries:
            if p.condition:
                votes[p.condition] += w
        if votes:
            out.condition = max(
                votes, key=lambda c: (votes[c], _SEVERITY_RANK.get(c, 0))
            )
        fused.append(out)
    return fused
