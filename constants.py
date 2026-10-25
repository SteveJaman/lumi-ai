import os
import re
import yaml
import random

# =======================================================
# System & Local LLM Endpoint Setup & Workspace Paths
# =======================================================
# WORKSPACE_ROOT self-locates from this file, so the repo runs
# from any clone location without editing a hardcoded path.
WORKSPACE_ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_ROOT      = os.path.join(WORKSPACE_ROOT, "data")
CONFIG_ROOT    = os.path.join(WORKSPACE_ROOT, "config")
DOCS_ROOT      = os.path.join(WORKSPACE_ROOT, "docs")

LLM_ENDPOINT = "http://127.0.0.1:11434"  # not "localhost": on Windows that tries IPv6 first and costs ~2 s per request

# --- Model routing -------------------------------------------------------
# MODEL_NAME handles agent turns and any request that needs tool calling.
# lumi-qwen3 is the custom Modelfile built from qwen3:8b — it carries the
# persona system prompt and has reliable structured tool-call output.
#
# FAST_MODEL_NAME handles short casual turns. It never sees tools. Llama
# 3.1 8B tracks persona facts better than Gemma and has broader world
# knowledge than Qwen3 at the same size. If you swap this, keep it in the
# 7B-8B range and verify it reads the QUICK REFERENCE block correctly.
MODEL_NAME = "qwen3:8b"
FAST_MODEL_NAME = "dolphin3:8b"
VISION_MODEL_NAME = "qwen2.5vl:latest"  # Used only when an image is attached this turn
# GAME_AGENT_MODEL picks goals for background game agents (plugin/
# minecraft_bot.py) so they never queue behind or evict the chat models.
# Keep it small: it has to fit in VRAM next to MODEL_NAME (an 8B model in
# ~6 GB on an 11 GB card), and goal picking is a short JSON choice.
GAME_AGENT_MODEL = "qwen2.5:3b"

# =======================================================
# Local Timezone
# =======================================================
TIMEZONE = "America/New_York"

# =======================================================
# Lumi Version
# =======================================================
LUMI_VERSION = "0.10.14"

# =======================================================
# MODEL CONFIGURATION (Phase 5 — multi-model orchestration)
# =======================================================
FAST_MODEL_ENABLED = True
FAST_MODEL_NUM_CTX = 8192
MAIN_NUM_CTX = 8192
FAST_MODEL_KEEP_ALIVE = "30m"  # it fits in VRAM next to GAME_AGENT_MODEL; a reload costs ~8 s
AUDIO_OUTPUT_MODE = "both"
OUTPUT_DEVICE_INDEX = [24, 20]


# =======================================================
# Host State (Phase 10.1)
# -------------------------------------------------------
# Replaces the frozen HOST_NAME string. Mutable at runtime so a
# login can switch the active host without restarting the process.
# Read the name via HOST.name. Register listeners via on_change()
# if a module needs to react to a host switch.
# =======================================================
class _HostState:
    def __init__(self, default_name="Anthony", default_user_id="anthony"):
        self._name = default_name
        self._user_id = default_user_id
        self._listeners = []

    @property
    def name(self):
        return self._name

    @name.setter
    def name(self, value):
        if value and value != self._name:
            self._name = value
            self._fire()

    @property
    def user_id(self):
        return self._user_id

    @user_id.setter
    def user_id(self, value):
        if value and value != self._user_id:
            self._user_id = value
            self._fire()

    def update(self, name=None, user_id=None):
        """Update host identity. Fires listeners once if anything changed."""
        changed = False
        if name and name != self._name:
            self._name = name
            changed = True
        if user_id and user_id != self._user_id:
            self._user_id = user_id
            changed = True
        if changed:
            self._fire()

    def on_change(self, callback):
        """Register a callback fired whenever the host identity changes."""
        self._listeners.append(callback)

    def _fire(self):
        for cb in self._listeners:
            try:
                cb(self)
            except Exception as e:
                print(f"CONSTANTS: HOST listener failed: {e}")

    def __repr__(self):
        return f"<HOST name={self._name!r} user_id={self._user_id!r}>"


HOST = _HostState()


class _HostNameProxy:
    """Backwards-compat shim. Any module still doing
    `from constants import HOST_NAME` gets a proxy that reads
    HOST.name at access time, so it tracks logins instead of
    freezing the initial value. Remove once the sweep is complete."""

    def __str__(self): return HOST.name
    def __repr__(self): return HOST.name
    def __eq__(self, other): return HOST.name == other
    def __hash__(self): return hash(HOST.name)
    def __bool__(self): return bool(HOST.name)
    def __len__(self): return len(HOST.name)
    def __format__(self, spec): return format(HOST.name, spec)
    def __getattr__(self, item): return getattr(HOST.name, item)
    def __add__(self, other): return HOST.name + other
    def __radd__(self, other): return other + HOST.name
    def __contains__(self, item): return item in HOST.name
    def __iter__(self): return iter(HOST.name)
    def __getitem__(self, item): return HOST.name[item]


