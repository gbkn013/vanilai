"""Normalization helpers shared by providers."""
from __future__ import annotations

# WMO weather interpretation codes -> Home Assistant condition strings.
_WMO_TO_HA: dict[int, str] = {
    0: "sunny",
    1: "sunny",
    2: "partlycloudy",
    3: "cloudy",
    45: "fog",
    48: "fog",
    51: "rainy",
    53: "rainy",
    55: "rainy",
    56: "snowy-rainy",
    57: "snowy-rainy",
    61: "rainy",
    63: "rainy",
    65: "pouring",
    66: "snowy-rainy",
    67: "snowy-rainy",
    71: "snowy",
    73: "snowy",
    75: "snowy",
    77: "snowy",
    80: "rainy",
    81: "rainy",
    82: "pouring",
    85: "snowy",
    86: "snowy",
    95: "lightning-rainy",
    96: "hail",
    99: "hail",
}


def wmo_to_condition(code: int | float | None, is_day: bool = True) -> str | None:
    """Map a WMO code to an HA condition, using clear-night after dark."""
    if code is None:
        return None
    condition = _WMO_TO_HA.get(int(code))
    if condition == "sunny" and not is_day:
        return "clear-night"
    return condition


# --- MET Norway symbol codes -------------------------------------------------

_NIGHTISH = ("_night", "_polartwilight")


def metno_symbol_to_condition(symbol: str | None) -> str | None:
    """MET Norway symbol_code (e.g. 'lightrainshowers_day') -> HA condition."""
    if not symbol:
        return None
    night = symbol.endswith(_NIGHTISH)
    base = symbol.rsplit("_", 1)[0] if symbol.endswith(("_day",) + _NIGHTISH) else symbol
    if "thunder" in base:
        return "lightning-rainy" if ("rain" in base or "sleet" in base) else "lightning"
    if base in ("clearsky", "fair"):
        return "clear-night" if night else "sunny"
    if base == "partlycloudy":
        return "partlycloudy"
    if base == "cloudy":
        return "cloudy"
    if base == "fog":
        return "fog"
    if "sleet" in base:
        return "snowy-rainy"
    if "snow" in base:
        return "snowy"
    if base.startswith("heavyrain"):
        return "pouring"
    if "rain" in base:
        return "rainy"
    return None


# --- NWS text forecasts ------------------------------------------------------

_COMPASS = {
    "N": 0, "NNE": 22.5, "NE": 45, "ENE": 67.5, "E": 90, "ESE": 112.5,
    "SE": 135, "SSE": 157.5, "S": 180, "SSW": 202.5, "SW": 225, "WSW": 247.5,
    "W": 270, "WNW": 292.5, "NW": 315, "NNW": 337.5,
}


def compass_to_degrees(text: str | None) -> float | None:
    return _COMPASS.get((text or "").strip().upper())


def nws_text_to_condition(text: str | None, is_day: bool = True) -> str | None:
    """NWS shortForecast ('Chance Showers And Thunderstorms') -> HA condition."""
    if not text:
        return None
    t = text.lower()
    if "thunder" in t:
        return "lightning-rainy" if any(w in t for w in ("shower", "rain")) else "lightning"
    if "hail" in t:
        return "hail"
    if "freezing" in t or "sleet" in t or "wintry" in t:
        return "snowy-rainy"
    if "snow" in t or "flurries" in t:
        return "snowy"
    if "heavy rain" in t:
        return "pouring"
    if any(w in t for w in ("rain", "shower", "drizzle")):
        return "rainy"
    if "fog" in t or "haze" in t or "smoke" in t:
        return "fog"
    if "partly" in t:
        return "partlycloudy"
    if "cloudy" in t or "overcast" in t:
        return "cloudy"
    if "sunny" in t or "clear" in t:
        return "sunny" if is_day else "clear-night"
    return None
