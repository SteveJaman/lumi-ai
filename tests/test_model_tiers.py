from modules import model_tiers as mt


def test_the_deep_model_is_used_when_installed():
    assert mt.pick_deep(["qwen3:8b", "inkling-2-mini:latest"]) == "inkling-2-mini"


def test_it_falls_back_to_the_fast_model_when_missing():
    assert mt.pick_deep(["qwen3:8b", "qwen3.5:9b"]) == mt.FAST
    assert mt.pick_deep([]) == mt.FAST


def test_deep_calls_are_capped_and_unloaded_quickly():
    assert mt.deep_options()["num_predict"] == mt.DEEP_MAX_TOKENS
    assert mt.deep_options(256)["num_predict"] == 256
    assert mt.DEEP_KEEP_ALIVE == "30s" and mt.FAST_KEEP_ALIVE == "10m"