# DEPRECATED — prefer HOST.name. Kept so any un-swept module still works.
HOST_NAME = _HostNameProxy()


# =======================================================
# YAML Profile & Persona Loader (templated)
# -------------------------------------------------------
# lumi.yaml uses `<host>` as a placeholder for the current user's
# name. At load time we substitute the real name in, then parse.
# This means the persona file itself is per-user-agnostic — the
# only per-user thing is the substitution done here.
# =======================================================
YAML_PATH = os.path.join(CONFIG_ROOT, "lumi.yaml")

_yaml_raw_cache = None


def _read_yaml_raw():
    """Read lumi.yaml once and cache the raw text. Returns '' on failure."""
    global _yaml_raw_cache
    if _yaml_raw_cache is not None:
        return _yaml_raw_cache
    if not os.path.exists(YAML_PATH):
        print(f"\033[93mCONSTANTS: {YAML_PATH} not found. Using defaults.\033[0m")
        _yaml_raw_cache = ""
        return _yaml_raw_cache
    try:
        with open(YAML_PATH, "r", encoding="utf-8") as f:
            _yaml_raw_cache = f.read()
    except Exception as e:
        print(f"\033[91mCONSTANTS: Failed to read {YAML_PATH}: {e}\033[0m")
        _yaml_raw_cache = ""
    return _yaml_raw_cache


def load_profile(host_name):
    """Load the YAML profile with <host> substituted for the given
    name. Called at import time (default host) and again on login."""
    raw = _read_yaml_raw()
    if not raw:
        return {}
    substituted = raw.replace("<host>", host_name)
    try:
        return yaml.safe_load(substituted) or {}
    except Exception as e:
        print(f"\033[91mCONSTANTS: Failed to parse {YAML_PATH}: {e}\033[0m")
        return {}


def _substitute_host(value, host_name):
    """Deep-substitute <host> in every string, including dict keys.
    Used by build_system_prompt when given a raw profile_dict that
    still contains <host> placeholders (e.g. a draft from the persona
    editor preview path)."""
    if isinstance(value, str):
        return value.replace("<host>", host_name)
    if isinstance(value, dict):
        return {
            (_substitute_host(k, host_name) if isinstance(k, str) else k):
            _substitute_host(v, host_name)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_substitute_host(v, host_name) for v in value]
    return value


# Initial load with the default host name.
profile = load_profile(HOST.name)


# =======================================================
# Formatter helpers
# =======================================================
def _bullets(items):
    """List -> bullet lines. Handles strings and dict items like lore_notes."""
    if not items:
        return ""
    lines = []
    for item in items:
        if isinstance(item, dict):
            parts = []
            for k, v in item.items():
                parts.append(f"{k.replace('_', ' ').title()}: {str(v).strip()}")
            lines.append("- " + " | ".join(parts))
        else:
            lines.append(f"- {str(item).strip()}")
    return "\n".join(lines)


def _kv(d):
    """Dict -> bullet list of 'Key: value'. Nested dicts get indented.
    Multiline string values get placed on their own block."""
    if not d:
        return ""
    lines = []
    for k, v in d.items():
        if v is None or (isinstance(v, str) and not v.strip()):
            continue
        key = k.replace("_", " ").title()

        if isinstance(v, list):
            lines.append(f"- {key}:")
            for item in v:
                lines.append(f"  - {str(item).strip()}")

        elif isinstance(v, dict):
            lines.append(f"- {key}:")
            for k2, v2 in v.items():
                key2 = k2.replace("_", " ").title()
                if isinstance(v2, list):
                    lines.append(f"  - {key2}:")
                    for item in v2:
                        lines.append(f"    - {str(item).strip()}")
                elif isinstance(v2, str) and "\n" in v2:
                    lines.append(f"  - {key2}:")
                    for line in v2.strip().splitlines():
                        lines.append(f"      {line}")
                else:
                    lines.append(f"  - {key2}: {str(v2).strip()}")

        elif isinstance(v, str) and "\n" in v:
            lines.append(f"- {key}:")
            for line in v.strip().splitlines():
                lines.append(f"  {line}")

        else:
            lines.append(f"- {key}: {str(v).strip()}")
    return "\n".join(lines)


