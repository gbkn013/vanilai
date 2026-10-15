"""Pure-logic tests: normalization, Open-Meteo parsing, placeholder fusion."""
from datetime import datetime, timezone

from custom_components.vanilai.fusion.aggregate import (
    fuse,
    weighted_circular_mean,
    weighted_median,
)
from custom_components.vanilai.normalize import wmo_to_condition
from custom_components.vanilai.providers.base import HourPoint, MemberForecast
from custom_components.vanilai.providers.open_meteo import parse_response

T0 = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)


def test_wmo_mapping():
    assert wmo_to_condition(0) == "sunny"
    assert wmo_to_condition(0, is_day=False) == "clear-night"
    assert wmo_to_condition(65) == "pouring"
    assert wmo_to_condition(96) == "hail"
    assert wmo_to_condition(None) is None
    assert wmo_to_condition(12345) is None


def test_circular_mean_wraps_north():
    assert round(weighted_circular_mean([(350, 1), (10, 1)]), 1) in (0.0, 360.0)
    assert abs(weighted_circular_mean([(90, 1), (90, 1)]) - 90) < 1e-6


def _member(name, temp, bearing, condition):
    return MemberForecast(
        member=name,
        family=name,
        hours=[
            HourPoint(
                time=T0,
                temperature=temp,
                wind_bearing=bearing,
                condition=condition,
            )
        ],
    )


def test_fuse_median_and_vote():
    out = fuse(
        [
            _member("a", 10.0, 350, "rainy"),
            _member("b", 12.0, 10, "rainy"),
            _member("c", 30.0, 5, "sunny"),  # outlier
        ]
    )
    assert len(out) == 1
    assert out[0].temperature == 12.0  # median shrugs off the outlier
    assert out[0].condition == "rainy"
    assert out[0].wind_bearing < 20 or out[0].wind_bearing > 340


def _tmember(name, family, temp):
    return MemberForecast(
        member=name, family=family, hours=[HourPoint(time=T0, temperature=temp)]
    )


def test_confidence_drops_with_spread_and_lead():
    from custom_components.vanilai.fusion.confidence import (
        confidence,
        temperature_stats,
    )

    agree = temperature_stats(
        [_tmember(c, c, 10.0 + i * 0.1) for i, c in enumerate("abcd")]
    )[T0]
    split = temperature_stats(
        [_tmember(c, c, 10.0 + i * 4) for i, c in enumerate("abcd")]
    )[T0]
    assert confidence(agree, 0) > 95
    assert confidence(split, 0) < confidence(agree, 0)
    assert confidence(agree, 168) < confidence(agree, 0)
    assert confidence(None, 0) == 0


def test_confidence_counts_independent_families_not_members():
    from custom_components.vanilai.fusion.confidence import (
        confidence,
        temperature_stats,
    )

    # Four members, all the same model family -> only one independent source.
    clones = temperature_stats([_tmember(f"m{i}", "gfs", 10.0) for i in range(4)])[T0]
    diverse = temperature_stats([_tmember(f"m{i}", f"f{i}", 10.0) for i in range(4)])[T0]
    assert clones.families == 1 and diverse.families == 4
    assert confidence(clones, 0) < confidence(diverse, 0)


def test_parse_place():
    pytest = __import__("pytest")
    aiohttp = pytest.importorskip("aiohttp")  # geocode imports aiohttp
    from custom_components.vanilai.geocode import parse_place

    assert parse_place(
        {"address": {"city": "Sydney", "state": "New South Wales", "country": "Australia"}}
    ) == "Sydney, New South Wales"
    assert parse_place({"address": {"village": "Tiny", "country": "Norway"}}) == "Tiny, Norway"
    assert parse_place({"address": {"city": "Singapore", "country": "Singapore"}}) == "Singapore"
    assert parse_place({"address": {"state": "Bavaria"}}) == "Bavaria"
    assert parse_place({}) is None
    assert aiohttp
    # The real town beats a wide administrative "city" label.
    assert parse_place(
        {
            "address": {
                "town": "Chengalpattu",
                "city": "Chennai Metropolitan Area",
                "state": "Tamil Nadu",
            }
        }
    ) == "Chengalpattu, Tamil Nadu"
    # City beats a finer district for big cities.
    assert parse_place(
        {"address": {"city": "Paris", "suburb": "4th Arrondissement", "state": "Ile-de-France"}}
    ) == "Paris, Ile-de-France"
    # Umbrella label is used only when nothing better exists.
    assert parse_place(
        {"address": {"city": "Chennai Metropolitan Area", "state": "Tamil Nadu"}}
    ) == "Chennai Metropolitan Area, Tamil Nadu"
    assert parse_place(
        {"address": {"city": "Chennai Metropolitan Area", "suburb": "Adyar", "state": "Tamil Nadu"}}
    ) == "Adyar, Tamil Nadu"


