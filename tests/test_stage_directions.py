import pytest

from modules import stage_directions as sd


def _chunks(text, size):
    return [text[i:i + size] for i in range(0, len(text), size)]


def _stream(text, size):
    s = sd.StageSplitter()
    spoken, thoughts, emotions = "", [], []
    for c in _chunks(text, size):
        o = s.feed(c)
        spoken += o.spoken
        thoughts += o.thoughts
        emotions += o.emotions
    o = s.flush()
    return spoken + o.spoken, thoughts + o.thoughts, emotions + o.emotions


TEXT = "<thought>she's about to be wrong</thought>Oh, please. [smug] Fine, I'll help [note] anyway."


def test_thoughts_and_tags_never_reach_the_spoken_text():
    out = sd.split(TEXT)
    assert out.spoken == "Oh, please.  Fine, I'll help [note] anyway."
    assert out.thoughts == ["she's about to be wrong"]
    assert out.emotions == ["smug"]


@pytest.mark.parametrize("size", [1, 2, 3, 5, 7, 40])
def test_any_chunking_gives_the_same_result(size):
    whole = sd.split(TEXT)
    spoken, thoughts, emotions = _stream(TEXT, size)
    assert spoken == whole.spoken
    assert thoughts == whole.thoughts
    assert emotions == whole.emotions


def test_a_tag_split_across_chunks_is_not_spoken():
    s = sd.StageSplitter()
    a = s.feed("Right. <tho")
    assert "<tho" not in a.spoken                # held back until it's clear what it is
    b = s.feed("ught>hidden</thought> ok")
    assert "hidden" not in a.spoken + b.spoken and b.thoughts == ["hidden"]


def test_an_unknown_bracket_word_is_ordinary_text():
    out = sd.split("Check [notes] first.")
    assert out.spoken == "Check [notes] first." and out.emotions == []


def test_an_unclosed_thought_is_kept_as_a_thought_at_the_end():
    s = sd.StageSplitter()
    s.feed("Hmm. <thought>maybe I should")
    end = s.flush()
    assert end.spoken == "" and end.thoughts == ["maybe I should"]


def test_a_trailing_bracket_is_not_lost_at_the_end():
    s = sd.StageSplitter()
    o = s.feed("ends with [")
    end = s.flush()
    assert (o.spoken + end.spoken) == "ends with ["


def test_emotion_tags_are_case_insensitive_and_only_the_known_ones():
    out = sd.split("[SMUG] yes [Blush] [excited]")
    assert out.emotions == ["smug", "blush", "excited"]
    assert out.spoken.strip() == "yes"


def test_thought_tags_are_case_insensitive():
    out = sd.split("<THOUGHT>quiet</Thought>Done.")
    assert out.thoughts == ["quiet"] and out.spoken == "Done."


def test_the_reply_stream_speaks_only_the_words_and_emits_the_thought():
    import types
    from modules import prompter as pr
    spoken, events = [], []
    sig = types.SimpleNamespace(speech_epoch=0, history=[],
                                broadcast_event=lambda name, payload: events.append((name, payload)))
    class P:
        signals = sig
        def _push_to_tts(self, text):
            spoken.append(text)
    st = pr._SpokenStream(P())
    for piece in ["<thoug", "ht>secret plan</thought>Hi there. ", "[smug] Bye now."]:
        st.feed(piece)
    st.finish(truncated=False)
    assert all("secret" not in s and "[smug]" not in s for s in spoken)
    assert spoken and spoken[0].startswith("Hi there")
    assert ("inner_thought", {"text": "secret plan"}) in events