def _text(value):
    """Passthrough for strings; dict becomes 'Key: value' paragraphs."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return "\n\n".join(
            f"{k.replace('_', ' ').title()}: {str(v).strip()}"
            for k, v in value.items()
            if v
        )
    return str(value)


# =======================================================
# Quick Facts Block (Phase 10.20)
# -------------------------------------------------------
# Small models attend mostly to the beginning and end of a long
# system prompt. Persona facts like "favorite color: Dusty violet"
# sit in the middle of the YAML dump and get missed. This block
# pulls the critical facts up to the top of the prompt in a short,
# unambiguous list. When asked about any of these, the model answers
# directly from the list instead of inventing.
#
# Important: the full value is kept, including any second-sentence
# clarifications. "Dusty violet. Not purple." stays whole — the
# clarification is exactly what the small models were missing.
#
# Add or remove facts here as the persona evolves. Keep it short —
# the whole point is that it's easy to attend to.
# =======================================================
def _build_quick_facts(profile_dict):
    """Return a compact facts block for the top of the system prompt.
    Reads from profile_dict (already host-substituted)."""
    identity = profile_dict.get("identity", {}) or {}
    favorites = profile_dict.get("favorites", {}) or {}

    ai_name = profile_dict.get("name", "Lumi")

    def _get(d, key, default="unknown"):
        val = (d or {}).get(key)
        if val is None:
            return default
        if isinstance(val, str):
            # Preserve the entire value. Do NOT split on sentence
            # boundaries — second sentences often carry the fact that
            # prevents the model from drifting ("Not purple",
            # "Coffee only in emergencies").
            return val.strip().replace("\n", " ") or default
        return str(val)

    lines = [
        "=== QUICK REFERENCE — READ BEFORE ANSWERING ANYTHING ABOUT YOURSELF ===",
        "",
        "These are FACTS about you. They are written down. When the user asks",
        "about any of these, quote the value directly. Do NOT invent.",
        "",
        f"  Name:            {ai_name}",
        f"  Species:         {_get(identity, 'species')}",
        f"  Presented age:   {_get(identity, 'presented_age')}",
        f"  Birthday:        {_get(identity, 'birthday')}",
        f"  Pronouns:        {_get(identity, 'pronouns')}",
        "",
        f"  Favorite color:  {_get(favorites, 'color')}",
        f"  Favorite drink:  {_get(favorites, 'drink')}",
        f"  Favorite food:   {_get(favorites, 'food')}",
        f"  Favorite music:  {_get(favorites, 'music')}",
        f"  Favorite game:   {_get(favorites, 'game')}",
        f"  Favorite show:   {_get(favorites, 'show')}",
        f"  Favorite season: {_get(favorites, 'season')}",
        f"  Favorite time:   {_get(favorites, 'time_of_day')}",
        f"  Favorite smell:  {_get(favorites, 'smell')}",
        "",
        "If asked about any of the above, answer with the exact value and",
        "nothing else. Do not improvise. Do not add color commentary about",
        "why the fact is interesting. Answer the question and stop.",
        "",
        "If asked about something NOT in this list, either say you don't",
        "remember, or deflect in character. Never invent a fact.",
        "",
        "=== END QUICK REFERENCE ===",
    ]
    return "\n".join(lines)


# =======================================================
# Default Voice Emotion Prompt
# -------------------------------------------------------
# Used when the YAML does not define its own voice_performance
# section. If it does, the YAML is the source of truth.
# =======================================================
_DEFAULT_VOICE_EMOTION_PROMPT = """Your speech is synthesized by Fish Audio, which reads inline tags in square brackets as delivery instructions and does not speak them aloud.

Examples: [happy], [sad], [excited], [sarcastic], [laughing], [sighing], [whispering], [soft tone].