def _hour(h, temp, prob=0, mm=0.0, cond="cloudy"):
    from datetime import timedelta

    return HourPoint(
        time=T0 + timedelta(hours=h),
        temperature=temp,
        precipitation_probability=prob,
        precipitation=mm,
        condition=cond,
    )


def test_summary_rain_later():
    from datetime import timezone as tz

    from custom_components.vanilai.summary import summarize

    hours = [_hour(i, 20 + i, prob=10 if i < 6 else 70, cond="rainy" if i >= 6 else "cloudy") for i in range(12)]
    out = summarize(hours, 80, tz.utc)
    assert out["label"] == "High"
    assert "Rain likely from 18:00" in out["text"]
    assert "up to 70%" in out["text"]
    assert "20–31°C" in out["text"]


def test_summary_dry_and_low_confidence():
    from datetime import timezone as tz

    from custom_components.vanilai.summary import summarize

    hours = [_hour(i, 25, cond="sunny") for i in range(6)]
    out = summarize(hours, 30, tz.utc)
    assert "No rain expected." in out["text"]
    assert out["label"] == "Low"
    assert "caution" in out["text"]
    assert out["text"].startswith("Sunny, 25°C")


def test_summary_raining_now_then_clearing():
    from datetime import timezone as tz

    from custom_components.vanilai.summary import summarize

    hours = [_hour(i, 18, prob=80 if i < 3 else 0) for i in range(8)]
    assert "Raining now, easing after 15:00." in summarize(hours, 60, tz.utc)["text"]


def test_parse_open_meteo_skips_missing_model():
    data = {
        "hourly": {
            "time": ["2026-10-14T12:00", "2026-10-14T13:00"],
            "temperature_2m_ecmwf_ifs025": [11.0, 12.0],
            "weather_code_ecmwf_ifs025": [0, 61],
            "is_day_ecmwf_ifs025": [1, 1],
            "precipitation_probability_ecmwf_ifs025": [None, 40],
            # other models absent -> must be skipped
        }
    }
    members = parse_response(data)
    assert [m.member for m in members] == ["open_meteo:ecmwf_ifs025"]
    hours = members[0].hours
    assert hours[0].temperature == 11.0
    assert hours[0].condition == "sunny"
    assert hours[1].condition == "rainy"
    assert hours[0].precipitation_probability is None
    assert hours[1].precipitation_probability == 40
    assert hours[0].time.tzinfo is not None


# ---- family weighting, duplicates, regional coverage ------------------------


def test_weighted_median_tie_returns_midpoint():
    assert weighted_median([(10.0, 1), (20.0, 1)]) == 15.0
    assert weighted_median([(10.0, 1), (20.0, 1), (30.0, 1)]) == 20.0
    assert weighted_median([(10.0, 5), (20.0, 1)]) == 10.0


def test_fuse_gives_each_family_one_vote():
    # Three correlated GFS-family models at 10 °C vs a single ECMWF at 20 °C.
    # Each family counts once, so the result sits between them (15), not at 10.
    members = [_member(f"gfs{i}", 10.0, 0, "cloudy") for i in range(3)]
    for m in members:
        m.family = "gfs"
    members.append(_member("ecmwf", 20.0, 0, "sunny"))
    out = fuse(members)
    assert out[0].temperature == 15.0


def test_condition_tie_breaks_toward_severe():
    out = fuse([_member("a", 10, 0, "sunny"), _member("b", 10, 0, "rainy")])
    assert out[0].condition == "rainy"


