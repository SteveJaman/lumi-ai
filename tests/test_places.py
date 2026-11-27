from modules import places


class _Resp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body or {}

    def json(self):
        return self._body


def _configured(monkeypatch):
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "k-test")
    monkeypatch.setenv("DEFAULT_LOCATION_LATLNG", "34.03,-84.58")


def test_without_setup_it_says_so_and_never_calls_google(monkeypatch):
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    called = []
    out = places.search_nearby_places("ramen", post=lambda *a, **k: called.append(1))
    assert out == places.SETUP and called == []


def test_a_bad_location_counts_as_not_set_up(monkeypatch):
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "k")
    monkeypatch.setenv("DEFAULT_LOCATION_LATLNG", "not,numbers")
    assert places.search_nearby_places("ramen") == places.SETUP


def test_results_are_compact_lines(monkeypatch):
    _configured(monkeypatch)
    body = {"places": [{"id": "p1", "displayName": {"text": "Ramen Bar"}, "rating": 4.6,
                        "priceLevel": "PRICE_LEVEL_MODERATE",
                        "currentOpeningHours": {"openNow": True},
                        "location": {"latitude": 34.04, "longitude": -84.58},
                        "formattedAddress": "1 Main St"}]}
    seen = {}

    def post(url, json=None, timeout=None, headers=None):
        seen.update(url=url, body=json, headers=headers)
        return _Resp(200, body)
    out = places.search_nearby_places("ramen", post=post)
    assert out.startswith("1. Ramen Bar (4.6★, $$, open,") and "1 Main St" in out
    assert seen["headers"]["X-Goog-Api-Key"] == "k-test"
    assert seen["body"]["openNow"] is True
    assert seen["body"]["locationBias"]["circle"]["radius"] == 8000.0


def test_no_results_and_refused_calls_have_plain_messages(monkeypatch):
    _configured(monkeypatch)
    assert "No ramen found" in places.search_nearby_places(
        "ramen", post=lambda *a, **k: _Resp(200, {}))
    assert "Places said no (403)" in places.search_nearby_places(
        "ramen", post=lambda *a, **k: _Resp(403, {}))


def test_place_ids_are_checked_before_any_request(monkeypatch):
    _configured(monkeypatch)
    called = []
    out = places.get_place_details("../../etc", get=lambda *a, **k: called.append(1))
    assert out.startswith("That isn") and called == []