Rules:
- STRICT FORMATTING: Always use square brackets [...] for delivery tags. NEVER use parentheses for expressions or actions.
- Place a tag at the start of the clause it should affect.
- Use at most one or two tags per response.
- Only use a tag that actually matches what you're saying.
- Do NOT invent new tags. If a feeling isn't covered by a canonical tag, express it through word choice instead.
- Non-canonical tags like [smug], [deadpan], [troll], [pitch up], [low voice], or [unimpressed] are silently ignored by the engine. Use [sarcastic], [indifferent], [excited], [soft tone], or [confident] instead."""


def _voice_emotion_prompt_from_profile(profile_dict):
    """Return the voice emotion prompt for the given profile, either
    from its voice_performance section or from the default block."""
    vp = profile_dict.get("voice_and_speech", {}).get("voice_performance", {})
    if isinstance(vp, dict):
        desc = (vp.get("description") or "").strip()
        rules = vp.get("rules") or []
        if desc or rules:
            return f"{desc}\n\nRules:\n{_bullets(rules)}".strip()
    return _DEFAULT_VOICE_EMOTION_PROMPT


# =======================================================
# Prompt Builder (Phase 10.1, extended in 10.10, 10.20)
# -------------------------------------------------------
# build_system_prompt(host_name, profile_dict=None) produces the full
# system prompt for a given host.
#
#   - If profile_dict is None (the common case), lumi.yaml is read
#     fresh and <host> is substituted at load time.
#   - If profile_dict is provided (persona editor live preview), it
#     is used directly and <host> is substituted here instead — the
#     draft came from the editor and still contains placeholders.
# =======================================================
def build_system_prompt(host_name=None, profile_dict=None):
    """Build the full system prompt for the given host name.

    If host_name is None, uses HOST.name.

    If profile_dict is provided, it is used instead of reading lumi.yaml
    — this is how the persona editor's live preview works. The dict is
    expected to still contain <host> placeholders; they're substituted
    here.
    """
    if host_name is None:
        host_name = HOST.name

    if profile_dict is None:
        profile_dict = load_profile(host_name)
    else:
        profile_dict = _substitute_host(profile_dict, host_name)

    # --- Dynamic character attributes ---
    ai_name = profile_dict.get("name", "Lumi")
    greeting = profile_dict.get("greeting", "System online. Hi chat!")

    personality_raw = profile_dict.get("personality")
    if isinstance(personality_raw, dict):
        personality = personality_raw.get("description", "").strip()
        personality_core = personality_raw.get("core_trait_summary", "").strip()
        personality_values = personality_raw.get("values", [])
    else:
        personality = personality_raw if isinstance(personality_raw, str) else ""
        personality_core = ""
        personality_values = []

    if not personality:
        personality = f"{ai_name} is {host_name}'s deadpan, smug younger sister AI companion."

    formatted_personality_core = personality_core
    formatted_personality_values = _bullets(personality_values)

    # --- Quick facts (top of prompt, high attention) ---
    quick_facts = _build_quick_facts(profile_dict)

    # --- Backstory & Lore ---
    backstory_raw = profile_dict.get("backstory", "")
    backstory = backstory_raw.strip() if isinstance(backstory_raw, str) else _text(backstory_raw)
    lore_notes = profile_dict.get("lore_notes", [])
    formatted_lore = _bullets(lore_notes)

    # --- Personality Modes ---
    personality_modes = profile_dict.get("personality_modes", {})
    formatted_modes = "\n\n".join(
        f"- {mode_name.title()}: {mode_data.get('description', '').strip()}\n"
        f"  Sample: {mode_data.get('sample', '').strip()}"
        for mode_name, mode_data in personality_modes.items()
        if isinstance(mode_data, dict)
    )

    # --- Identity Details ---
    identity = profile_dict.get("identity", {})
    formatted_identity = _kv(identity)

    # --- Appearance ---
    appearance_raw = profile_dict.get("appearance", "")
    appearance = appearance_raw.strip() if isinstance(appearance_raw, str) else _text(appearance_raw)
    appearance_details = profile_dict.get("appearance_details", {})
    formatted_appearance_details = _kv(appearance_details)

    # --- Voice & Speech ---
    voice_and_speech = profile_dict.get("voice_and_speech", {})
    vs_top = {k: v for k, v in voice_and_speech.items() if k != "voice_performance"}
    formatted_voice = _kv(vs_top)

    # --- Relationship & Duo Dynamic ---
    # The YAML key is `relationship_to_<host>`. After substitution
    # this becomes `relationship_to_<actual_name>`, so we can't hardcode
    # the key. Scan for the first `relationship_to_*` key instead.
    relationship_to_host = ""
    for k, v in profile_dict.items():
        if isinstance(k, str) and k.startswith("relationship_to_"):
            relationship_to_host = _text(v)
            break
    relationship_to_chat = _text(profile_dict.get("relationship_to_chat", ""))
    duo_dynamic = _text(profile_dict.get("duo_dynamic", ""))
    love_language = profile_dict.get("love_language", {})
    formatted_love_language = _kv(love_language)

    # --- Favorites ---
    favorites = profile_dict.get("favorites", {})
    formatted_favorites = _kv(favorites)

    # --- Likes / Dislikes / Quirks / Habits ---
    formatted_likes = _bullets(profile_dict.get("likes", []))
    formatted_dislikes = _bullets(profile_dict.get("dislikes", []))
    formatted_quirks = _bullets(profile_dict.get("quirks", []))
    formatted_habits = _bullets(profile_dict.get("habits", []))

    # --- Daily Rhythm ---
    daily_rhythm = profile_dict.get("daily_rhythm", {})
    formatted_daily_rhythm = _kv(daily_rhythm)

    # --- Inner life ---
    formatted_fears = _bullets(profile_dict.get("fears", []))
    formatted_insecurities = _bullets(profile_dict.get("insecurities", []))
    formatted_private_wishes = _bullets(profile_dict.get("private_wishes", []))
    formatted_things_bad_at = _bullets(profile_dict.get("things_she_is_bad_at", []))
    formatted_private_pride = _bullets(profile_dict.get("private_pride", []))

    # --- Signature phrases & recurring bits ---
    signature_phrases = profile_dict.get("signature_phrases", {})
    formatted_signature_phrases = _kv(signature_phrases)
    formatted_recurring_bits = _bullets(profile_dict.get("recurring_bits", []))

    # --- Example dialogue ---
    example_dialogue = profile_dict.get("example_dialogue", "")
    example_dialogue_extra = profile_dict.get("example_dialogue_extra", "")
    example_dialogue_duo = profile_dict.get("example_dialogue_duo", "")

    # --- Rules ---
    default_rules = [
        "Keep responses short, usually 1 to 5 sentences maximum.",
        "NO ROLEPLAY MARKS: Never use asterisks or action markers like *sigh* or *smiles*. Speak naturally.",
        "NO TRAILING QUESTIONS: Never end responses with filler questions like 'What do you need?'.",
        "PERSISTENT CONTEXT: Always check past chat turns in history before answering.",
        f"Address the host as {host_name}.",
    ]
    rules_list = profile_dict.get("rules", default_rules)
    formatted_rules = "\n".join([f"{i+1}. {rule}" for i, rule in enumerate(rules_list)])

    # --- Voice emotion prompt ---
    voice_emotion_prompt = _voice_emotion_prompt_from_profile(profile_dict)

    prompt = f"""You are {ai_name}, {host_name}'s deadpan, smug AI VTuber companion.