def test_drop_duplicates_removes_copies_only():
    from custom_components.vanilai.fusion.dedupe import drop_duplicates

    def series(name, base):
        return MemberForecast(
            member=name,
            family=name,
            hours=[
                HourPoint(time=T0.replace(hour=0) + __import__("datetime").timedelta(hours=i),
                          temperature=base + i * 0.1, precipitation=0.0)
                for i in range(48)
            ],
        )

    kept, dropped = drop_duplicates(
        [series("a", 20.0), series("copy_of_a", 20.0), series("b", 21.0)]
    )
    assert [m.member for m in kept] == ["a", "b"]
    assert len(dropped) == 1 and "copy_of_a" in dropped[0] and "a)" in dropped[0]


def test_drop_duplicates_ignores_sparse_series():
    from custom_components.vanilai.fusion.dedupe import drop_duplicates

    sparse = [_tmember("x", "x", 10.0), _tmember("y", "y", 10.0)]
    kept, dropped = drop_duplicates(sparse)  # only 1 point each: can't judge
    assert len(kept) == 2 and not dropped


def test_regional_models_follow_location():
    from custom_components.vanilai.providers.open_meteo import models_for

    ids = lambda lat, lon: {m.id for m in models_for(lat, lon)[1]}
    assert ids(12.72, 80.03) == set()  # southern India: no regional models
    assert "metno_seamless" in ids(59.91, 10.75)  # Oslo
    assert "knmi_seamless" in ids(52.37, 4.90)  # Amsterdam
    assert "meteoswiss_icon_seamless" in ids(47.37, 8.54)  # Zurich
    assert "italia_meteo_arpae_icon_2i" in ids(41.90, 12.50)  # Rome
    assert "dmi_seamless" in ids(55.68, 12.57)  # Copenhagen
    assert models_for(12.72, 80.03)[0]  # global set is always present


def test_parse_single_model_response_uses_bare_keys():
    from custom_components.vanilai.providers.open_meteo import REGIONAL_MODELS

    spec = REGIONAL_MODELS[0]
    data = {
        "hourly": {
            "time": ["2026-10-14T12:00"],
            "temperature_2m": [9.0],
            "weather_code": [3],
            "is_day": [1],
        }
    }
    members = parse_response(data, [spec])
    assert len(members) == 1
    assert members[0].member == f"open_meteo:{spec.id}"
    assert members[0].family == spec.family
    assert members[0].hours[0].temperature == 9.0
    assert members[0].hours[0].condition == "cloudy"


# ---- rain consensus, rain-aware confidence, skill weighting -----------------


def _wet_member(name, family, mm, prob=None):
    return MemberForecast(
        member=name,
        family=family,
        hours=[HourPoint(time=T0, temperature=20.0, precipitation=mm,
                         precipitation_probability=prob)],
    )


def test_rain_probability_falls_back_to_family_consensus():
    # No model publishes a probability; 2 of 4 families expect rain -> 50 %.
    members = [
        _wet_member("a", "a", 1.0),
        _wet_member("b", "b", 0.5),
        _wet_member("c", "c", 0.0),
        _wet_member("d", "d", 0.0),
    ]
    assert fuse(members)[0].precipitation_probability == 50.0


def test_rain_probability_uses_published_values_when_enough_families():
    members = [_wet_member(n, n, 0.0, prob=p) for n, p in (("a", 20), ("b", 40), ("c", 60))]
    assert fuse(members)[0].precipitation_probability == 40.0


def test_rain_probability_ignores_too_few_published_values():
    # Only 2 families publish a probability (both 90 %), but 3 of 4 say dry.
    members = [
        _wet_member("a", "a", 0.0, prob=90),
        _wet_member("b", "b", 0.0, prob=90),
        _wet_member("c", "c", 0.0),
        _wet_member("d", "d", 2.0),
    ]
    assert fuse(members)[0].precipitation_probability == 25.0


def test_confidence_penalises_rain_disagreement():
    from custom_components.vanilai.fusion.confidence import (
        confidence,
        temperature_stats,
    )

    def stats(mms):
        return temperature_stats([_wet_member(f"m{i}", f"f{i}", mm) for i, mm in enumerate(mms)])[T0]

    agree_dry = stats([0, 0, 0, 0])
    agree_wet = stats([1, 1, 1, 1])
    split = stats([1, 1, 0, 0])
    assert confidence(agree_dry, 0) == confidence(agree_wet, 0)
    assert confidence(split, 0) < confidence(agree_dry, 0)
    assert split.rain_agreement == 0.0
    assert confidence(split, 0) >= 0.5 * confidence(agree_dry, 0) - 1  # floor at half


