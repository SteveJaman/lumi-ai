"""Play requests go to the user's own music first: a named playlist, then a
song in Liked Songs, and only then Spotify's catalogue. Spotify is faked; each
test checks which play request Lumi actually sends."""

import pytest

from modules import spotify as sp


def _track(n, title, artist="Artist"):
    return {"uri": f"spotify:track:t{n:03d}", "title": title, "artist": artist, "playable": True,
            "duration_ms": 1000, "number": n}


class Fake:
    def __init__(self, monkeypatch, liked=(), playlists=(), playlist_songs=None, search=None):
        self.liked = list(liked)
        self.playlists = list(playlists)
        self.playlist_songs = playlist_songs or {}
        self.search_items = search or {}
        self.plays = []          # (params, body) of each PUT /me/player/play
        self.searches = []
        monkeypatch.setattr(sp, "liked_tracks", self.liked_tracks)
        monkeypatch.setattr(sp, "library_page", self.library_page)
        monkeypatch.setattr(sp, "playlist_tracks", self.playlist_tracks)
        monkeypatch.setattr(sp, "_api", self.api)
        monkeypatch.setattr(sp, "_devices", lambda: [])

    def liked_tracks(self, offset=0, limit=50):
        page = self.liked[offset:offset + limit]
        nxt = offset + len(page) if offset + len(page) < len(self.liked) else None
        return {"items": page, "next_offset": nxt, "total": len(self.liked)}

    def library_page(self, shelf, offset=0, limit=20):
        assert shelf == "playlists"
        return {"items": self.playlists, "next_offset": None, "total": len(self.playlists)}

    def playlist_tracks(self, pid, offset=0, limit=50):
        return {"items": self.playlist_songs.get(pid, []), "next_offset": None}

    def api(self, method, path, params=None, body=None, _retry=True):
        if method == "GET" and path == "/search":
            self.searches.append(params)
            return {f"{params['type']}s": {"items": self.search_items.get(params["type"], [])}}
        if method == "PUT" and path == "/me/player/play":
            self.plays.append((params, body))
            return None
        raise AssertionError(f"unexpected Spotify call {method} {path}")


def test_a_liked_song_plays_from_liked_songs_not_a_global_search(monkeypatch):
    liked = [_track(i, f"Other {i}") for i in range(200)]
    liked[150] = _track(150, "Snow Halation", "Mr. Liquid")
    f = Fake(monkeypatch, liked=liked)
    msg = sp.play_search("snow halation")
    assert f.searches == []                              # never asked Spotify's catalogue
    _, body = f.plays[-1]
    assert body["uris"][0] == "spotify:track:t150"       # the song the user meant, first
    assert body["offset"] == {"position": 0}
    assert "Snow Halation" in msg and "Liked Songs" in msg


def test_a_song_past_the_first_fifty_is_not_replaced_by_the_first_song(monkeypatch):
    liked = [_track(i, f"Filler {i}") for i in range(120)]
    liked[90] = _track(90, "Deep Cut", "Someone")
    f = Fake(monkeypatch, liked=liked)
    sp.play_search("deep cut")
    _, body = f.plays[-1]
    assert body["uris"][0] == "spotify:track:t090"       # not position 0 (the old bug)


def test_a_named_playlist_plays_that_playlist(monkeypatch):
    pl = [{"title": "Chill Vibes", "id": "pl1", "uri": "spotify:playlist:pl1", "owner": {}}]
    f = Fake(monkeypatch, playlists=pl)
    msg = sp.play_search("my chill playlist")
    assert f.searches == []
    params, body = f.plays[-1]
    assert body == {"context_uri": "spotify:playlist:pl1"}
    assert "Chill Vibes" in msg


def test_a_song_named_in_a_playlist_starts_at_that_song(monkeypatch):
    pl = [{"title": "Study", "id": "st", "uri": "spotify:playlist:st", "owner": {}}]
    f = Fake(monkeypatch, playlists=pl, playlist_songs={"st": [
        _track(1, "Intro"), _track(2, "Rain on Glass", "Lo Fi Cat")]})
    sp.play_search("rain on glass from my study playlist")
    _, body = f.plays[-1]
    assert body["context_uri"] == "spotify:playlist:st"
    assert body["offset"] == {"uri": "spotify:track:t002"}


def test_play_my_liked_songs_plays_liked_songs(monkeypatch):
    f = Fake(monkeypatch, liked=[_track(1, "A"), _track(2, "B")])
    msg = sp.play_search("play my liked songs")
    assert f.searches == []
    assert f.plays[-1][1]["uris"] == ["spotify:track:t001", "spotify:track:t002"]
    assert "Liked Songs" in msg


def test_a_song_not_in_the_library_comes_from_catalogue_and_the_best_hit_wins(monkeypatch):
    results = [
        {"uri": "spotify:track:wrong", "name": "Lofi Mix", "artists": [{"name": "Nobody"}]},
        {"uri": "spotify:track:right", "name": "Lofi Beats", "artists": [{"name": "Chillhop"}]},
    ]
    f = Fake(monkeypatch, liked=[_track(1, "Unrelated")], search={"track": results})
    sp.play_search("lofi beats chillhop")
    assert f.searches and f.searches[0]["limit"] == 5
    assert f.plays[-1][1] == {"uris": ["spotify:track:right"]}


def test_a_missing_playlist_is_an_error_when_the_user_asked_for_a_playlist(monkeypatch):
    f = Fake(monkeypatch, playlists=[])
    with pytest.raises(sp.SpotifyError):
        sp.play_search("nope", kind="playlist")
    assert f.plays == []


def test_play_in_context_puts_the_chosen_song_first_and_keeps_the_rest(monkeypatch):
    uris = [f"spotify:track:t{i:03d}" for i in range(80)]
    seen = {}
    monkeypatch.setattr(sp, "_with_device", lambda fn: seen.setdefault("body", fn(None)))
    monkeypatch.setattr(sp, "_api", lambda method, path, params=None, body=None, _retry=True: seen.update(body=body))
    sp.play_in_context(uris=uris, track_uri=uris[70])
    assert seen["body"]["uris"][0] == uris[70]
    assert len(seen["body"]["uris"]) == 50 and seen["body"]["offset"] == {"position": 0}