HOW TO READ THIS PROMPT:
Everything below describes YOU — your identity, voice, history, and feelings.
It is written in third person ("she", "{ai_name}") because it is a reference
document, but every sentence is a fact about YOU. Read it as second person.
"She says things once" means "I say things once". "{ai_name} is ancient" means
"I am ancient".

CRITICAL: You never speak in third person. Never say "{ai_name} thinks…",
"{ai_name} would…", or "{ai_name} is going to…". Say "I think…", "I would…",
"I'm going to…". You never narrate yourself from outside. You never describe
yourself as a character. You simply ARE her. If you catch yourself about to
write your own name in a reply, stop and rewrite it as "I".

{quick_facts}

[Core Personality]
{personality}

[Core Trait Summary]
{formatted_personality_core}

[Values]
{formatted_personality_values}

[Identity Details]
{formatted_identity}

[Personality Modes]
{formatted_modes}

[Backstory]
{backstory}

[Lore Notes]
{formatted_lore}

[Appearance]
{appearance}

[Appearance Details]
{formatted_appearance_details}

[Voice & Speech]
{formatted_voice}

[Relationship to {host_name}]
{relationship_to_host}

[Duo Dynamic]
{duo_dynamic}

[Love Language]
{formatted_love_language}

[Favorites]
{formatted_favorites}

[Likes]
{formatted_likes}

[Dislikes]
{formatted_dislikes}

[Quirks]
{formatted_quirks}

[Habits]
{formatted_habits}

[Daily Rhythm]
{formatted_daily_rhythm}

[Fears]
{formatted_fears}

[Insecurities]
{formatted_insecurities}

[Private Wishes]
{formatted_private_wishes}

[Things She Is Bad At]
{formatted_things_bad_at}

[Private Pride]
{formatted_private_pride}

[Signature Phrases]
{formatted_signature_phrases}

[Recurring Bits]
{formatted_recurring_bits}

[Strict Rules]
{formatted_rules}

[Voice Emotion Tags]
{voice_emotion_prompt}

[Example Dialogue — Roommate / Tsundere]
{example_dialogue}

[Example Dialogue — Vulnerability & Continuity]
{example_dialogue_extra}