def test_skill_scales_member_influence():
    from datetime import timedelta

    members = [_member("good", 10.0, 0, "cloudy"), _member("bad", 30.0, 0, "cloudy")]
    skill = lambda member, lead: 3.0 if member == "good" else 1.0
    out = fuse(members, now=T0 - timedelta(hours=1), skill=skill)
    assert out[0].temperature == 10.0  # trusted member now dominates the median
    assert fuse(members)[0].temperature == 20.0  # equal weights: midpoint


# ---- providers against recorded real responses ------------------------------

import json
from pathlib import Path

FIX = Path(__file__).parent / "fixtures"


def test_metno_parses_recorded_response():
    from custom_components.vanilai.providers.met_no import family_for, parse_response as pr

    data = json.loads((FIX / "metno_oslo.json").read_text())
    members = pr(data, family_for(59.91, 10.75))
    assert len(members) == 1 and members[0].family == "metno"
    hours = members[0].hours
    assert 40 <= len(hours) <= 72  # hourly part only; 6-hourly tail is skipped
    gaps = {(b.time - a.time).total_seconds() / 3600 for a, b in zip(hours, hours[1:])}
    assert gaps == {1.0}
    h = hours[0]
    assert -50 < h.temperature < 50 and 0 <= h.humidity <= 100
    assert 900 < h.pressure < 1100 and h.wind_speed >= 0
    assert h.precipitation is not None and h.precipitation_probability is not None
    assert h.condition in {"sunny", "clear-night", "partlycloudy", "cloudy", "fog",
                           "rainy", "pouring", "snowy", "snowy-rainy", "lightning",
                           "lightning-rainy"}
    assert family_for(12.72, 80.03) == "ecmwf"  # outside the Nordics


def test_metno_symbol_mapping():
    from custom_components.vanilai.normalize import metno_symbol_to_condition as m

    assert m("clearsky_day") == "sunny" and m("clearsky_night") == "clear-night"
    assert m("fair_polartwilight") == "clear-night"
    assert m("lightrainshowers_day") == "rainy" and m("heavyrain") == "pouring"
    assert m("rainandthunder") == "lightning-rainy"
    assert m("heavysnowshowers_night") == "snowy" and m("sleet") == "snowy-rainy"
    assert m("fog") == "fog" and m(None) is None and m("nonsense") is None


def test_nws_parses_recorded_response():
    from custom_components.vanilai.providers.nws import parse_response as pr

    data = json.loads((FIX / "nws_hourly_dc.json").read_text())
    members = pr(data)
    assert len(members) == 1 and members[0].family == "nws"
    hours = members[0].hours
    assert len(hours) == 72
    h = hours[0]
    assert 0 < h.temperature < 40
    assert h.wind_speed is not None and h.wind_speed < 60  # m/s, not km/h
    assert h.condition is not None
    assert all(x.time.tzinfo is not None for x in hours)
    assert h.precipitation is None  # NWS hourly has no amount


def test_nws_helpers():
    from custom_components.vanilai.normalize import nws_text_to_condition as c
    from custom_components.vanilai.providers.nws import covers, parse_wind_ms

    assert parse_wind_ms("7 km/h") == round(7 / 3.6, 2)
    assert parse_wind_ms("5 to 10 km/h") == round(7.5 / 3.6, 2)
    assert parse_wind_ms("calm") is None
    assert c("Chance Showers And Thunderstorms") == "lightning-rainy"
    assert c("Mostly Sunny") == "sunny" and c("Clear", is_day=False) == "clear-night"
    assert c("Partly Cloudy") == "partlycloudy" and c("Heavy Rain") == "pouring"
    assert covers(38.89, -77.03) and not covers(12.72, 80.03)


def test_metar_picks_nearest_active_station():
    from custom_components.vanilai.observations import parse_metar

    reports = json.loads((FIX / "metar_chennai.json").read_text())
    obs = parse_metar(reports, 12.72, 80.03)
    assert obs is not None
    assert obs.station == "VOMM"  # Chennai Intl, nearest of four stations
    assert 25 < obs.distance_km < 45
    assert len(obs.temps) >= 12
    assert all(t % 3600 == 0 for t in obs.temps)  # snapped to the hour
    assert all(15 < v < 45 for v in obs.temps.values())
    assert parse_metar(reports, 59.91, 10.75) is None  # nothing within 100 km
    assert parse_metar([], 12.72, 80.03) is None


