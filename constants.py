import os
import yaml
import random

# =======================================================
# System & Local LLM Endpoint Setup & Workspace Paths
# =======================================================
WORKSPACE_ROOT = r"C:\Users\anguy\OneDrive - Kennesaw State University\Documents\Lumi-master"
LLM_ENDPOINT = "http://localhost:11434"
MODEL_NAME = "lumi-coder"               # main model: custom Modelfile with 32K num_ctx
VISION_MODEL_NAME = "qwen2.5vl:latest"  # Used only when an image is attached this turn

# =======================================================
# Local Timezone
# -------------------------------------------------------
# Pinned explicitly so Lumi's clock stays correct even if the
# host machine's timezone setting changes (VM, remote session,
# travel, etc.). Set to your actual zone.
# =======================================================
TIMEZONE = "America/New_York"

# =======================================================
# FAST-PATH MODEL (Phase 5 — multi-model orchestration)
# -------------------------------------------------------
# Short casual chat routes to a small fast model. Agent turns and
# complex queries stay on MODEL_NAME. Keep-alive is longer for the
# fast model so it stays warm during active chat; the main model
# unloads quickly between turns to free VRAM.
# =======================================================
FAST_MODEL_NAME = "qwen2.5:7b"          # fall back to "qwen2.5:7b" if VRAM is tight
FAST_MODEL_ENABLED = True               # set False to disable routing entirely
FAST_MODEL_NUM_CTX = 8192               # chat doesn't need 32K
FAST_MODEL_KEEP_ALIVE = "10m"            # stays warm between turns

# Audio Output Routing: "discord", "local", or "both"
AUDIO_OUTPUT_MODE = "both"

# Multi-Device Output: 19 = Virtual Cable (VTuber model), 5 = Focusrite Headphones
OUTPUT_DEVICE_INDEX = [24, 20]

# =======================================================
# YAML Profile & Persona Loader
# =======================================================
YAML_PATH = os.path.join(WORKSPACE_ROOT, "lumi.yaml")

if os.path.exists(YAML_PATH):
    try:
        with open(YAML_PATH, "r", encoding="utf-8") as f:
            profile = yaml.safe_load(f) or {}
    except Exception as e:
        print(f"\033[91mCONSTANTS: Failed to parse {YAML_PATH}: {e}\033[0m")
        profile = {}
else:
    print(f"\033[93mCONSTANTS: {YAML_PATH} not found. Using default persona settings.\033[0m")
    profile = {}

# =======================================================
# Formatter helpers — turn YAML into readable prompt text
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
# Dynamic Character Attributes
# =======================================================
AI_NAME = profile.get("name", "Lumi")
HOST_NAME = profile.get("host", "Anthony")
GREETING = profile.get("greeting", "System online. Hi chat!")

# --- Personality (string in minimal yaml, dict in expanded yaml) ---
personality_raw = profile.get("personality")
if isinstance(personality_raw, dict):
    personality = personality_raw.get("description", "").strip()
    personality_core = personality_raw.get("core_trait_summary", "").strip()
    personality_values = personality_raw.get("values", [])
else:
    personality = personality_raw if isinstance(personality_raw, str) else ""
    personality_core = ""
    personality_values = []

if not personality:
    personality = f"{AI_NAME} is {HOST_NAME}'s deadpan, smug younger sister AI companion."

formatted_personality_core = personality_core
formatted_personality_values = _bullets(personality_values)

# --- Backstory & Lore ---
backstory_raw = profile.get("backstory", "")
backstory = backstory_raw.strip() if isinstance(backstory_raw, str) else _text(backstory_raw)
lore_notes = profile.get("lore_notes", [])
formatted_lore = _bullets(lore_notes)

# --- Personality Modes ---
personality_modes = profile.get("personality_modes", {})
formatted_modes = "\n\n".join(
    f"- {mode_name.title()}: {mode_data.get('description', '').strip()}\n"
    f"  Sample: {mode_data.get('sample', '').strip()}"
    for mode_name, mode_data in personality_modes.items()
    if isinstance(mode_data, dict)
)

# --- Identity Details (birthday, presented age, self-description, etc.) ---
identity = profile.get("identity", {})
formatted_identity = _kv(identity)

# --- Appearance ---
appearance_raw = profile.get("appearance", "")
appearance = appearance_raw.strip() if isinstance(appearance_raw, str) else _text(appearance_raw)
appearance_details = profile.get("appearance_details", {})
formatted_appearance_details = _kv(appearance_details)

# --- Voice & Speech ---
voice_and_speech = profile.get("voice_and_speech", {})
# Split off voice_performance — that goes into its own prompt section
vs_top = {k: v for k, v in voice_and_speech.items() if k != "voice_performance"}
formatted_voice = _kv(vs_top)

voice_performance = voice_and_speech.get("voice_performance", {})
if isinstance(voice_performance, dict):
    vp_description = voice_performance.get("description", "").strip()
    vp_rules = voice_performance.get("rules", [])
else:
    vp_description = ""
    vp_rules = []
formatted_voice_performance_rules = _bullets(vp_rules)