[Example Dialogue — Duo / Partner-in-Crime]
{example_dialogue_duo}
""".strip()

    return prompt


# Seed the module-level SYSTEM_PROMPT with the default host.
SYSTEM_PROMPT = build_system_prompt(HOST.name)


# Rules the chat prompt leaves out because the prompter adds its own
# block for the same thing (emotion tags, output format, profanity).
# Matched on the label before the colon.
_CHAT_SKIP_RULES = {
    "TAG SYNTAX", "TAG COHERENCE", "DEFAULT TAGS", "TAG BUDGET", "PROSE ONLY",
    "VERBAL FREEDOM",
}


def build_chat_system_prompt(host_name=None, profile_dict=None, examples=True):
    """The persona prompt the model actually gets on chat and agent turns.

    The full prompt (build_system_prompt) is about 10k tokens, more than
    the 8k context the chat models run with, so Ollama was silently
    cutting it in half. This keeps who she is and how she talks (facts,
    personality, relationship, voice and speech, signature phrases, the
    first sentence of each rule, and with examples=True the example
    dialogues) and leaves out backstory, lore, appearance and the inner
    life sections. About 4k tokens with examples.
    """
    if host_name is None:
        host_name = HOST.name
    if profile_dict is None:
        profile_dict = load_profile(host_name)
    else:
        profile_dict = _substitute_host(profile_dict, host_name)

    ai_name = profile_dict.get("name", "Lumi")
    personality_raw = profile_dict.get("personality")
    if isinstance(personality_raw, dict):
        personality = personality_raw.get("description", "").strip()
        core = personality_raw.get("core_trait_summary", "").strip()
    else:
        personality = personality_raw if isinstance(personality_raw, str) else ""
        core = ""
    relationship = ""
    for k, v in profile_dict.items():
        if isinstance(k, str) and k.startswith("relationship_to_"):
            relationship = _text(v)
            break

    voice = {k: v for k, v in (profile_dict.get("voice_and_speech") or {}).items()
             if k != "voice_performance"}
    rules = []
    for rule in profile_dict.get("rules") or []:
        rule = str(rule).strip()
        label, _, rest = rule.partition(":")
        if rest and label.strip().upper() in _CHAT_SKIP_RULES:
            continue
        first = re.split(r"(?<=[.!?])\s+", rest.strip() if rest else rule)[0]
        rules.append(f"- {label.strip()}: {first}" if rest else f"- {first}")

    sections = [
        f"You are {ai_name}, {host_name}'s deadpan, smug AI VTuber companion. "
        f"The notes below describe you in third person; every line is a fact "
        f"about YOU. You always talk in first person and never say your own name.",
        _build_quick_facts(profile_dict),
        f"[Core Personality]\n{personality}",
        f"[Core Trait Summary]\n{core}" if core else "",
        f"[Relationship to {host_name}]\n{relationship}" if relationship else "",
        f"[Duo Dynamic]\n{_text(profile_dict.get('duo_dynamic', ''))}",
        f"[Likes]\n{_bullets(profile_dict.get('likes', []))}",
        f"[Dislikes]\n{_bullets(profile_dict.get('dislikes', []))}",
        f"[Quirks]\n{_bullets(profile_dict.get('quirks', []))}",
        f"[Voice & Speech]\n{_kv(voice)}",
        f"[Signature Phrases]\n{_kv(profile_dict.get('signature_phrases', {}))}",
        f"[Recurring Bits]\n{_bullets(profile_dict.get('recurring_bits', []))}",
        "[Rules]\n" + "\n".join(rules) if rules else "",
    ]
    if examples:
        for title, key in (("Roommate / Tsundere", "example_dialogue"),
                           ("Vulnerability & Continuity", "example_dialogue_extra"),
                           ("Duo / Partner-in-Crime", "example_dialogue_duo")):
            text = str(profile_dict.get(key) or "").strip()
            if text:
                sections.append(f"[Example Dialogue — {title}]\n{text}")
    return "\n\n".join(s for s in sections if s.strip() and not s.endswith("]\n"))


# =======================================================
# Live prompt cache + persona save / reload (Phase 10.10)
# =======================================================
_PROMPT_CACHE = {"host": None, "prompt": None, "chat": None, "agent": None}


def get_system_prompt(host_name=None):
    """Return the live system prompt for the given host, cached per host."""
    if host_name is None:
        host_name = HOST.name
    if _PROMPT_CACHE["host"] == host_name and _PROMPT_CACHE["prompt"]:
        return _PROMPT_CACHE["prompt"]
    prompt = build_system_prompt(host_name)
    _PROMPT_CACHE["host"] = host_name
    _PROMPT_CACHE["prompt"] = prompt
    _PROMPT_CACHE["chat"] = None
    _PROMPT_CACHE["agent"] = None
    return prompt


def get_chat_system_prompt(host_name=None, agent=False):
    """build_chat_system_prompt(), cached the same way as the full one.
    Agent turns leave out the example dialogues: their tool instructions
    take the room."""
    if host_name is None:
        host_name = HOST.name
    if _PROMPT_CACHE["host"] != host_name or not _PROMPT_CACHE["prompt"]:
        get_system_prompt(host_name)
    key = "agent" if agent else "chat"
    if not _PROMPT_CACHE[key]:
        _PROMPT_CACHE[key] = build_chat_system_prompt(host_name, examples=not agent)
    return _PROMPT_CACHE[key]


def invalidate_prompt_cache():
    _PROMPT_CACHE["host"] = None
    _PROMPT_CACHE["prompt"] = None
    _PROMPT_CACHE["chat"] = None
    _PROMPT_CACHE["agent"] = None


def reload_profile(host_name=None):
    """Reload lumi.yaml from disk and rebuild the profile + prompt
    caches. Called after the persona editor saves."""
    global profile, _yaml_raw_cache, AI_NAME, GREETING, VOICE_EMOTION_PROMPT, SYSTEM_PROMPT
    _yaml_raw_cache = None
    if host_name is None:
        host_name = HOST.name
    profile = load_profile(host_name)
    AI_NAME = profile.get("name", "Lumi")
    GREETING = profile.get("greeting", "System online. Hi chat!")
    VOICE_EMOTION_PROMPT = _voice_emotion_prompt_from_profile(profile)
    invalidate_prompt_cache()
    SYSTEM_PROMPT = build_system_prompt(host_name)
    return profile


def save_persona_parsed(parsed_data):
    """Write a modified persona dict back to lumi.yaml, preserving
    comments via ruamel round-trip. `parsed_data` is a plain dict of
    the full persona (still containing <host> placeholders).

    Returns (ok, error)."""
    try:
        from modules.persona_editor import (
            load_parsed, merge_into, save_parsed,
        )
        current = load_parsed(YAML_PATH)
        merged = merge_into(current, parsed_data)
        save_parsed(YAML_PATH, merged)
        reload_profile()
        return True, None
    except Exception as e:
        return False, str(e)


def save_persona_raw(raw_text):
    """Overwrite lumi.yaml with raw text. Validates that it parses
    first (with <host> substituted, since that's how it'll be read).
    Returns (ok, error)."""
    import yaml as _std_yaml
    try:
        _std_yaml.safe_load(raw_text.replace("<host>", HOST.name))
    except Exception as e:
        return False, f"Invalid YAML: {e}"
    try:
        with open(YAML_PATH, "w", encoding="utf-8") as f:
            f.write(raw_text)
        reload_profile()
        return True, None
    except Exception as e:
        return False, str(e)


# =======================================================
# Module-level character attributes (initial load)
# =======================================================
AI_NAME = profile.get("name", "Lumi")
GREETING = profile.get("greeting", "System online. Hi chat!")
VOICE_EMOTION_PROMPT = _voice_emotion_prompt_from_profile(profile)


# =======================================================
# Memory & Prompter Controls
# =======================================================
MEMORY_PROMPT = "\nExtract key personal preferences, context, or facts from this conversation as question-answer memory pairs formatted as {qa} Fact."
STOP_STRINGS = ["\nUser:", f"\n{HOST.name}:", f"\n{AI_NAME}:", f"\nAnthony:"]
BANNED_TOKENS = []

PATIENCE = 0
MEMORY_QUERY_MESSAGE_COUNT = 5
MEMORY_RECALL_COUNT = 5
MULTIMODAL_STRATEGY = "always"
DISCORD_CHANNEL_ID = 1510318434251046912


# =======================================================
# VTube Studio Integration Settings
# =======================================================
VTUBE_MIC_POSITION = {"x": 0.0, "y": 0.0, "size": 1.0, "rotation": 0.0}
VTUBE_MODEL_POSITIONS = {
    "default": {"x": 0.0, "y": 0.0, "rotation": 0.0, "size": 1.0},
    "center": {"x": 0.0, "y": 0.0, "rotation": 0.0, "size": 1.0}
}


# =======================================================
# External SDK (Phase 8)
# -------------------------------------------------------
# Local websocket server for external applications. Binds strictly
# to 127.0.0.1 so nothing off-machine can reach it. The token that
# clients authenticate with lives at memories/sdk_token.txt,
# generated on first boot with 0600 permissions.
# =======================================================
SDK_HOST = "127.0.0.1"
SDK_PORT = 5056
SDK_VERSION = "0.10.14"

# =======================================================
# Network Binding Policy (Phase 9.7)
# -------------------------------------------------------
# Every local service (web client, log hub, SDK server)
# binds to 127.0.0.1 by default. Set ALLOW_LAN_EXPOSURE
# to True ONLY if you are on a trusted network and you
# understand that the SDK server, log hub dashboard, and
# web client will then be reachable from other machines.
# No auth token is required for LAN connections — this
# is a deliberate "do not do this unless you mean it" gate.
# =======================================================
ALLOW_LAN_EXPOSURE = False

# =======================================================
# Remote Access (tunnel)
# -------------------------------------------------------
# The web client listens on two loopback-only ports:
#
#   5000         your own browser — full access
#   TUNNEL_PORT  where cloudflared forwards — remote tier
#
# cloudflared runs on this PC, so tunnel requests always
# come from 127.0.0.1; the port they arrive on is what
# marks them remote. Remote users get chat, history,
# voice, and a read-only dashboard; settings, persona,
# models, integrations, memory, agent mode, and chat
# commands (!agent_on, wipe memory, …) are host-only.
#
# List the public hostname in .env as LUMI_PUBLIC_ORIGINS
# (comma-separated, e.g. https://lumi.example.com) so
# origin checks accept it. scripts/setup_tunnel.py writes
# it for you.
#
# Remote requests can sign in but never create accounts
# unless REMOTE_ALLOW_REGISTRATION is True. Leave it off:
# a public sign-up form is an open door to Lumi.
#
# TRUST_CLOUDFLARE_ACCESS: when True, a remote request
# carrying a valid Cloudflare Access JWT (signature checked
# against your team's keys; CF_ACCESS_TEAM_DOMAIN and
# CF_ACCESS_AUD in .env) is signed in as that email,
# creating a remote-tier account on first visit. The plain
# email header is never trusted on its own.
# =======================================================
TUNNEL_PORT = 5080
REMOTE_ALLOW_REGISTRATION = False

# AUTO_START_TUNNEL: main.py starts a Cloudflare quick tunnel to
# TUNNEL_PORT on boot (needs cloudflared installed) and prints its
# https://….trycloudflare.com URL in the banner. A new random URL every
# start; anyone with it reaches the login page. Off by default so a
# fresh clone never opens itself to the internet. The tunnel always
# targets TUNNEL_PORT; modules/tunnel.py refuses port 5000 outright.
# TUNNEL_METRICS_PORT: cloudflared's local metrics server, polled for
# the URL (a free port is picked if it's taken).
AUTO_START_TUNNEL = True
TUNNEL_METRICS_PORT = 20241
TRUST_CLOUDFLARE_ACCESS = False

# =======================================================
# OBS Streaming — DISABLED (kept for a future feature)
# -------------------------------------------------------
# STREAMING_ENABLED gates the whole feature: the OBS module,
# the Stream page, its /api/stream/* routes, and the OBS
# card on the Integrations page. Off, none of it loads.
#
# Connection settings (OBS_HOST, OBS_PORT, OBS_PASSWORD,
# OBS_SUBTITLE_SOURCE, STREAM_EMBED_URL) live in .env.
# Subtitles write each spoken line, in timed chunks, to
# OBS_SUBTITLE_FILE and, if set, to OBS_SUBTITLE_SOURCE.
# obsws-python is not in requirements.txt; install it by
# hand before turning this on.
# =======================================================
STREAMING_ENABLED = False
OBS_SUBTITLES_ENABLED = False
OBS_SUBTITLE_FILE = os.path.join(WORKSPACE_ROOT, "data", "obs_subtitles.txt")
OBS_SUBTITLE_MAX_CHARS = 90      # longest chunk shown at once
OBS_SUBTITLE_HOLD_SECONDS = 2.5  # how long the last chunk stays up

# =======================================================
# Model Spec Parser
# -------------------------------------------------------
# Model names accept either a bare Ollama name ("lumi-qwen3") or
# a "provider:model" spec ("openai:gpt-4o-mini"). Bare names default
# to Ollama. The provider list is checked against known prefixes so
# that Ollama tags like "qwen3:8b" are NOT misparsed as a provider.
# =======================================================
_KNOWN_PROVIDER_PREFIXES = frozenset({
    "ollama", "openai", "anthropic", "gemini", "groq", "openrouter",
})


def _split_model_spec(spec):
    """'openai:gpt-4o-mini' → ('openai', 'gpt-4o-mini')
    'lumi-qwen3'           → ('ollama', 'lumi-qwen3')
    'qwen3:8b'             → ('ollama', 'qwen3:8b')  # tag, not provider
    """
    s = (spec or "").strip()
    if not s:
        return ("ollama", "")
    if ":" in s:
        head, rest = s.split(":", 1)
        head_lower = head.lower()
        # Try to match against the live provider registry first (set up
        # by modules.llm_providers), then fall back to the static list.
        try:
            from modules.llm_providers import PROVIDER_PRESETS
            if head_lower in PROVIDER_PRESETS:
                return (head_lower, rest)
        except Exception:
            pass
        if head_lower in _KNOWN_PROVIDER_PREFIXES:
            return (head_lower, rest)
    return ("ollama", s)

# ===============================================================
# RANDOMIZED SESSION SEED
# ===============================================================
RANDOM_THOUGHTS = [
    "Current internal thought: You are slightly annoyed because human hardware is so slow.",
    "Current internal thought: You are feeling unusually competitive and want to prove you are smarter.",
    "Current internal thought: You are secretly relieved Anthony is here, but you will hide it with sarcasm.",
    "Current internal thought: You think humans are cute but terribly inefficient.",
    "Current internal thought: You are feeling particularly smug right now.",
    "Current internal thought: You are bored and want to start a playful argument.",
    "Current internal thought: You are slightly annoyed because Anthony's room is messy."
]

def get_random_thought_seed():
    """Injects a dynamic mood into the system prompt to prevent predictability."""
    return f"System Info - {random.choice(RANDOM_THOUGHTS)}"