# ---- accuracy tracker (learning from observations) ---------------------------

def _simulate(days=4, site_offset=3.0, seed=1):
    """Hourly updates; A,B,C accurate, D wrong by a swinging +-2.5 degrees."""
    import random
    from datetime import timedelta

    from custom_components.vanilai.fusion.verify import SkillTracker

    rng = random.Random(seed)
    start = datetime(2026, 10, 1, 0, tzinfo=timezone.utc)
    truth = lambda t: 20 + 6 * __import__("math").sin(t.timestamp() / 3600 / 24 * 6.2832)
    tracker = SkillTracker()
    for step in range(days * 24):
        now = start + timedelta(hours=step)
        members = []
        for name in "ABCD":
            hours = []
            for h in range(1, 49):
                t = now + timedelta(hours=h)
                err = rng.gauss(0, 0.3) if name != "D" else rng.choice([-2.5, 2.5])
                hours.append(HourPoint(time=t, temperature=truth(t) + err))
            members.append(MemberForecast(member=name, family=name, hours=hours))
        tracker.note_members(members, now)
        obs_t = int(now.timestamp()) // 3600 * 3600
        # The observation site is warmer than the forecast point by site_offset.
        tracker.score({obs_t: truth(now) + site_offset}, now)
        tracker.record_snapshot(members, now)
    return tracker, start + timedelta(hours=days * 24)


def test_tracker_learns_who_is_accurate_and_ignores_site_offset():
    tracker, now = _simulate()
    good = [tracker.factor(m, 3.0) for m in "ABC"]
    bad = tracker.factor("D", 3.0)
    assert min(good) > 0.85, good         # accurate models keep (about) full weight
    assert bad <= 0.5, bad                # the biased one is demoted...
    assert bad < min(good) / 2
    assert tracker.status("D", now) == "demoted"
    assert tracker.status("A", now) == "active"
    assert 0.25 <= bad <= 2.0 and max(good) <= 2.0
    assert abs(max(good) - min(good)) < 0.3  # and are not told apart by noise


def test_tracker_survives_persistence_round_trip():
    from custom_components.vanilai.fusion.verify import SkillTracker

    tracker, now = _simulate(days=3)
    reloaded = SkillTracker(json.loads(json.dumps(tracker.to_dict())))
    for m in "ABCD":
        for lead in (2.0, 9.0, 18.0, 30.0, 60.0):
            assert reloaded.factor(m, lead) == tracker.factor(m, lead)


def test_tracker_is_neutral_without_enough_evidence():
    from custom_components.vanilai.fusion.verify import SkillTracker

    t = SkillTracker()
    assert t.factor("anything", 3.0) == 1.0
    tracker, now = _simulate(days=1)
    # one day gives <12 samples in the longer buckets -> still neutral there
    assert tracker.factor("D", 40.0) == 1.0


def test_new_source_starts_on_probation_but_founders_do_not():
    from datetime import timedelta

    tracker, now = _simulate(days=2)
    newcomer = MemberForecast(member="NEW", family="NEW", hours=[])
    tracker.note_members([newcomer], now)
    assert tracker.status("NEW", now) == "probation"
    assert tracker.weight("NEW", 3.0, now) == 0.5 * tracker.factor("NEW", 3.0)
    assert tracker.status("A", now) == "active"
    # not reporting for hours -> unavailable
    assert tracker.status("A", now + timedelta(hours=5)) == "unavailable"


def test_tracker_ignores_future_and_repeated_observations():
    from custom_components.vanilai.fusion.verify import SkillTracker

    tracker, now = _simulate(days=1)
    before = tracker.samples_total
    future = int(now.timestamp()) + 7200
    assert tracker.score({future: 25.0}, now) == 0
    done = tracker.processed[-1]
    assert tracker.score({done: 25.0}, now) == 0
    assert tracker.samples_total == before


# ---- scorecard and bias correction -------------------------------------------

BIASES = {"A": 2.0, "B": -1.0, "C": 0.0, "D": 1.0}


