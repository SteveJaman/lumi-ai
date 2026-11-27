"""The compact media tools (five schemas for the model), their Spotify calls, and
the Windows media-session fallback when Spotify is closed. Spotify and Windows are faked."""

import asyncio

from modules import media_bridge as mb
from modules import spotify as sp
from tests.test_media import FakeAudio, FakeSpotify, FakeWin, Sess
from tests.test_spotify_play import Fake, _track


class Ops(Fake):
    """Fake Spotify that also records the playback-control calls."""

    def __init__(self, monkeypatch, player=None, **kw):
        super().__init__(monkeypatch, **kw)
        self.ops = []
        self.player = player

    def api(self, method, path, params=None, body=None, _retry=True):
        if method == "GET" and path == "/me/player":
            return self.player
        if method == "PUT" and path.startswith("/me/player/") and path != "/me/player/play":
            self.ops.append((path.rsplit("/", 1)[-1], params))
            return None
        if method == "PUT" and path == "/me/player/play":
            self.plays.append((params, body))
            return None
        return super().api(method, path, params, body, _retry)


def _run(name, args):
    return asyncio.run(sp.run_tool(name, args, None))


def test_the_model_sees_five_short_tools():
    names = [t["function"]["name"] for t in sp.TOOL_SCHEMAS]
    assert names == ["media_play_liked", "media_play_playlist", "media_search_and_play",
                     "media_control", "media_library"]
    for t in sp.TOOL_SCHEMAS:
        desc = t["function"]["description"]
        assert len(desc.split(". ")) == 1, desc           # one sentence each, per the CLAUDE.md rule


def test_play_liked_shuffles_then_plays_the_whole_list(monkeypatch):
    f = Ops(monkeypatch, liked=[_track(1, "A"), _track(2, "B")])
    _run("media_play_liked", {})
    assert f.ops[0] == ("shuffle", {"state": "true"})
    assert f.plays[-1][1]["uris"] == ["spotify:track:t001", "spotify:track:t002"]


def test_play_liked_with_a_filter_plays_that_song(monkeypatch):
    f = Ops(monkeypatch, liked=[_track(1, "Other"), _track(2, "Deep Cut", "Someone")])
    msg = _run("media_play_liked", {"query_filter": "deep cut", "shuffle": False})
    assert "Deep Cut" in msg
    assert f.plays[-1][1]["offset"] == {"position": 1}
    assert f.ops[0] == ("shuffle", {"state": "false"})


def test_play_liked_with_no_match_says_so(monkeypatch):
    Ops(monkeypatch, liked=[_track(1, "Other")])
    assert _run("media_play_liked", {"query_filter": "zzz"}).startswith("Error: None of your liked songs")


def test_play_playlist_by_name(monkeypatch):
    pl = [{"title": "Chill Vibes", "id": "pl1", "uri": "spotify:playlist:pl1", "owner": {}}]
    f = Ops(monkeypatch, playlists=pl)
    msg = _run("media_play_playlist", {"playlist_name": "chill playlist"})
    assert f.plays[-1][1] == {"context_uri": "spotify:playlist:pl1"}
    assert "Chill Vibes" in msg


def test_control_pause_resume_and_volume(monkeypatch):
    f = Ops(monkeypatch)
    _run("media_control", {"action": "pause"})
    _run("media_control", {"action": "resume"})
    _run("media_control", {"action": "volume", "value": "40"})
    assert [op[0] for op in f.ops] == ["pause", "volume"]
    assert len(f.plays) == 1 and f.plays[0][1] is None    # resume is a plain play request


def test_control_rejects_a_bad_repeat_mode(monkeypatch):
    Ops(monkeypatch)
    assert _run("media_control", {"action": "repeat", "value": "forever"}).startswith("Error:")


def test_like_current_with_nothing_playing_is_an_error(monkeypatch):
    Ops(monkeypatch, player=None)
    assert _run("media_control", {"action": "like_current"}) == "Error: Nothing is playing right now."


def test_library_lists_liked_songs(monkeypatch):
    Ops(monkeypatch, liked=[_track(1, "A", "X"), _track(2, "B", "Y")])
    out = _run("media_library", {"view": "liked"})
    assert "- A - X" in out and "- B - Y" in out


def test_windows_fallback_pauses_whatever_app_is_playing_when_spotify_is_closed():
    win = FakeWin()
    br = mb.MediaBridge(spotify_mod=FakeSpotify(connected=False), win=win,
                        audio=FakeAudio([]), fetch=lambda url: (b"", "image/jpeg"))
    msg, _ = br.command("pause")
    assert msg == "Done."
    assert ("pause", "") in win.calls                     # prefer="" -> the current media session


def test_with_no_media_app_at_all_the_fallback_says_nothing_is_playing():
    import pytest
    br = mb.MediaBridge(spotify_mod=FakeSpotify(connected=False), win=FakeWin(ok=False),
                        audio=FakeAudio([]), fetch=lambda url: (b"", "image/jpeg"))
    with pytest.raises(ValueError, match="Nothing is playing"):
        br.command("pause")


def test_the_fallback_never_handles_volume_or_seek_for_other_apps():
    win = FakeWin()
    br = mb.MediaBridge(spotify_mod=FakeSpotify(connected=False), win=win,
                        audio=FakeAudio([Sess("Spotify.exe", 10)]), fetch=lambda url: (b"", "image/jpeg"))
    try:
        br.command("seek", 30)
    except ValueError:
        pass
    assert not any(c[0] == "seek" for c in win.calls)
