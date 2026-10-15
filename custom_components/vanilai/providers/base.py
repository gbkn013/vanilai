"""Provider base class and normalized forecast schema."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime

from aiohttp import ClientSession


@dataclass
class HourPoint:
    """One normalized hourly datapoint (UTC time)."""

    time: datetime
    temperature: float | None = None  # °C
    humidity: float | None = None  # %
    pressure: float | None = None  # hPa
    wind_speed: float | None = None  # m/s
    wind_bearing: float | None = None  # degrees
    precipitation: float | None = None  # mm/h
    precipitation_probability: float | None = None  # 0-100 %
    cloud_coverage: float | None = None  # %
    condition: str | None = None  # Home Assistant condition string


@dataclass
class MemberForecast:
    """Forecast from one independent member (a provider or one model)."""

    member: str  # e.g. "open_meteo:ecmwf_ifs025"
    family: str  # underlying model family, for independence weighting
    hours: list[HourPoint] = field(default_factory=list)


class Provider(ABC):
    """A weather source that may yield several members."""

    key: str

    def __init__(self, session: ClientSession) -> None:
        self._session = session

    @abstractmethod
    async def fetch(self, lat: float, lon: float) -> list[MemberForecast]:
        """Fetch and normalize forecasts for the (already rounded) location."""