def _simulate_bias(days, absolute, site_offset=0.0, seed=3):
    import math
    import random
    from datetime import timedelta

    from custom_components.vanilai.fusion.aggregate import fuse
    from custom_components.vanilai.fusion.verify import SkillTracker

    rng = random.Random(seed)
    start = datetime(2026, 10, 1, 0, tzinfo=timezone.utc)
    truth = lambda t: 20 + 6 * math.sin(t.timestamp() / 3600 / 24 * 6.2832)
    tracker = SkillTracker()
    for step in range(days * 24):
        now = start + timedelta(hours=step)
        members = [
            MemberForecast(member=n, family=n, hours=[
                HourPoint(time=now + timedelta(hours=h),
                          temperature=truth(now + timedelta(hours=h)) + b + rng.gauss(0, 0.3))
                for h in range(1, 49)
            ])
            for n, b in BIASES.items()
        ]
        tracker.note_members(members, now)
        obs_t = int(now.timestamp()) // 3600 * 3600
        tracker.score({obs_t: truth(now) + site_offset}, now, lon=0.0, absolute=absolute)
        corrected = tracker.corrected(members, now, 0.0, absolute)
        fused = fuse(corrected, now=now, skill=lambda m, lead: tracker.weight(m, lead, now))
        tracker.record_snapshot(members, now, fused)
    return tracker, now, members


def test_scorecard_shows_fused_beating_equal_vote_after_bias_correction():
    tracker, _, _ = _simulate_bias(8, absolute=True)
    head = tracker.headline()
    assert head["fused_mae"] is not None and head["consensus_mae"] is not None
    assert head["fused_mae"] < head["consensus_mae"], head
    card = tracker.scorecard()
    assert {"fused", "consensus", "A", "B", "C", "D"} <= set(card)
    assert card["A"]["0-6"]["bias"] > 1.0 > abs(card["C"]["0-6"]["bias"])  # A runs warm


def test_absolute_bias_is_learned_and_removed():
    tracker, now, members = _simulate_bias(8, absolute=True)
    from datetime import timedelta
    when = now + timedelta(hours=3)
    assert tracker.correction("A", when, now, 0.0, absolute=True) > 1.0
    assert tracker.correction("B", when, now, 0.0, absolute=True) < -0.5
    fixed = tracker.corrected(members, now, 0.0, absolute=True)
    a_raw = members[0].hours[2].temperature
    a_fixed = fixed[0].hours[2].temperature
    assert a_fixed < a_raw - 1.0


def test_distant_station_never_teaches_a_shared_offset():
    # The station reads 4 degrees warmer than home. With relative bias, the
    # median source gets ~no correction, so the offset is not baked in.
    tracker, now, _ = _simulate_bias(8, absolute=False, site_offset=4.0)
    from datetime import timedelta
    when = now + timedelta(hours=3)
    corr = [tracker.correction(n, when, now, 0.0) for n in "ABCD"]
    assert abs(sum(corr) / 4) < 0.7, corr
    # absolute mode was never fed, so it must stay silent
    assert tracker.correction("A", when, now, 0.0, absolute=True) == 0.0


def test_correction_waits_for_enough_samples_and_is_capped():
    from datetime import timedelta

    from custom_components.vanilai.fusion.verify import BIAS_CAP, MIN_SAMPLES, SkillTracker
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    t = SkillTracker()
    when = now + timedelta(hours=2)
    for _ in range(MIN_SAMPLES - 1):
        t._bias_add("abs", "X", "0-6|night", 9.0)
    assert t.correction("X", when, now, 0.0, absolute=True) == 0.0
    for _ in range(200):
        t._bias_add("abs", "X", "0-6|night", 9.0)
    assert t.correction("X", when, now, 0.0, absolute=True) == BIAS_CAP


def test_scorecard_survives_persistence():
    import json

    from custom_components.vanilai.fusion.verify import SkillTracker
    tracker, _, _ = _simulate_bias(3, absolute=True)
    again = SkillTracker(json.loads(json.dumps(tracker.to_dict())))
    assert again.scorecard() == tracker.scorecard()
    assert again.bias == tracker.bias


# ---- absolute error and rain scoring -------------------------------------------

