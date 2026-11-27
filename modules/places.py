"""
Nearby places for Lumi (Phase 5): search_nearby_places and get_place_details, read-only,
through the Google Places API (New).

Needs GOOGLE_MAPS_API_KEY and DEFAULT_LOCATION_LATLNG ("lat,lng") in .env. Without them
the tools answer with the setup steps and make no request at all. What leaves this PC:
the search text, the place id, and the default location as the search centre. Results
come back as compact lines (name, rating, price, open now, distance, address), not raw JSON.
"""

from __future__ import annotations

import math
import os
from typing import Optional

import requests

SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
DETAILS_URL = "https://places.googleapis.com/v1/places/{id}"
SEARCH_FIELDS = ("places.id,places.displayName,places.rating,places.priceLevel,"
                 "places.currentOpeningHours.openNow,places.formattedAddress,places.location")
DETAIL_FIELDS = ("id,displayName,rating,priceLevel,currentOpeningHours,formattedAddress,"
                 "internationalPhoneNumber,websiteUri")
PRICE = {"PRICE_LEVEL_FREE": "free", "PRICE_LEVEL_INEXPENSIVE": "$", "PRICE_LEVEL_MODERATE": "$$",
         "PRICE_LEVEL_EXPENSIVE": "$$$", "PRICE_LEVEL_VERY_EXPENSIVE": "$$$$"}
SETUP = ("Nearby places aren't set up yet. Add the Google Maps API key and your home location "
         "(like 34.03,-84.58) under Integrations → Nearby places.")


def _config() -> Optional[tuple]:
    key = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()
    loc = os.environ.get("DEFAULT_LOCATION_LATLNG", "").strip()
    if not key or not loc:
        return None
    try:
        lat, lng = (float(x) for x in loc.split(","))
    except ValueError:
        return None
    return key, lat, lng


def _km(a: tuple, b: tuple) -> float:
    la1, lo1 = map(math.radians, a)
    la2, lo2 = map(math.radians, b)
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(h))


def _line(p: dict, origin: tuple) -> str:
    name = (p.get("displayName") or {}).get("text") or "Unnamed place"
    bits = []
    if p.get("rating"):
        bits.append(f"{p['rating']}★")
    price = PRICE.get(p.get("priceLevel", ""), "")
    if price:
        bits.append(price)
    open_now = (p.get("currentOpeningHours") or {}).get("openNow")
    if open_now is not None:
        bits.append("open" if open_now else "closed")
    loc = p.get("location") or {}
    if "latitude" in loc and "longitude" in loc:
        bits.append(f"{_km(origin, (loc['latitude'], loc['longitude'])):.1f} km")
    addr = p.get("formattedAddress") or ""
    return name + (f" ({', '.join(bits)})" if bits else "") + (f" - {addr}" if addr else "")


def search_nearby_places(query: str, category: str = "", open_now: bool = True,
                         radius_meters: int = 8000, post=requests.post) -> str:
    cfg = _config()
    if cfg is None:
        return SETUP
    key, lat, lng = cfg
    text = " ".join(x for x in (str(query or "").strip(), str(category or "").strip()) if x)[:200]
    if not text:
        return "Say what kind of place to look for."
    body = {
        "textQuery": text,
        "maxResultCount": 5,
        "openNow": bool(open_now),
        "locationBias": {"circle": {"center": {"latitude": lat, "longitude": lng},
                                    "radius": float(max(500, min(int(radius_meters or 8000), 50000)))}},
    }
    r = post(SEARCH_URL, json=body, timeout=15,
             headers={"X-Goog-Api-Key": key, "X-Goog-FieldMask": SEARCH_FIELDS})
    if r.status_code >= 400:
        return f"Places said no ({r.status_code}). Check the API key and that Places API (New) is enabled."
    places = r.json().get("places") or []
    if not places:
        return f"No {text} found nearby" + (" that's open now." if open_now else ".")
    return "\n".join(f"{i}. {_line(p, (lat, lng))}" for i, p in enumerate(places, 1))


def get_place_details(place_id: str, get=requests.get) -> str:
    cfg = _config()
    if cfg is None:
        return SETUP
    key = cfg[0]
    pid = str(place_id or "").strip()
    if not pid or not all(c.isalnum() or c in "-_" for c in pid):
        return "That isn't a place id from a search."
    r = get(DETAILS_URL.format(id=pid), timeout=15,
            headers={"X-Goog-Api-Key": key, "X-Goog-FieldMask": DETAIL_FIELDS})
    if r.status_code >= 400:
        return f"Places said no ({r.status_code})."
    p = r.json() or {}
    lines = [_line(p, (0.0, 0.0))]
    hours = (p.get("currentOpeningHours") or {}).get("weekdayDescriptions") or []
    if hours:
        lines.append("Hours: " + "; ".join(hours[:7]))
    if p.get("internationalPhoneNumber"):
        lines.append(f"Phone: {p['internationalPhoneNumber']}")
    if p.get("websiteUri"):
        lines.append(f"Web: {p['websiteUri']}")
    return "\n".join(lines)
