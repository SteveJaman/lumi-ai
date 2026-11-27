from modules import model_tiers as mt
from modules.harness import config


def test_first_installed_model_in_the_chain_wins():
    chain = ["qwen3:14b", "qwen2.5-coder:14b"]
    assert mt.first_installed(chain, ["qwen3:8b", "qwen2.5-coder:14b"]) == "qwen2.5-coder:14b"
    assert mt.first_installed(chain, ["qwen2.5-coder:14b", "qwen3:14b"]) == "qwen3:14b"   # chain order decides


def test_latest_tag_is_ignored_when_matching():
    assert mt.first_installed(["deep"], ["deep:latest"]) == "deep"


def test_no_installed_deep_model_means_none():
    assert mt.first_installed(["qwen3:14b"], ["qwen3:8b"]) is None
    assert mt.first_installed([], ["qwen3:8b"]) is None
    assert mt.first_installed(["qwen3:14b"], []) is None


def test_deep_calls_are_capped_unloaded_quickly_and_take_a_context():
    assert mt.deep_options()["num_predict"] == mt.DEEP_MAX_TOKENS
    assert mt.deep_options(256)["num_predict"] == 256
    assert "num_ctx" not in mt.deep_options()
    assert mt.deep_options(num_ctx=4096)["num_ctx"] == 4096
    assert mt.DEEP_KEEP_ALIVE == "30s"


def test_installed_names_is_empty_when_ollama_is_down(monkeypatch):
    import requests

    def boom(*a, **k):
        raise requests.exceptions.ConnectionError("down")
    monkeypatch.setattr(requests, "get", boom)
    assert mt.installed_names() == []


def test_the_deep_chain_comes_from_lumi_md(tmp_path):
    md = tmp_path / "LUMI.md"
    md.write_text("## Model\n\n- deep_models: qwen3:14b, qwen2.5-coder:14b\n", encoding="utf-8")
    assert config.deep_models(str(md)) == ["qwen3:14b", "qwen2.5-coder:14b"]
    md.write_text("## Model\n\n- daily_models: qwen3:8b\n", encoding="utf-8")
    assert config.deep_models(str(md)) == []
