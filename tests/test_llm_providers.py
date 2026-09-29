"""Tests for the multi-provider LLM client — no network calls."""

import pytest

from modules.llm_providers import (
    parse_model_spec,
    make_model_spec,
    mask_key,
    PROVIDER_PRESETS,
)


# ---- parse_model_spec --------------------------------------------------

def test_bare_name_is_ollama():
    assert parse_model_spec("lumi-qwen3") == ("ollama", "lumi-qwen3")


def test_known_provider_prefix_is_split():
    assert parse_model_spec("openai:gpt-4o-mini") == ("openai", "gpt-4o-mini")
    assert parse_model_spec("anthropic:claude-3-haiku") == \
        ("anthropic", "claude-3-haiku")
    assert parse_model_spec("groq:llama-3.3-70b-versatile") == \
        ("groq", "llama-3.3-70b-versatile")


def test_ollama_tag_is_not_mistaken_for_provider():
    # "qwen3:8b" — head "qwen3" is not a known provider, so stays ollama
    assert parse_model_spec("qwen3:8b") == ("ollama", "qwen3:8b")


def test_unknown_prefix_stays_ollama():
    assert parse_model_spec("nonexistent:model") == \
        ("ollama", "nonexistent:model")


def test_empty_spec():
    assert parse_model_spec("") == ("ollama", "")
    assert parse_model_spec("   ") == ("ollama", "")


def test_case_insensitive_provider():
    assert parse_model_spec("OpenAI:gpt-4") == ("openai", "gpt-4")
    assert parse_model_spec("GROQ:llama") == ("groq", "llama")


# ---- make_model_spec ---------------------------------------------------

def test_make_spec_ollama_stays_bare():
    assert make_model_spec("ollama", "lumi-qwen3") == "lumi-qwen3"
    assert make_model_spec("OLLAMA", "lumi-qwen3") == "lumi-qwen3"


def test_make_spec_cloud_prefixed():
    assert make_model_spec("openai", "gpt-4o") == "openai:gpt-4o"
    assert make_model_spec("groq", "llama") == "groq:llama"


def test_roundtrip_parse_make():
    for spec in ["openai:gpt-4o-mini", "groq:llama-3.3-70b", "anthropic:claude-3"]:
        provider, model = parse_model_spec(spec)
        assert make_model_spec(provider, model) == spec


# ---- mask_key ----------------------------------------------------------

def test_mask_key_empty():
    assert mask_key("") == ""
    assert mask_key(None) == ""


def test_mask_key_short_is_fully_hidden():
    assert mask_key("abc") == "•••"
    assert mask_key("12345678") == "••••••••"


def test_mask_key_long_shows_ends():
    masked = mask_key("sk-1234567890abcdef")
    assert masked.startswith("sk-1")
    assert masked.endswith("cdef")
    assert "…" in masked
    assert "567890abcd" not in masked


# ---- registry sanity ---------------------------------------------------

def test_all_presets_have_required_keys():
    for pid, preset in PROVIDER_PRESETS.items():
        assert "label" in preset, f"{pid} missing label"
        assert "kind" in preset, f"{pid} missing kind"
        assert "base_url" in preset, f"{pid} missing base_url"


def test_ollama_preset_requires_no_key():
    assert PROVIDER_PRESETS["ollama"]["key_env"] is None


def test_cloud_presets_declare_key_env():
    for pid in ("openai", "anthropic", "gemini", "groq", "openrouter"):
        assert PROVIDER_PRESETS[pid]["key_env"] is not None