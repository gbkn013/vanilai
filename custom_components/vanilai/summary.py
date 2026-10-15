"""Plain-language summary of the next 24 hours (pure logic, no HA imports)."""
from __future__ import annotations

from collections import Counter
from datetime import tzinfo
from typing import Any

from .providers.base import HourPoint

CONDITION_WORDS = {
    "sunny": "Sunny",
    "clear-night": "Clear",
    "partlycloudy": "Partly cloudy",
    "cloudy": "Cloudy",
    "fog": "Foggy",
    "rainy": "Rainy",
    "pouring": "Heavy rain",
    "snowy": "Snowy",
    "snowy-rainy": "Sleet",
    "lightning": "Thunderstorms",
    "lightning-rainy": "Thunderstorms",
    "hail": "Hail",
    "windy": "Windy",
}

RAIN_PROBABILITY = 50  # % at which we call it "rain likely"
RAIN_MM = 0.3  # mm/h at which an hour counts as wet regardless of probability


def confidence_label(score: float) -> str:
    if score >= 75:
        return "High"
    if score >= 50:
        return "Medium"
    return "Low"


def _wet(p: HourPoint) -> bool:
    return (p.precipitation_probability or 0) >= RAIN_PROBABILITY or (
        (p.precipitation or 0) >= RAIN_MM
    )


def first_rain(hours: list[HourPoint]) -> HourPoint | None:
    """The first hour that counts as wet, if any."""
    return next((h for h in hours if _wet(h)), None)


def _clock(p: HourPoint, tz: tzinfo) -> str:
    return p.time.astimezone(tz).strftime("%H:%M")


def rain_phrase(hours: list[HourPoint], tz: tzinfo) -> str:
    if not hours:
        return ""
    peak = max((h.precipitation_probability or 0) for h in hours)
    wet = [i for i, h in enumerate(hours) if _wet(h)]
    if not wet:
        if peak >= 30:
            return f"Small chance of rain (up to {round(peak)}%)."
        return "No rain expected."
    first = wet[0]
    if first == 0:
        end = next((i for i in range(1, len(hours)) if not _wet(hours[i])), None)
        if end is None:
            return f"Rain through the next {len(hours)} hours."
        return f"Raining now, easing after {_clock(hours[end], tz)}."
    return f"Rain likely from {_clock(hours[first], tz)} (up to {round(peak)}%)."


def summarize(
    hours: list[HourPoint], score: float | None, tz: tzinfo
) -> dict[str, Any]:
    """`hours` is the next ~24 fused hours; `score` is mean confidence 0-100."""
    temps = [h.temperature for h in hours if h.temperature is not None]
    if not hours or not temps:
        return {"text": "No forecast data yet.", "label": "Low"}

    conditions = [
        h.condition for h in hours if h.condition and h.condition != "clear-night"
    ] or [h.condition for h in hours if h.condition]
    sky = (
        CONDITION_WORDS.get(Counter(conditions).most_common(1)[0][0], "")
        if conditions
        else ""
    )
    low, high = round(min(temps)), round(max(temps))
    temp_part = f"{low}°C" if low == high else f"{low}–{high}°C"
    lead = f"{sky}, " if sky else ""

    label = confidence_label(score) if score is not None else "Low"
    agreement = {
        "High": "Weather models agree well.",
        "Medium": "Weather models partly disagree.",
        "Low": "Weather models disagree, so treat this with caution.",
    }[label]

    return {
        "text": f"{lead}{temp_part} over the next 24 h. "
        f"{rain_phrase(hours, tz)} {agreement}",
        "label": label,
        "low": low,
        "high": high,
        "sky": sky,
    }
