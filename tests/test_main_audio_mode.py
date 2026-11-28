import pytest

import main


@pytest.mark.parametrize("argv,env,want", [
    ([], {}, None),                                          # nothing set: ask as usual
    (["--audio", "local"], {}, "local"),
    (["--audio=both"], {}, "both"),
    ([], {"LUMI_AUDIO_MODE": "discord"}, "discord"),
    ([], {"LUMI_AUDIO_MODE": "1"}, "local"),
    (["--audio", "local"], {"LUMI_AUDIO_MODE": "both"}, "local"),   # the flag wins
    (["--audio", "loud"], {}, None),                         # unknown: ask
])
def test_audio_mode_can_be_preset(argv, env, want):
    assert main.preset_audio_mode(argv, env) == want
