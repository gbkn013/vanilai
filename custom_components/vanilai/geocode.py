"""Reverse geocoding via OpenStreetMap Nominatim (rounded coordinates only).

Usage policy: identify the app, stay under 1 request/second, cache results.
We call only when the (rounded) location changes.
"""
from __future__ import annotations

import asyncio
from typing import Any

import aiohttp

URL = "https://nominatim.openstreetmap.org/reverse"
USER_AGENT = "vanilai-homeassistant/0.1 (personal Home Assistant integration)"

# Most specific first. "city" can be a wide administrative label such as
# "Chennai Metropolitan Area", so the actual settlement wins over it.
_LOCALITY_KEYS = (
    "village",
    "town",
    "city",
    "suburb",
    "city_district",
    "municipality",
    "county",
)

# Administrative umbrellas, not places people name as "where I am".
_UMBRELLA_WORDS = ("metropolitan", "metropolis", "urban area", "region", "district")


def _is_umbrella(value: str) -> bool:
    low = value.lower()
    return any(word in low for word in _UMBRELLA_WORDS)


def parse_place(data: dict[str, Any]) -> str | None:
    """'Locality, Region' (or country) from a Nominatim reverse response."""
    address = data.get("address") or {}
    candidates = [address[k] for k in _LOCALITY_KEYS if address.get(k)]
    # Prefer a real place over an umbrella label, but fall back if that's all there is.
    locality = next((c for c in candidates if not _is_umbrella(c)), None) or (
        candidates[0] if candidates else None
    )
    region = address.get("state") or address.get("country")
    parts = [p for p in (locality, region) if p]
    if not parts:
        return None
    # Avoid "Singapore, Singapore"
    if len(parts) == 2 and parts[0] == parts[1]:
        parts = parts[:1]
    return ", ".join(parts)


async def reverse_geocode(
    session: aiohttp.ClientSession, lat: float, lon: float
) -> str | None:
    """Return a place name, or None on any failure (never raises)."""
    params = {
        "format": "jsonv2",
        "lat": lat,
        "lon": lon,
        # 12 = town level. Finer levels land on forests/roads at ~1 km precision.
        "zoom": 12,
        "addressdetails": 1,
        "accept-language": "en",
    }
    try:
        async with session.get(
            URL,
            params=params,
            headers={"User-Agent": USER_AGENT},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            resp.raise_for_status()
            return parse_place(await resp.json())
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return None