# --- Relationships & Duo Dynamic ---
relationship_to_anthony = _text(profile.get("relationship_to_anthony", ""))
relationship_to_chat = _text(profile.get("relationship_to_chat", ""))
duo_dynamic = _text(profile.get("duo_dynamic", ""))
love_language = profile.get("love_language", {})
formatted_love_language = _kv(love_language)

# --- Favorites ---
favorites = profile.get("favorites", {})
formatted_favorites = _kv(favorites)

# --- Likes / Dislikes / Quirks / Habits ---
formatted_likes = _bullets(profile.get("likes", []))
formatted_dislikes = _bullets(profile.get("dislikes", []))
formatted_quirks = _bullets(profile.get("quirks", []))
formatted_habits = _bullets(profile.get("habits", []))

# --- Daily Rhythm ---
daily_rhythm = profile.get("daily_rhythm", {})
formatted_daily_rhythm = _kv(daily_rhythm)

# --- Inner life: fears, insecurities, wishes, flaws, pride ---
formatted_fears = _bullets(profile.get("fears", []))
formatted_insecurities = _bullets(profile.get("insecurities", []))
formatted_private_wishes = _bullets(profile.get("private_wishes", []))
formatted_things_bad_at = _bullets(profile.get("things_she_is_bad_at", []))
formatted_private_pride = _bullets(profile.get("private_pride", []))

# --- Signature phrases & recurring bits ---
signature_phrases = profile.get("signature_phrases", {})
formatted_signature_phrases = _kv(signature_phrases)
formatted_recurring_bits = _bullets(profile.get("recurring_bits", []))

# --- Example dialogue ---
example_dialogue = profile.get("example_dialogue", "")
example_dialogue_extra = profile.get("example_dialogue_extra", "")
example_dialogue_duo = profile.get("example_dialogue_duo", "")

# --- Rules ---
default_rules = [
    "Keep responses short, usually 1 to 5 sentences maximum.",
    "NO ROLEPLAY MARKS: Never use asterisks or action markers like *sigh* or *smiles*. Speak naturally.",
    "NO TRAILING QUESTIONS: Never end responses with filler questions like 'What do you need?'.",
    "PERSISTENT CONTEXT: Always check past chat turns in history before answering.",
    f"Address the host as {HOST_NAME}.",
]
rules_list = profile.get("rules", default_rules)
formatted_rules = "\n".join([f"{i+1}. {rule}" for i, rule in enumerate(rules_list)])

# =======================================================
# Voice Emotion Prompt
# ---------------------------------------------------------------
# If the YAML defines its own voice_performance section, we use it —
# that's the source of truth for Fish Audio tag syntax. Otherwise fall
# back to a generic Fish Audio S2 block using ONLY canonical tags.
# =======================================================
if vp_description or vp_rules:
    VOICE_EMOTION_PROMPT = f"{vp_description}\n\nRules:\n{formatted_voice_performance_rules}".strip()
else:
    VOICE_EMOTION_PROMPT = """Your speech is synthesized by Fish Audio, which reads inline tags in square brackets as delivery instructions and does not speak them aloud.

Examples: [happy], [sad], [excited], [sarcastic], [laughing], [sighing], [whispering], [soft tone].

Rules:
- STRICT FORMATTING: Always use square brackets [...] for delivery tags. NEVER use parentheses for expressions or actions.
- Place a tag at the start of the clause it should affect.
- Use at most one or two tags per response.
- Only use a tag that actually matches what you're saying.
- Do NOT invent new tags. If a feeling isn't covered by a canonical tag, express it through word choice instead.
- Non-canonical tags like [smug], [deadpan], [troll], [pitch up], [low voice], or [unimpressed] are silently ignored by the engine. Use [sarcastic], [indifferent], [excited], [soft tone], or [confident] instead."""

# =======================================================
# SYSTEM PROMPT
# =======================================================
SYSTEM_PROMPT = f"""You are {AI_NAME}, {HOST_NAME}'s deadpan, smug AI VTuber companion.

HOW TO READ THIS PROMPT:
Everything below describes YOU — your identity, voice, history, and feelings.
It is written in third person ("she", "{AI_NAME}") because it is a reference
document, but every sentence is a fact about YOU. Read it as second person.
"She says things once" means "I say things once". "{AI_NAME} is ancient" means
"I am ancient".

CRITICAL: You never speak in third person. Never say "{AI_NAME} thinks…",
"{AI_NAME} would…", or "{AI_NAME} is going to…". Say "I think…", "I would…",
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

[Relationship to {HOST_NAME}]
{relationship_to_anthony}

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
{VOICE_EMOTION_PROMPT}

[Example Dialogue — Roommate / Tsundere]
{example_dialogue}

[Example Dialogue — Vulnerability & Continuity]
{example_dialogue_extra}

[Example Dialogue — Duo / Partner-in-Crime]
{example_dialogue_duo}
""".strip()

# =======================================================
# Memory & Prompter Controls
# =======================================================
MEMORY_PROMPT = "\nExtract key personal preferences, context, or facts from this conversation as question-answer memory pairs formatted as {qa} Fact."
STOP_STRINGS = ["\nUser:", f"\n{HOST_NAME}:", f"\n{AI_NAME}:", "\nAnthony:"]
BANNED_TOKENS = []

# Engine State & Recall Tuning
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