"""
Home info: display preferences and the weather, for Lumi Island's Home tab
(and anything else that wants them).

  * Preferences (data/display.json), edited in the web client under
    Settings → General → Display and applied immediately:
        time_format  "12h" | "24h"
        units        "imperial" (°F, mph) | "metric" (°C, km/h)
        location     the city you typed, resolved once to coordinates
    Listeners are told when they change, so an open island updates at once.
  * Weather from Open-Meteo (https://open-meteo.com): free, no account, no
    key. The only thing sent is the city name you typed (once, to find its
    coordinates) and then those coordinates. No IP lookup, no auto-detect,
    and nothing is requested until you enter a city. Results are cached for
    15 minutes and fetched only when someone asks (the island's Home tab), so
    there is no polling and an idle Lumi makes no network calls.

The routes (/api/settings/display) are host-only: none is in socketIO's
_REMOTE_API_ALLOW, so a tunnel visitor gets 403.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

import requests

from constants import DATA_ROOT

PREFS_PATH = os.path.join(DATA_ROOT, "display.json")
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
WEATHER_TTL = 900            # seconds
HTTP_TIMEOUT = 8
MAX_LOCATION_CHARS = 80

DEFAULTS: Dict[str, Any] = {
    "time_format": "12h", "units": "imperial",
    "location": "", "place": "", "lat": None, "lon": None,
}

# WMO weather codes -> (words, day icon, night icon)
_WMO = {
    0: ("Clear", "☀️", "🌙"), 1: ("Mostly clear", "🌤️", "🌙"), 2: ("Partly cloudy", "⛅", "☁️"),
    3: ("Overcast", "☁️", "☁️"), 45: ("Fog", "🌫️", "🌫️"), 48: ("Freezing fog", "🌫️", "🌫️"),
    51: ("Light drizzle", "🌦️", "🌧️"), 53: ("Drizzle", "🌦️", "🌧️"), 55: ("Heavy drizzle", "🌧️", "🌧️"),
    56: ("Freezing drizzle", "🌧️", "🌧️"), 57: ("Freezing drizzle", "🌧️", "🌧️"),
    61: ("Light rain", "🌦️", "🌧️"), 63: ("Rain", "🌧️", "🌧️"), 65: ("Heavy rain", "🌧️", "🌧️"),
    66: ("Freezing rain", "🌧️", "🌧️"), 67: ("Freezing rain", "🌧️", "🌧️"),
    71: ("Light snow", "🌨️", "🌨️"), 73: ("Snow", "🌨️", "🌨️"), 75: ("Heavy snow", "❄️", "❄️"),
    77: ("Snow grains", "🌨️", "🌨️"), 80: ("Rain showers", "🌦️", "🌧️"), 81: ("Rain showers", "🌧️", "🌧️"),
    82: ("Violent showers", "🌧️", "🌧️"), 85: ("Snow showers", "🌨️", "🌨️"), 86: ("Heavy snow showers", "❄️", "❄️"),
    95: ("Thunderstorm", "⛈️", "⛈️"), 96: ("Thunderstorm, hail", "⛈️", "⛈️"), 99: ("Severe thunderstorm", "⛈️", "⛈️"),
}


class HomeInfoError(Exception):
    """Something worth showing to the user as-is."""


class InvalidValue(HomeInfoError, ValueError):
    """The user typed something unusable (a 400)."""


class NotConfigured(HomeInfoError):
    """No weather location has been set."""


def describe(code: int, is_day: bool) -> tuple:
    words, day, night = _WMO.get(int(code), ("Unknown", "🌡️", "🌡️"))
    return words, (day if is_day else night)


def _atomic_write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{uuid.uuid4().hex[:6]}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


class HomeInfo:
    def __init__(self, path: str = PREFS_PATH) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._listeners: List[Callable[[dict], None]] = []
        self._cache: Optional[dict] = None          # {"key": ..., "at": ..., "data": ...}
        self._prefs = dict(DEFAULTS)
        try:
            with open(path, encoding="utf-8") as f:
                saved = json.load(f)
            if isinstance(saved, dict):
                for k in DEFAULTS:
                    if k in saved:
                        self._prefs[k] = saved[k]
        except (OSError, ValueError):
            pass
        self._sanitize()

    def _sanitize(self) -> None:
        p = self._prefs
        if p["time_format"] not in ("12h", "24h"):
            p["time_format"] = DEFAULTS["time_format"]
        if p["units"] not in ("imperial", "metric"):
            p["units"] = DEFAULTS["units"]
        if not (isinstance(p["lat"], (int, float)) and isinstance(p["lon"], (int, float))
                and -90 <= p["lat"] <= 90 and -180 <= p["lon"] <= 180):
            p["lat"] = p["lon"] = None
        p["location"] = str(p.get("location") or "")[:MAX_LOCATION_CHARS]
        p["place"] = str(p.get("place") or "")[:120]
        if p["lat"] is None:
            p["place"] = ""

    # ── preferences ──────────────────────────────────────────────

    def get_prefs(self) -> Dict[str, Any]:
        """What clients see. Coordinates stay in this module."""
        with self._lock:
            p = self._prefs
            return {"time_format": p["time_format"], "units": p["units"],
                    "location": p["location"], "place": p["place"], "has_location": p["lat"] is not None}

    def update(self, values: Dict[str, Any]) -> Dict[str, Any]:
        """Apply any of time_format / units / location. Resolving a new
        location needs the network (blocking: run it in an executor)."""
        if not isinstance(values, dict):
            raise InvalidValue("Expected an object of settings.")
        new = dict(self._prefs)
        if "time_format" in values:
            if values["time_format"] not in ("12h", "24h"):
                raise InvalidValue("Time format must be 12h or 24h.")
            new["time_format"] = values["time_format"]
        if "units" in values:
            if values["units"] not in ("imperial", "metric"):
                raise InvalidValue("Units must be imperial or metric.")
            new["units"] = values["units"]
        if "location" in values:
            text = " ".join(str(values["location"] or "").split())[:MAX_LOCATION_CHARS]
            if text != new["location"] or (text and new["lat"] is None):
                if not text:
                    new.update(location="", place="", lat=None, lon=None)
                else:
                    place, lat, lon = self.geocode(text)
                    new.update(location=text, place=place, lat=lat, lon=lon)
        with self._lock:
            changed = {k: v for k, v in new.items() if self._prefs.get(k) != v}
            if not changed:
                return self.get_prefs()
            self._prefs = new
            self._sanitize()
            if {"lat", "lon", "units"} & set(changed):
                self._cache = None
            _atomic_write(self._path, json.dumps(self._prefs))
            prefs = self.get_prefs()
        for fn in list(self._listeners):
            try:
                fn(prefs)
            except Exception:
                pass
        return prefs

    def add_listener(self, fn: Callable[[dict], None]) -> None:
        """fn(prefs) runs after a change, from whichever thread made it."""
        self._listeners.append(fn)

    # ── weather ──────────────────────────────────────────────────

    def geocode(self, text: str) -> tuple:
        try:
            r = requests.get(GEOCODE_URL, params={"name": text, "count": 1, "language": "en", "format": "json"},
                             timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            results = (r.json() or {}).get("results") or []
        except (requests.RequestException, ValueError):
            raise HomeInfoError("Couldn't reach the weather service to look that city up.")
        if not results:
            raise InvalidValue(f"I couldn't find a place called \"{text}\". Try the city name, e.g. Kennesaw.")
        top = results[0]
        parts = [top.get("name"), top.get("admin1"), top.get("country")]
        place = ", ".join(dict.fromkeys(str(p) for p in parts if p))        # no duplicate "Singapore, Singapore"
        return place, float(top["latitude"]), float(top["longitude"])

    def get_weather(self, refresh: bool = False) -> Dict[str, Any]:
        with self._lock:
            p = dict(self._prefs)
        if p["lat"] is None:
            raise NotConfigured("Set your city in Lumi: Settings, then General, then Display.")
        key = (p["lat"], p["lon"], p["units"])
        now = time.time()
        with self._lock:
            c = self._cache
        if c and c["key"] == key and not refresh and now - c["at"] < WEATHER_TTL:
            return {**c["data"], "stale": False}
        imperial = p["units"] == "imperial"
        try:
            r = requests.get(FORECAST_URL, timeout=HTTP_TIMEOUT, params={
                "latitude": p["lat"], "longitude": p["lon"], "timezone": "auto", "forecast_days": 1,
                "current": "temperature_2m,apparent_temperature,weather_code,is_day,wind_speed_10m,relative_humidity_2m",
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                "temperature_unit": "fahrenheit" if imperial else "celsius",
                "wind_speed_unit": "mph" if imperial else "kmh"})
            r.raise_for_status()
            data = self._parse(r.json(), p, imperial)
        except (requests.RequestException, ValueError, KeyError, TypeError):
            if c and c["key"] == key:
                return {**c["data"], "stale": True}              # better an old reading than none
            raise HomeInfoError("Couldn't reach the weather service.")
        with self._lock:
            self._cache = {"key": key, "at": now, "data": data}
        return {**data, "stale": False}

    @staticmethod
    def _parse(j: dict, p: dict, imperial: bool) -> dict:
        cur, day = j["current"], j.get("daily") or {}
        is_day = bool(cur.get("is_day", 1))
        words, icon = describe(cur["weather_code"], is_day)

        def first(name):
            vals = day.get(name) or []
            return vals[0] if vals else None

        def rnd(v):
            return None if v is None else int(round(v))

        return {
            "temp": rnd(cur["temperature_2m"]), "feels": rnd(cur.get("apparent_temperature")),
            "text": words, "icon": icon, "is_day": is_day, "code": int(cur["weather_code"]),
            "high": rnd(first("temperature_2m_max")), "low": rnd(first("temperature_2m_min")),
            "precip": rnd(first("precipitation_probability_max")),
            "wind": rnd(cur.get("wind_speed_10m")), "humidity": rnd(cur.get("relative_humidity_2m")),
            "unit": "°F" if imperial else "°C", "wind_unit": "mph" if imperial else "km/h",
            "place": p["place"], "at": time.time(),
        }


_instance: Optional[HomeInfo] = None


def instance() -> HomeInfo:
    global _instance
    if _instance is None:
        _instance = HomeInfo()
    return _instance


# ── Web routes ───────────────────────────────────────────────────────

def register(router, server) -> None:
    """GET/PUT /api/settings/display (host-only; login + CSRF like the rest)."""
    import asyncio
    from aiohttp import web
    from modules.socketIO import _json_error

    async def get_display(request):
        if not server._current_user(request):
            return _json_error(401, "Not logged in.")
        return web.json_response(instance().get_prefs())

    async def put_display(request):
        if not server._origin_ok(request):
            return _json_error(403, "Bad origin.")
        if not server._current_user(request):
            return _json_error(401, "Not logged in.")
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        ok, err = server._require_csrf(request, body)
        if not ok:
            return _json_error(403, err)
        values = body.get("values") if isinstance(body.get("values"), dict) else {}
        loop = asyncio.get_running_loop()
        info = instance()
        try:
            prefs = await loop.run_in_executor(None, info.update, values)
        except ValueError as e:                                   # InvalidValue
            return _json_error(400, str(e))
        except HomeInfoError as e:
            return _json_error(502, str(e))
        out: Dict[str, Any] = {"prefs": prefs}
        if prefs["has_location"] and ("location" in values or "units" in values):
            try:                                                   # show the user what Lumi will show
                out["weather"] = await loop.run_in_executor(None, info.get_weather)
            except HomeInfoError as e:
                out["weather_error"] = str(e)
        return web.json_response(out)

    router.add_get("/api/settings/display", get_display)
    router.add_put("/api/settings/display", put_display)