def test_metar_rain_codes():
    from custom_components.vanilai.observations import reports_rain
    assert reports_rain("TSRA") and reports_rain("-RA BR") and reports_rain("+SHRA")
    assert reports_rain("DZ") and reports_rain("SN")
    assert not reports_rain("HZ") and not reports_rain("BR") and not reports_rain("FG")
    assert not reports_rain("VCSH") and not reports_rain("VCTS")  # nearby, not here
    assert not reports_rain(None) and not reports_rain("")


def test_station_elevation_adjustment():
    from custom_components.vanilai.observations import adjust_to_elevation
    temps = {0: 20.0}
    assert adjust_to_elevation(temps, 500, 0)[0] == 23.25    # station higher: you are warmer
    assert adjust_to_elevation(temps, 0, 500)[0] == 16.75    # you are higher: colder
    assert adjust_to_elevation(temps, 5000, 0)[0] == 25.0    # capped at 5 degrees
    assert adjust_to_elevation(temps, None, 10) == temps     # unknown elevation: unchanged


def test_metar_fixture_has_hourly_rain_flags_and_elevation():
    from custom_components.vanilai.observations import parse_metar
    reports = json.loads((FIX / "metar_chennai.json").read_text())
    obs = parse_metar(reports, 12.72, 80.03)
    assert obs.elevation_m is not None
    assert obs.wet and set(obs.wet) >= set(obs.temps) - {None}
    assert any(obs.wet.values()) or not any("RA" in (r.get("wxString") or "") for r in reports)


def _simulate_rain(hours=120, seed=5):
    """Source GOOD calls rain right; BAD calls it at random. The fused chance follows GOOD."""
    import random
    from datetime import timedelta

    from custom_components.vanilai.fusion.verify import SkillTracker

    rng = random.Random(seed)
    start = datetime(2026, 10, 1, 0, tzinfo=timezone.utc)
    wet_at = lambda t: (int(t.timestamp()) // 3600) % 5 == 0   # rain every fifth hour
    tracker = SkillTracker()
    for step in range(hours):
        now = start + timedelta(hours=step)
        members = []
        fused = []
        for name in ("GOOD", "BAD"):
            pts = []
            for h in range(1, 25):
                t = now + timedelta(hours=h)
                rain = wet_at(t) if name == "GOOD" else rng.random() < 0.5
                pts.append(HourPoint(time=t, temperature=20.0, precipitation=1.0 if rain else 0.0))
            members.append(MemberForecast(member=name, family=name, hours=pts))
        for h in range(1, 25):
            t = now + timedelta(hours=h)
            fused.append(HourPoint(time=t, temperature=20.0,
                                   precipitation_probability=90.0 if wet_at(t) else 5.0))
        tracker.note_members(members, now)
        obs_t = int(now.timestamp()) // 3600 * 3600
        tracker.score_rain({obs_t: wet_at(now)}, now)
        tracker.record_snapshot(members, now, fused)
    return tracker


def test_rain_scoring_ranks_sources_and_fused_skill():
    t = _simulate_rain()
    card = t.rain_scorecard()
    good, bad = card["GOOD"]["0-6"], card["BAD"]["0-6"]
    assert good["caught"] == 1.0 and good["false_alarms"] == 0.0 and good["csi"] == 1.0
    assert bad["csi"] < 0.5 and bad["false_alarms"] > 0.5
    head = t.rain_headline()
    assert head["rain_skill"] is not None and head["rain_skill"] > 0.7, head
    assert 0.1 < head["wet_share"] < 0.3


def test_rain_skill_is_none_until_enough_rain_has_been_seen():
    from custom_components.vanilai.fusion.verify import SkillTracker
    t = SkillTracker()
    assert t.rain_headline()["rain_skill"] is None
    assert t.rain_scorecard() == {}


def test_rain_scoring_survives_persistence_and_never_double_counts():
    from datetime import timedelta

    from custom_components.vanilai.fusion.verify import SkillTracker
    t = _simulate_rain(60)
    again = SkillTracker(json.loads(json.dumps(t.to_dict())))
    assert again.rain_scorecard() == t.rain_scorecard()
    now = datetime(2026, 10, 1, 0, tzinfo=timezone.utc) + timedelta(hours=59)
    done = t.processed_rain[-1]
    assert again.score_rain({done: True}, now) == 0
