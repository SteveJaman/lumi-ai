import os
import yaml
import random

# =======================================================
# System & Local LLM Endpoint Setup & Workspace Paths
# =======================================================
WORKSPACE_ROOT = r"C:\Users\anguy\OneDrive - Kennesaw State University\Documents\Lumi-master"
LLM_ENDPOINT = "http://localhost:11434"
MODEL_NAME = "lumi-qwen3"               # main model: custom Modelfile built from qwen3:8b
VISION_MODEL_NAME = "qwen2.5vl:latest"  # Used only when an image is attached this turn

# =======================================================
# Local Timezone
# =======================================================
TIMEZONE = "America/New_York"

# =======================================================
# MODEL CONFIGURATION (Phase 5 — multi-model orchestration)
# =======================================================
FAST_MODEL_NAME = "lumi-qwen3"          # same weights as main — Ollama deduplicates in VRAM
FAST_MODEL_ENABLED = True
FAST_MODEL_NUM_CTX = 8192
MAIN_NUM_CTX = 16384
FAST_MODEL_KEEP_ALIVE = "5m"

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
YAML_PATH = os.path.join(WORKSPACE_ROOT, "lumi.yaml")

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
# Prompt Builder (Phase 10.1)
# -------------------------------------------------------
# build_system_prompt(host_name) produces the full system prompt
# for a given host, with <host> substituted everywhere in the
# persona file. Called once at import to seed SYSTEM_PROMPT, and
# again by the prompter on every turn (or on login) to get the
# live version.
# =======================================================
def build_system_prompt(host_name=None):
    """Build the full system prompt for the given host name.
    If host_name is None, uses HOST.name."""
    if host_name is None:
        host_name = HOST.name

    profile_dict = load_profile(host_name)

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
    relationship_to_host = _text(profile_dict.get("relationship_to_anthony", ""))
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
# Code that imports SYSTEM_PROMPT gets this initial snapshot;
# code that needs the live version calls build_system_prompt().
SYSTEM_PROMPT = build_system_prompt(HOST.name)


# =======================================================
# Module-level character attributes (initial load)
# -------------------------------------------------------
# These reflect the default host at import time. If you need them
# to track HOST changes at runtime, call build_system_prompt() or
# load_profile(HOST.name) fresh instead of importing these.
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