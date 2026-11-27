"""
Barge-in without headphones (Phase 4): telling you talking over Lumi apart from
her own voice coming back through the microphone.

While Lumi is speaking, the mic hears her too. Without a gate, every word she says
looks like you interrupting her. The gate estimates how loud her own playback will
be in the mic (its level times an echo coupling), and only counts a barge-in when
the mic is clearly louder than that echo and above a floor.

    is_barge_in(mic_level, playback_level, speaking) -> bool

Levels are RMS of the audio frame, 0..1. Pure logic; the STT loop feeds it numbers.
"""

from __future__ import annotations

ECHO_COUPLING = 0.6        # how much of her playback level reaches the mic (laptop speakers, typical)
MARGIN = 2.0               # the mic must be this many times louder than the expected echo
FLOOR = 0.02               # quieter than this is never a voice


def expected_echo(playback_level: float) -> float:
    return max(0.0, float(playback_level)) * ECHO_COUPLING


def is_barge_in(mic_level: float, playback_level: float, speaking: bool) -> bool:
    mic = max(0.0, float(mic_level))
    if mic < FLOOR:
        return False
    if not speaking:
        return True                    # she isn't talking: any voice is just you
    return mic > max(FLOOR, expected_echo(playback_level) * MARGIN)
