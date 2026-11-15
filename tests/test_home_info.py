"""Display preferences + weather (modules/home_info.py), its web routes, and
the island messages that carry them. No network: requests is faked."""

import asyncio
import json
import os
import time

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from modules import home_info as hi
from modules.home_info import HomeInfo, HomeInfoError, InvalidValue, NotConfigured


class Resp:
    def __init__(self, data, status=200):
        self._d, self.status_code = data, status
    def raise_for_status(self):
        if self.status_code >= 400:
            raise hi.requests.HTTPError(str(self.status_code))
    def json(self): return self._d


GEO = {"results": [{"name": "Kennesaw", "admin1": "Georgia", "country": "United States", "latitude": 34.02343, "longitude": -84.61549}]}


def forecast(temp=58.4, code=3, is_day=1):
    return {"current": {"temperature_2m": temp, "apparent_temperature": temp - 3, "weather_code": code, "is_day": is_day,
                        "wind_speed_10m": 7.6, "relative_humidity_2m": 61},
            "daily": {"temperature_2m_max": [64.2], "temperature_2m_min": [41.5], "precipitation_probability_max": [20]}}


class FakeNet:
    """Replaces requests.get inside home_info and records every call."""
    def __init__(self, monkeypatch):
        self.calls, self.geo, self.wx, self.fail = [], GEO, forecast(), False
        monkeypatch.setattr(hi.requests, "get", self)
    def __call__(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        if self.fail:
            raise hi.requests.ConnectionError("offline")
        return Resp(self.geo if url == hi.GEOCODE_URL else self.wx)
    def weather_calls(self): return [c for c in self.calls if c[0] == hi.FORECAST_URL]


@pytest.fixture
def info(tmp_path):
    return HomeInfo(str(tmp_path / "display.json"))


@pytest.fixture
def net(monkeypatch):
    return FakeNet(monkeypatch)


# ── preferences ───────────────────────────────────────────────────

def test_defaults_are_12_hour_fahrenheit_and_no_weather(info):
    assert info.get_prefs() == {"time_format": "12h", "units": "imperial", "location": "", "place": "", "has_location": False}


def test_settings_persist_and_coordinates_stay_private(tmp_path, net):
    a = HomeInfo(str(tmp_path / "d.json"))
    a.update({"time_format": "24h", "units": "metric", "location": "Kennesaw"})
    b = HomeInfo(str(tmp_path / "d.json"))                       # a fresh process
    assert b.get_prefs() == {"time_format": "24h", "units": "metric", "location": "Kennesaw",
                             "place": "Kennesaw, Georgia, United States", "has_location": True}
    assert "lat" not in b.get_prefs() and "lon" not in b.get_prefs()


def test_a_corrupt_or_hostile_file_falls_back_to_defaults(tmp_path):
    p = tmp_path / "d.json"
    p.write_text(json.dumps({"time_format": "banana", "units": 5, "lat": 999, "lon": "x", "location": "x" * 500, "place": "Ghost"}))
    prefs = HomeInfo(str(p)).get_prefs()
    assert prefs["time_format"] == "12h" and prefs["units"] == "imperial" and prefs["has_location"] is False
    assert prefs["place"] == "" and len(prefs["location"]) <= hi.MAX_LOCATION_CHARS
    p.write_text("{not json")
    assert HomeInfo(str(p)).get_prefs()["time_format"] == "12h"


def test_bad_values_are_rejected_as_user_errors(info):
    with pytest.raises(InvalidValue):
        info.update({"time_format": "25h"})
    with pytest.raises(InvalidValue):
        info.update({"units": "kelvin"})
    with pytest.raises(ValueError):                              # maps to a 400 in the route
        info.update("nope")
    assert info.get_prefs()["time_format"] == "12h"              # nothing half-applied


def test_listeners_hear_real_changes_only(info):
    seen = []
    info.add_listener(seen.append)
    info.update({"time_format": "24h"})
    info.update({"time_format": "24h"})                          # no-op
    info.update({"units": "metric"})
    assert [s["time_format"] for s in seen] == ["24h", "24h"] and seen[-1]["units"] == "metric"


def test_a_broken_listener_cannot_break_saving(info):
    info.add_listener(lambda p: 1 / 0)
    assert info.update({"time_format": "24h"})["time_format"] == "24h"


# ── location ──────────────────────────────────────────────────────

def test_a_city_is_looked_up_once_and_only_when_it_changes(info, net):
    info.update({"location": "  Kennesaw "})
    assert [c[1]["name"] for c in net.calls] == ["Kennesaw"]     # whitespace tidied
    info.update({"location": "Kennesaw"})
    info.update({"time_format": "24h"})
    assert len(net.calls) == 1


def test_unknown_city_is_a_user_error_and_keeps_the_old_one(info, net):
    info.update({"location": "Kennesaw"})
    net.geo = {"results": []}
    with pytest.raises(InvalidValue, match="couldn't find"):
        info.update({"location": "Qwertyuiop"})
    assert info.get_prefs()["place"].startswith("Kennesaw")


def test_geocoder_down_is_not_blamed_on_the_user(info, net):
    net.fail = True
    with pytest.raises(HomeInfoError) as e:
        info.update({"location": "Kennesaw"})
    assert not isinstance(e.value, ValueError)


def test_clearing_the_city_forgets_it_and_stops_weather(info, net):
    info.update({"location": "Kennesaw"})
    info.update({"location": ""})
    assert info.get_prefs()["has_location"] is False
    with pytest.raises(NotConfigured):
        info.get_weather()


def test_duplicate_place_names_are_not_repeated(info, net):
    net.geo = {"results": [{"name": "Singapore", "admin1": "Singapore", "country": "Singapore", "latitude": 1.29, "longitude": 103.85}]}
    assert info.update({"location": "Singapore"})["place"] == "Singapore"


# ── weather ───────────────────────────────────────────────────────

def test_weather_needs_a_city_and_makes_no_request_without_one(info, net):
    with pytest.raises(NotConfigured):
        info.get_weather()
    assert net.calls == []                                       # nothing is sent until you opt in


def test_imperial_reading_is_parsed_and_rounded(info, net):
    info.update({"location": "Kennesaw"})
    wx = info.get_weather()
    assert (wx["temp"], wx["feels"], wx["high"], wx["low"], wx["precip"], wx["wind"], wx["humidity"]) == (58, 55, 64, 42, 20, 8, 61)
    assert wx["unit"] == "°F" and wx["wind_unit"] == "mph" and wx["text"] == "Overcast" and wx["icon"] == "☁️"
    assert wx["place"] == "Kennesaw, Georgia, United States" and wx["stale"] is False
    params = net.weather_calls()[0][1]
    assert params["temperature_unit"] == "fahrenheit" and params["wind_speed_unit"] == "mph"
    assert params["latitude"] == pytest.approx(34.02343) and params["timezone"] == "auto"


def test_metric_asks_for_celsius_and_night_gets_a_night_icon(info, net):
    info.update({"location": "Kennesaw", "units": "metric"})
    net.wx = forecast(code=0, is_day=0)
    wx = info.get_weather()
    assert wx["unit"] == "°C" and wx["wind_unit"] == "km/h" and wx["icon"] == "🌙" and wx["is_day"] is False
    assert net.weather_calls()[0][1]["temperature_unit"] == "celsius"


def test_weather_is_cached_for_15_minutes_and_refresh_bypasses_it(info, net):
    info.update({"location": "Kennesaw"})
    info.get_weather(); info.get_weather(); info.get_weather()
    assert len(net.weather_calls()) == 1
    info.get_weather(refresh=True)
    assert len(net.weather_calls()) == 2
    info._cache["at"] -= hi.WEATHER_TTL + 1                      # time passes
    info.get_weather()
    assert len(net.weather_calls()) == 3


def test_changing_units_or_city_discards_the_cache(info, net):
    info.update({"location": "Kennesaw"})
    info.get_weather()
    info.update({"units": "metric"})
    assert info.get_weather()["unit"] == "°C" and len(net.weather_calls()) == 2


def test_an_old_reading_beats_none_when_the_service_is_down(info, net):
    info.update({"location": "Kennesaw"})
    info.get_weather()
    net.fail = True
    wx = info.get_weather(refresh=True)
    assert wx["temp"] == 58 and wx["stale"] is True


def test_weather_failure_with_nothing_cached_is_reported(info, net):
    info.update({"location": "Kennesaw"})
    net.fail = True
    with pytest.raises(HomeInfoError, match="weather service"):
        info.get_weather()


def test_garbage_from_the_service_is_an_error_not_a_crash(info, net):
    info.update({"location": "Kennesaw"})
    net.wx = {"unexpected": True}
    with pytest.raises(HomeInfoError):
        info.get_weather()


def test_weather_codes_cover_the_common_sky():
    assert hi.describe(0, True) == ("Clear", "☀️") and hi.describe(0, False)[1] == "🌙"
    assert hi.describe(95, True)[0] == "Thunderstorm" and hi.describe(73, True)[0] == "Snow"
    assert hi.describe(12345, True)[0] == "Unknown"


# ── web routes ────────────────────────────────────────────────────

class StubServer:
    def _origin_ok(self, request): return request.headers.get("Origin") in (None, "http://127.0.0.1")
    def _current_user(self, request): return None if request.headers.get("X-Anon") else {"id": 1}
    def _require_csrf(self, request, body):
        return (True, None) if request.headers.get("X-CSRF-Token") == "tok" else (False, "Missing CSRF token.")


@pytest.fixture
def client_env(info, monkeypatch):
    monkeypatch.setattr(hi, "_instance", info)
    app = web.Application()
    hi.register(app.router, StubServer())

    def run(coro_fn):
        async def go():
            async with TestClient(TestServer(app)) as c:
                return await coro_fn(c)
        return asyncio.run(go())
    return run


H = {"X-CSRF-Token": "tok"}


def test_routes_require_login_csrf_and_a_local_origin(client_env, net):
    async def t(c):
        assert (await c.get("/api/settings/display", headers={"X-Anon": "1"})).status == 401
        assert (await c.put("/api/settings/display", json={"values": {"units": "metric"}})).status == 403
        assert (await c.put("/api/settings/display", json={"values": {}}, headers={**H, "Origin": "http://evil.test"})).status == 403
        assert (await c.put("/api/settings/display", json={"values": {}}, headers={**H, "X-Anon": "1"})).status == 401
    client_env(t)


def test_saving_a_city_returns_what_lumi_will_show(client_env, net):
    async def t(c):
        r = await c.put("/api/settings/display", headers=H, json={"values": {"location": "Kennesaw", "time_format": "24h"}})
        data = await r.json()
        assert r.status == 200 and data["prefs"]["time_format"] == "24h" and data["prefs"]["has_location"] is True
        assert data["weather"]["temp"] == 58 and data["weather"]["place"].startswith("Kennesaw")
        got = await (await c.get("/api/settings/display")).json()
        assert got["location"] == "Kennesaw" and "lat" not in got
    client_env(t)


def test_route_errors_are_400_for_the_user_and_502_for_the_network(client_env, net):
    async def t(c):
        assert (await c.put("/api/settings/display", headers=H, json={"values": {"time_format": "99h"}})).status == 400
        net.geo = {"results": []}
        r = await c.put("/api/settings/display", headers=H, json={"values": {"location": "Nowhereville"}})
        assert r.status == 400 and "couldn't find" in (await r.json())["error"]
        net.fail = True
        assert (await c.put("/api/settings/display", headers=H, json={"values": {"location": "Kennesaw"}})).status == 502
    client_env(t)


def test_a_city_saves_even_if_the_first_weather_fetch_fails(client_env, net, monkeypatch):
    async def t(c):
        orig = net.__call__
        calls = {"n": 0}
        def flaky(url, params=None, timeout=None):
            if url == hi.FORECAST_URL:
                raise hi.requests.ConnectionError("down")
            return orig(url, params, timeout)
        monkeypatch.setattr(hi.requests, "get", flaky)
        r = await c.put("/api/settings/display", headers=H, json={"values": {"location": "Kennesaw"}})
        data = await r.json()
        assert r.status == 200 and data["prefs"]["has_location"] and "weather_error" in data and "weather" not in data
    client_env(t)
