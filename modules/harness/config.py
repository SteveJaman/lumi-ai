"""Harness configuration: the model section and rules of LUMI.md, paths, switches.

LUMI.md's `## Model` section holds `key: value` lines (optionally as list items):

    - agent_model: qwen2.5-coder:14b
    - num_ctx: 8192

The harness never hard-codes a model name; when LUMI.md has none, it falls back
to constants.AGENT_MODEL_NAME / constants.MAIN_NUM_CTX.
"""

import os
import re

from constants import WORKSPACE_ROOT, DATA_ROOT

LUMI_MD_PATH = os.path.join(WORKSPACE_ROOT, "LUMI.md")
TASKS_DIR = os.path.join(DATA_ROOT, "tasks")
HOOKS_DIR = os.path.join(DATA_ROOT, "hooks")
WORKFLOWS_DIR = os.path.join(DATA_ROOT, "workflows")

DEFAULT_MAX_SAFETY_STEPS = 200
DEFAULT_TIME_BUDGET_S = 2 * 60 * 60
DEFAULT_STAGNATION_CYCLES = 5
DEFAULT_COMPACT_THRESHOLD = 0.80

_KV = re.compile(r"^\s*(?:[-*]\s*)?`?([A-Za-z_][\w.-]*)`?\s*[:=]\s*`?([^`#]+?)`?\s*(?:#.*)?$")


def _read(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def section(name, path=None):
    """The body of the `## <name>` section of LUMI.md ('' when absent)."""
    text = _read(path or LUMI_MD_PATH)
    m = re.search(rf"^##\s+{re.escape(name)}\s*$(.*?)(?=^##\s|\Z)", text, re.M | re.S)
    return m.group(1).strip() if m else ""


def model_settings(path=None):
    """Key/value pairs from the `## Model` section, lower-cased keys."""
    out = {}
    for line in section("Model", path).splitlines():
        m = _KV.match(line)
        if m:
            out[m.group(1).lower()] = m.group(2).strip()
    return out


def agent_model(path=None):
    """The model the harness runs (LUMI.md, else constants.AGENT_MODEL_NAME)."""
    s = model_settings(path)
    if s.get("agent_model"):
        return s["agent_model"]
    from constants import AGENT_MODEL_NAME
    return AGENT_MODEL_NAME


def num_ctx(path=None):
    """The context window in tokens (LUMI.md, else constants.MAIN_NUM_CTX)."""
    s = model_settings(path)
    try:
        return int(s["num_ctx"])
    except (KeyError, ValueError):
        from constants import MAIN_NUM_CTX
        return MAIN_NUM_CTX


def _chain(key, fallback, path=None):
    raw = model_settings(path).get(key, "")
    names = [n.strip() for n in raw.split(",") if n.strip()]
    return names or list(fallback)


def primary_agent_models(path=None):
    """Heavy-work model chain, first choice first (LUMI.md `primary_agent_models`,
    else constants.PRIMARY_AGENT_MODEL_CHAIN)."""
    import constants
    return _chain("primary_agent_models", getattr(constants, "PRIMARY_AGENT_MODEL_CHAIN", ()), path)


def fast_agent_models(path=None):
    """Quick tool-calling model chain (LUMI.md `fast_agent_models`, else
    constants.FAST_AGENT_MODEL_CHAIN)."""
    import constants
    return _chain("fast_agent_models", getattr(constants, "FAST_AGENT_MODEL_CHAIN", ()), path)


# ── task tiers (modules/model_router.py: task_tier, TurnRouter) ──────────
# daily:  conversation, web browsing, media, light tools. Must fit in VRAM, so the
#         big context (scraped pages, tool schemas) never spills to system RAM.
# coding: writing and fixing code. May spill; its small context keeps the KV
#         cache from thrashing system RAM.
TASK_TIERS = ("daily", "coding")


def daily_models(path=None):
    """Daily / web / media model chain (LUMI.md `daily_models`, else the fast chain)."""
    names = _chain("daily_models", (), path)
    return names or fast_agent_models(path)


def coding_models(path=None):
    """Coding model chain (LUMI.md `coding_models`, else the primary chain)."""
    names = _chain("coding_models", (), path)
    return names or primary_agent_models(path)


def tier_num_ctx(task, path=None):
    """Context window for a task tier (LUMI.md `daily_num_ctx` / `coding_num_ctx`,
    else `num_ctx`)."""
    s = model_settings(path)
    try:
        return int(s[f"{task}_num_ctx"])
    except (KeyError, ValueError):
        return num_ctx(path)


def agent_timeout_s(path=None):
    """Seconds one agent model call may take (LUMI.md `agent_timeout_s`, default 300).
    A 9B/14B on the 11 GB card can need minutes to read a big context."""
    return _float("agent_timeout_s", 300.0, path)


def game_model(path=None):
    """The Minecraft agent's model (LUMI.md `game_model`), or None to use constants."""
    return model_settings(path).get("game_model") or None


def _float(key, fallback, path=None):
    try:
        return float(model_settings(path)[key])
    except (KeyError, ValueError):
        return fallback


def vram_gb(path=None):
    """LUMI.md `vram_gb` overrides the detected GPU memory (None: detect)."""
    return _float("vram_gb", None, path)


def vram_headroom_gb(path=None):
    import constants
    return _float("vram_headroom_gb", getattr(constants, "VRAM_HEADROOM_GB", 2.5), path)


def think(path=None):
    """LUMI.md `think: true|false`; None (absent) leaves it to the model's default.
    Models without thinking support (e.g. qwen2.5-coder) need false or absent."""
    v = model_settings(path).get("think", "").lower()
    if v in ("true", "yes", "on", "1"):
        return True
    if v in ("false", "no", "off", "0"):
        return False
    return None


def rules_text(path=None):
    """The `## Rules` section of LUMI.md, re-injected at start and after compaction."""
    return section("Rules", path)


def _env_int(name, default):
    try:
        return int(os.environ.get(name, ""))
    except ValueError:
        return default


def harness_enabled():
    """Agent mode uses the harness unless LUMI_HARNESS=0."""
    return os.environ.get("LUMI_HARNESS", "1").strip().lower() not in ("0", "false", "off", "no")


def yolo_enabled():
    """LUMI_AGENT_YOLO=1 skips human approval (hooks still gate every call)."""
    return os.environ.get("LUMI_AGENT_YOLO", "").strip().lower() in ("1", "true", "on", "yes")


def max_safety_steps():
    return _env_int("LUMI_HARNESS_MAX_STEPS", DEFAULT_MAX_SAFETY_STEPS)


def time_budget_s():
    return _env_int("LUMI_HARNESS_TIME_BUDGET_S", DEFAULT_TIME_BUDGET_S)
