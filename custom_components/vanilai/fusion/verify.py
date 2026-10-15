"""Learn which sources are actually accurate here, from real observations.

Every update stores a snapshot of each source's temperature forecast. When an
observation arrives for an hour that snapshots predicted, each source's error
is measured *relative to the other sources* (the shared offset between the
observation site and your location cancels out). A rolling average of that
relative error, per source and lead time, becomes a weight multiplier.

Everything here is plain data (JSON-serialisable) so it can be persisted.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any

from ..providers.base import HourPoint, MemberForecast

BUCKETS = (("0-6", 0, 6), ("6-12", 6, 12), ("12-24", 12, 24), ("24-48", 24, 48))
ALPHA = 0.1  # weight of the newest sample in the rolling average
MIN_SAMPLES = 12  # before a source's skill is trusted
MIN_MEMBERS = 3  # relative error needs this many sources at that hour
EPS = 0.3  # °C; also the measurement noise floor of whole-degree observations
FACTOR_MIN, FACTOR_MAX = 0.25, 2.0
SNAPSHOT_KEEP = timedelta(hours=72)
SNAPSHOT_EVERY = timedelta(minutes=30)
PROBATION = timedelta(hours=48)
PROBATION_WEIGHT = 0.5
UNAVAILABLE_AFTER = timedelta(hours=3)
DEMOTED_BELOW = 0.5
WET_MM = 0.1  # mm/h at which a source counts as "raining" that hour
RAIN_MIN_HOURS = 24  # scored hours before a rain score is shown
RAIN_MIN_EVENTS = 5  # wet hours seen before rain skill means anything
RAIN_HALVE_AT = 1000  # forget old evidence slowly by halving counts
BIAS_CAP = 3.0  # °C; never correct a source by more than this
BIAS_PHASES = (("day", 8, 20), ("night", 20, 32))  # solar hours (night wraps)


def _bucket(lead_h: float) -> str | None:
    for name, lo, hi in BUCKETS:
        if lo <= lead_h < hi:
            return name
    return None


def _ts(dt: datetime) -> int:
    return int(dt.astimezone(timezone.utc).timestamp())


def _phase(ts: int, lon: float) -> str:
    """"day" or "night" at the location, using solar time (no timezone data needed)."""
    solar = (ts / 3600 + lon / 15) % 24
    return "day" if 8 <= solar < 20 else "night"


class SkillTracker:
    def __init__(self, state: dict[str, Any] | None = None) -> None:
        state = state or {}
        self.snapshots: list[dict] = state.get("snapshots", [])
        self.stats: dict[str, dict[str, dict[str, float]]] = state.get("stats", {})
        self.members: dict[str, dict[str, int]] = state.get("members", {})
        self.processed: list[int] = state.get("processed", [])
        self.samples_total: int = state.get("samples_total", 0)
        # name ("fused", "consensus" or a source) -> lead bucket -> error vs the observation
        self.processed_rain: list[int] = state.get("processed_rain", [])
        # {"members": name -> bucket -> {hit, miss, fa, cn}, "fused": bucket -> {n, sq, wet}}
        self.rain: dict[str, dict] = state.get("rain", {"members": {}, "fused": {}})
        self.card: dict[str, dict[str, dict[str, float]]] = state.get("card", {})
        # kind ("rel" | "abs") -> source -> "bucket|phase" -> signed bias (forecast - truth)
        self.bias: dict[str, dict[str, dict[str, dict[str, float]]]] = state.get(
            "bias", {"rel": {}, "abs": {}}
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshots": self.snapshots,
            "stats": self.stats,
            "members": self.members,
            "processed": self.processed[-200:],
            "samples_total": self.samples_total,
            "processed_rain": self.processed_rain[-200:],
            "rain": self.rain,
            "card": self.card,
            "bias": self.bias,
        }

    # -- bookkeeping ----------------------------------------------------------

    def note_members(self, members: list[MemberForecast], now: datetime) -> None:
        """Track first/last seen so newly appearing sources start on probation."""
        t = _ts(now)
        founding = not self.members  # first ever run: everyone is established
        for m in members:
            entry = self.members.setdefault(
                m.member,
                {"first_seen": t - int(PROBATION.total_seconds()) * 2 if founding else t},
            )
            entry["last_seen"] = t

    def record_snapshot(
        self,
        members: list[MemberForecast],
        now: datetime,
        fused: list[HourPoint] | None = None,
    ) -> None:
        t = _ts(now)
        if self.snapshots and t - self.snapshots[-1]["issued"] < SNAPSHOT_EVERY.total_seconds():
            return
        cutoff = t + 48 * 3600
        temps = {
            m.member: {
                _ts(h.time): h.temperature
                for h in m.hours
                if h.temperature is not None and _ts(h.time) <= cutoff
            }
            for m in members
        }
        snap: dict[str, Any] = {"issued": t, "temps": temps}
        if fused:
            snap["fused"] = {
                _ts(h.time): h.temperature
                for h in fused
                if h.temperature is not None and _ts(h.time) <= cutoff
            }
        snap["wet"] = {
            m.member: [
                _ts(h.time) for h in m.hours
                if h.precipitation is not None and h.precipitation >= WET_MM
                and _ts(h.time) <= cutoff
            ]
            for m in members
        }
        if fused:
            snap["pfused"] = {
                _ts(h.time): h.precipitation_probability
                for h in fused
                if h.precipitation_probability is not None and _ts(h.time) <= cutoff
            }
        self.snapshots.append(snap)
        keep_after = t - int(SNAPSHOT_KEEP.total_seconds())
        self.snapshots = [s for s in self.snapshots if s["issued"] >= keep_after]

    # -- learning -------------------------------------------------------------

    def score(
        self,
        observations: dict[int, float],
        now: datetime,
        lon: float = 0.0,
        absolute: bool = False,
    ) -> int:
        """Fold new observations into the per-source error averages.

        `absolute` says the observation is measured at the location itself
        (your own sensor), so signed bias can be learned against it. Otherwise
        (a distant station) only bias relative to the other sources is trusted.
        """
        t_now = _ts(now)
        done = set(self.processed)
        scored = 0
        for obs_t, observed in sorted(observations.items()):
            if obs_t in done or obs_t > t_now:
                continue
            for snap in self.snapshots:
                lead = (obs_t - snap["issued"]) / 3600
                bucket = _bucket(lead)
                if bucket is None:
                    continue
                errors = {
                    member: series[str(obs_t)] - observed
                    if str(obs_t) in series
                    else series[obs_t] - observed
                    for member, series in snap["temps"].items()
                    if str(obs_t) in series or obs_t in series
                }
                if len(errors) < MIN_MEMBERS:
                    continue
                # Median, not mean: one wild source must not shift everyone
                # else's score. It also absorbs the shared site offset.
                common = median(errors.values())
                self._card_add("consensus", bucket, common)
                fused_t = snap.get("fused", {})
                if (f := fused_t.get(str(obs_t), fused_t.get(obs_t))) is not None:
                    self._card_add("fused", bucket, f - observed)
                phase = f"{bucket}|{_phase(obs_t, lon)}"
                for member, err in errors.items():
                    self._card_add(member, bucket, err)
                    self._bias_add("rel", member, phase, err - common)
                    if absolute:
                        self._bias_add("abs", member, phase, err)
                    cell = self.stats.setdefault(member, {}).setdefault(
                        bucket, {"err": abs(err - common), "n": 0}
                    )
                    cell["err"] = (1 - ALPHA) * cell["err"] + ALPHA * abs(err - common)
                    cell["n"] += 1
                    self.samples_total += 1
                    scored += 1
            self.processed.append(obs_t)
        return scored

    def score_rain(self, observed_wet: dict[int, bool], now: datetime) -> int:
        """Score each source's rain/no-rain call (and the fused chance) against reports."""
        t_now = _ts(now)
        done = set(self.processed_rain)
        scored = 0
        for obs_t, wet in sorted(observed_wet.items()):
            if obs_t in done or obs_t > t_now:
                continue
            for snap in self.snapshots:
                bucket = _bucket((obs_t - snap["issued"]) / 3600)
                if bucket is None:
                    continue
                for member, series in snap["temps"].items():
                    if str(obs_t) not in series and obs_t not in series:
                        continue  # this source did not cover that hour
                    called = obs_t in snap.get("wet", {}).get(member, ())
                    cell = self.rain["members"].setdefault(member, {}).setdefault(
                        bucket, {"hit": 0, "miss": 0, "fa": 0, "cn": 0}
                    )
                    cell["hit" if called and wet else "miss" if wet else "fa" if called else "cn"] += 1
                    if sum(cell.values()) > RAIN_HALVE_AT:
                        for k in cell:
                            cell[k] //= 2
                    scored += 1
                pf = snap.get("pfused", {})
                p = pf.get(str(obs_t), pf.get(obs_t))
                if p is not None:
                    cell = self.rain["fused"].setdefault(bucket, {"n": 0, "sq": 0.0, "wet": 0})
                    cell["n"] += 1
                    cell["sq"] += (p / 100 - (1.0 if wet else 0.0)) ** 2
                    cell["wet"] += 1 if wet else 0
                    if cell["n"] > RAIN_HALVE_AT:
                        cell["n"] //= 2
                        cell["sq"] /= 2
                        cell["wet"] //= 2
            self.processed_rain.append(obs_t)
        return scored

    def rain_scorecard(self) -> dict[str, dict[str, Any]]:
        """Per source and lead bucket: POD (wet hours caught), FAR (false alarms), CSI."""
        out: dict[str, dict[str, Any]] = {}
        for member, cells in self.rain["members"].items():
            for bucket, c in cells.items():
                n = sum(c.values())
                if n < RAIN_MIN_HOURS:
                    continue
                called, wet = c["hit"] + c["fa"], c["hit"] + c["miss"]
                union = c["hit"] + c["miss"] + c["fa"]
                out.setdefault(member, {})[bucket] = {
                    "hours": n,
                    "wet_hours": wet,
                    "caught": round(c["hit"] / wet, 2) if wet else None,
                    "false_alarms": round(c["fa"] / called, 2) if called else None,
                    "csi": round(c["hit"] / union, 2) if union else None,
                }
        return out

    def rain_headline(self) -> dict[str, float | None]:
        """Fused rain-chance skill vs always forecasting the usual rain frequency.

        1 = perfect, 0 = no better than climatology, below 0 = worse.
        """
        cells = [c for b, c in self.rain["fused"].items() if b in ("0-6", "6-12", "12-24")]
        n = sum(c["n"] for c in cells)
        wet = sum(c["wet"] for c in cells)
        if n < RAIN_MIN_HOURS or wet < RAIN_MIN_EVENTS:
            return {"rain_skill": None, "brier": None, "wet_share": round(wet / n, 3) if n else None}
        brier = sum(c["sq"] for c in cells) / n
        # Per-bucket climatology so a rainy afternoon doesn't flatter the baseline.
        base = sum(c["n"] * (c["wet"] / c["n"]) * (1 - c["wet"] / c["n"]) for c in cells if c["n"]) / n
        return {
            "rain_skill": round(1 - brier / base, 2) if base > 0 else None,
            "brier": round(brier, 3),
            "wet_share": round(wet / n, 3),
        }

    def _card_add(self, name: str, bucket: str, err: float) -> None:
        cell = self.card.setdefault(name, {}).get(bucket)
        if cell is None:
            self.card[name][bucket] = {"mae": abs(err), "bias": err, "n": 1}
            return
        cell["mae"] = (1 - ALPHA) * cell["mae"] + ALPHA * abs(err)
        cell["bias"] = (1 - ALPHA) * cell["bias"] + ALPHA * err
        cell["n"] += 1

    def _bias_add(self, kind: str, member: str, phase: str, err: float) -> None:
        table = self.bias.setdefault(kind, {}).setdefault(member, {})
        cell = table.get(phase)
        if cell is None:
            table[phase] = {"b": err, "n": 1}
            return
        cell["b"] = (1 - ALPHA) * cell["b"] + ALPHA * err
        cell["n"] += 1

    def correction(
        self, member: str, when: datetime, now: datetime, lon: float = 0.0,
        absolute: bool = False,
    ) -> float:
        """°C to subtract from a source's temperature for this hour (0 = no opinion)."""
        lead = (when - now).total_seconds() / 3600
        bucket = _bucket(lead)
        shrink = 1.0
        if bucket is None:
            if lead < 48:
                return 0.0
            bucket, shrink = "24-48", 0.5  # only 24-48 h evidence exists
        cell = (
            self.bias.get("abs" if absolute else "rel", {})
            .get(member, {})
            .get(f"{bucket}|{_phase(_ts(when), lon)}")
        )
        if not cell or cell["n"] < MIN_SAMPLES:
            return 0.0
        # Few samples -> small correction; it grows toward the full bias.
        corr = cell["b"] * cell["n"] / (cell["n"] + MIN_SAMPLES) * shrink
        return max(-BIAS_CAP, min(BIAS_CAP, corr))

    def corrected(
        self, members: list[MemberForecast], now: datetime, lon: float = 0.0,
        absolute: bool = False,
    ) -> list[MemberForecast]:
        """Copies of `members` with learned bias removed from temperature."""
        out = []
        for m in members:
            hours = [
                replace(h, temperature=round(
                    h.temperature - self.correction(m.member, h.time, now, lon, absolute), 2))
                if h.temperature is not None else h
                for h in m.hours
            ]
            out.append(MemberForecast(member=m.member, family=m.family, hours=hours))
        return out

    def scorecard(self) -> dict[str, dict[str, Any]]:
        """Per name and lead bucket: typical error and bias vs observations (°C)."""
        return {
            name: {
                b: {"mae": round(c["mae"], 2), "bias": round(c["bias"], 2), "n": c["n"]}
                for b, c in cells.items()
            }
            for name, cells in self.card.items()
        }

    def headline(self) -> dict[str, float | None]:
        """Fused vs equal-vote consensus error over 0-24 h, for one-glance checks."""
        def mae(name):
            cells = [c for b, c in self.card.get(name, {}).items()
                     if b in ("0-6", "6-12", "12-24") and c["n"] >= MIN_SAMPLES]
            n = sum(c["n"] for c in cells)
            return round(sum(c["mae"] * c["n"] for c in cells) / n, 2) if n else None
        return {"fused_mae": mae("fused"), "consensus_mae": mae("consensus")}

    # -- using what was learned ------------------------------------------------

    def _factor_in(self, member: str, bucket: str) -> float | None:
        cell = self.stats.get(member, {}).get(bucket)
        if not cell or cell["n"] < MIN_SAMPLES:
            return None
        peers = [
            s[bucket]["err"]
            for s in self.stats.values()
            if bucket in s and s[bucket]["n"] >= MIN_SAMPLES
        ]
        if len(peers) < MIN_MEMBERS:
            return None
        ratio = (median(peers) + EPS) / (cell["err"] + EPS)
        return max(FACTOR_MIN, min(FACTOR_MAX, ratio**1.5))

    def factor(self, member: str, lead_h: float) -> float:
        """Learned weight multiplier (1.0 = no opinion yet)."""
        bucket = _bucket(lead_h)
        if bucket is None:
            # Beyond 48 h we only have 24-48 h evidence: trust it less.
            f = self._factor_in(member, "24-48") if lead_h >= 48 else None
            return 1.0 if f is None else f**0.5
        f = self._factor_in(member, bucket)
        return 1.0 if f is None else f

    def status(self, member: str, now: datetime) -> str:
        info = self.members.get(member)
        if not info:
            return "unknown"
        t = _ts(now)
        if t - info.get("last_seen", 0) > UNAVAILABLE_AFTER.total_seconds():
            return "unavailable"
        factors = [
            f for b, _, _ in BUCKETS
            if (f := self._factor_in(member, b)) is not None
        ]
        if factors and min(factors) <= DEMOTED_BELOW:
            return "demoted"
        verified = any(
            c["n"] >= MIN_SAMPLES for c in self.stats.get(member, {}).values()
        )
        young = t - info.get("first_seen", 0) < PROBATION.total_seconds()
        return "probation" if young and not verified else "active"

    def weight(self, member: str, lead_h: float, now: datetime | None = None) -> float:
        """What the fuser multiplies a source's vote by."""
        now = now or datetime.now(timezone.utc)
        w = self.factor(member, lead_h)
        if self.status(member, now) == "probation":
            w *= PROBATION_WEIGHT
        return w

    def summary(self, now: datetime) -> dict[str, dict[str, Any]]:
        """Human-friendly per-source report for diagnostics."""
        out: dict[str, dict[str, Any]] = {}
        for member in self.members:
            cells = self.stats.get(member, {})
            n = sum(c["n"] for c in cells.values())
            err = [c["err"] for c in cells.values() if c["n"] >= MIN_SAMPLES]
            out[member] = {
                "status": self.status(member, now),
                "trust": round(self.factor(member, 3.0), 2),
                "typical_error_c": round(sum(err) / len(err), 2) if err else None,
                "samples": n,
            }
        return out
