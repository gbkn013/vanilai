"""Agreement statistics and a 0-100 confidence score.

Confidence combines how closely the sources agree on temperature and on
*whether it rains*, how many independent model families contributed, and how
far ahead the hour is.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from statistics import pstdev

from ..providers.base import MemberForecast

SPREAD_SCALE_C = 3.0  # std dev (°C) at which temperature agreement falls to ~37 %
FULL_COVERAGE_FAMILIES = 4  # families needed for full coverage credit
MAX_LEAD_PENALTY = 0.3  # confidence lost by the 7-day horizon
HORIZON_HOURS = 168
RAIN_DISAGREEMENT_PENALTY = 0.5  # worst-case rain disagreement halves confidence
WET_MM = 0.1  # mm/h at which a model counts as "raining" that hour


@dataclass
class TempStats:
    """Cross-model agreement for one hour."""

    minimum: float
    maximum: float
    std: float
    families: int
    # 1.0 = every family agrees on rain / no rain; 0.0 = a perfect 50/50 split.
    rain_agreement: float = 1.0


def temperature_stats(members: list[MemberForecast]) -> dict[datetime, TempStats]:
    values: dict[datetime, list[float]] = defaultdict(list)
    by_family: dict[datetime, dict[str, list[float]]] = defaultdict(
        lambda: defaultdict(list)
    )
    wet: dict[datetime, dict[str, list[bool]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for member in members:
        for point in member.hours:
            if point.temperature is not None:
                values[point.time].append(point.temperature)
                by_family[point.time][member.family].append(point.temperature)
            if point.precipitation is not None:
                wet[point.time][member.family].append(point.precipitation >= WET_MM)
    out: dict[datetime, TempStats] = {}
    for t, v in values.items():
        # Spread is measured between families (one vote each), so a family
        # with many near-identical models doesn't fake agreement.
        family_means = [sum(f) / len(f) for f in by_family[t].values()]
        rain = 1.0
        if len(wet[t]) > 1:
            share = sum(sum(f) / len(f) for f in wet[t].values()) / len(wet[t])
            rain = 1 - 4 * share * (1 - share)
        out[t] = TempStats(
            minimum=min(v),
            maximum=max(v),
            std=pstdev(family_means) if len(family_means) > 1 else 0.0,
            families=len(family_means),
            rain_agreement=rain,
        )
    return out


def confidence(stats: TempStats | None, lead_hours: float) -> int:
    """0-100 score; 0 when there is no data."""
    if stats is None or stats.families == 0:
        return 0
    temperature = math.exp(-stats.std / SPREAD_SCALE_C)
    rain = 1 - RAIN_DISAGREEMENT_PENALTY * (1 - stats.rain_agreement)
    coverage = min(1.0, stats.families / FULL_COVERAGE_FAMILIES) ** 0.5
    lead = 1 - MAX_LEAD_PENALTY * min(max(lead_hours, 0) / HORIZON_HOURS, 1)
    return round(100 * temperature * rain * coverage * lead)
