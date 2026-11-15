"""Lumi's mood reaches every UI: mood_sync watches the emotional state and broadcasts
mood_update on change (and as a keepalive); the UI state carries the current mood."""

import asyncio
import types

from modules import mood_sync, ui_routes


def state(**kw):
    base = dict(valence=0.2, arousal=0.5, frustration=0.1, curiosity=0.4, boredom=0.0, current_frame="warm")
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_current_rounds_and_handles_missing_state():
    assert mood_sync.current(types.SimpleNamespace()) is None
    m = mood_sync.current(types.SimpleNamespace(emotional_state=state(valence=0.123456)))
    assert m == {"valence": 0.123, "arousal": 0.5, "frustration": 0.1, "curiosity": 0.4, "boredom": 0.0, "frame": "warm"}


def test_changed_needs_a_real_move():
    a = mood_sync.current(types.SimpleNamespace(emotional_state=state()))
    assert mood_sync.changed(None, a)
    assert not mood_sync.changed(a, dict(a, valence=a["valence"] + 0.01))
    assert mood_sync.changed(a, dict(a, frustration=a["frustration"] + 0.05))
    assert mood_sync.changed(a, dict(a, frame="grumpy"))


def test_run_broadcasts_changes_only(monkeypatch):
    sent = []
    monkeypatch.setattr(mood_sync.ui_sync, "broadcast", lambda ev, payload: sent.append((ev, payload)))
    monkeypatch.setattr(mood_sync, "POLL_S", 0.01)
    sig = types.SimpleNamespace(terminate=False, emotional_state=state())

    async def go():
        task = asyncio.create_task(mood_sync.run(sig))
        await asyncio.sleep(0.08)                    # first send, then nothing new
        n_first = len(sent)
        sig.emotional_state.frustration = 0.8        # Lumi gets frustrated
        await asyncio.sleep(0.05)
        sig.terminate = True
        await asyncio.sleep(0.03)
        task.cancel()
        return n_first
    n_first = asyncio.run(go())
    assert n_first == 1 and sent[0][0] == "mood_update"
    assert len(sent) == 2 and sent[1][1]["mood"]["frustration"] == 0.8


def test_ui_state_carries_the_mood(monkeypatch):
    monkeypatch.setattr(ui_routes.theme_store, "state", lambda: {"theme": "default", "themes": [], "hybrid": None})
    monkeypatch.setattr(mood_sync, "_signals", types.SimpleNamespace(emotional_state=state(boredom=0.7)))
    st = ui_routes._state()
    assert st["theme"] == "default" and st["mood"]["boredom"] == 0.7
