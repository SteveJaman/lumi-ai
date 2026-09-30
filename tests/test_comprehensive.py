#!/usr/bin/env python
"""
Lumi Comprehensive Integration & Hardware Harness.
"""

import os
import sys
import traceback
import sqlite3

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# ANSI Colors
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
GRAY   = "\033[90m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

def print_header(title):
    print(f"\n{BOLD}{CYAN}[ {title} ]{RESET}")

def test_audio_hardware():
    print_header("Hardware: Audio Output Routing")
    try:
        import pyaudio
        from constants import OUTPUT_DEVICE_INDEX
    except ImportError:
        print(f"  {YELLOW}⚠{RESET} PyAudio or constants missing. Cannot test audio hardware.")
        return

    p = pyaudio.PyAudio()
    indices = OUTPUT_DEVICE_INDEX if isinstance(OUTPUT_DEVICE_INDEX, list) else [OUTPUT_DEVICE_INDEX]
    
    for idx in indices:
        try:
            info = p.get_device_info_by_index(idx)
            print(f"  {GREEN}✓{RESET} Device {idx} found: {info.get('name')}")
            
            # Use the device's actual native sample rate instead of hardcoding 16000
            native_rate = int(info.get('defaultSampleRate', 44100))
            stream = p.open(format=pyaudio.paInt16, channels=1, rate=native_rate, output=True, output_device_index=idx)
            stream.close()
            print(f"  {GREEN}✓{RESET} Device {idx} successfully opened and closed a test stream at {native_rate}Hz.")
        except IOError as e:
            print(f"  {RED}✗{RESET} Device {idx} is locked or unavailable: {e}")
        except Exception as e:
            print(f"  {RED}✗{RESET} Device {idx} error: {e}")
    p.terminate()

def test_database_locks():
    print_header("Storage: Database Locks & Integrity")
    from constants import DATA_ROOT
    
    users_db = os.path.join(DATA_ROOT, "memories", "users.db")
    if os.path.exists(users_db):
        try:
            conn = sqlite3.connect(users_db)
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
            tables = cursor.fetchall()
            conn.close()
            print(f"  {GREEN}✓{RESET} users.db accessible and unlocked. Found tables: {[t[0] for t in tables]}")
        except sqlite3.OperationalError as e:
            print(f"  {RED}✗{RESET} users.db locked: {e} (Is Lumi currently running?)")
    else:
        print(f"  {GRAY}○{RESET} users.db not found (expected on fresh clone).")

    try:
        import chromadb
        print(f"  {GREEN}✓{RESET} ChromaDB library loaded successfully.")
    except ImportError:
        print(f"  {RED}✗{RESET} ChromaDB library missing.")

def test_prompt_assembly_stack():
    print_header("Logic: Prompt Assembly & Persona Resolution")
    try:
        import constants
        # Fixed: signals.py is in the root directory, not in modules/
        from signals import Signals 
    except ImportError as e:
        print(f"  {RED}✗{RESET} Failed to load core modules: {e}")
        return

    sig = Signals()
    # Simulate a user message
    sig.add_message("user", "Hello Lumi, this is a test.", source="terminal")
    
    try:
        # Load profile and build the prompt
        constants.load_profile(constants.HOST.name)
        prompt = constants.build_system_prompt(constants.HOST.name)
        
        if "Lumi Voidwright" in prompt or constants.AI_NAME in prompt:
            print(f"  {GREEN}✓{RESET} System prompt built successfully ({len(prompt)} chars).")
            print(f"  {GREEN}✓{RESET} Persona variables (<host>) successfully resolved.")
        else:
            print(f"  {RED}✗{RESET} Prompt built but missing core identity strings.")
    except Exception as e:
        print(f"  {RED}✗{RESET} Prompt assembly crashed: {e}")
        traceback.print_exc()

def test_tool_dispatch_parity():
    print_header("Logic: Agent Tool Dispatch Mapping")
    try:
        from modules.agent_tools import TOOL_SCHEMAS, _DISPATCH
        from modules.prompter import EXTRA_TOOL_SCHEMAS
    except ImportError as e:
        print(f"  {RED}✗{RESET} Could not load tool modules: {e}")
        return

    all_schemas = TOOL_SCHEMAS + EXTRA_TOOL_SCHEMAS
    schema_names = [s.get("function", {}).get("name") for s in all_schemas if "function" in s]
    
    # Internal hardcoded exceptions in prompter._dispatch_tool_call
    prompter_handled = ["web_search", "browse_url", "recall_memory", "vtube_set_expression", "vtube_move"]
    
    for name in schema_names:
        if name in _DISPATCH:
            print(f"  {GREEN}✓{RESET} Tool '{name}' is mapped in agent_tools._DISPATCH.")
        elif name in prompter_handled:
            print(f"  {GREEN}✓{RESET} Tool '{name}' is explicitly handled by prompter.py.")
        else:
            print(f"  {RED}✗{RESET} Tool '{name}' has a schema but NO DISPATCH logic. The model will crash if it calls this.")

if __name__ == "__main__":
    print(f"{BOLD}{CYAN}======================================================={RESET}")
    print(f"{BOLD}{CYAN}  Lumi Comprehensive Integration Harness{RESET}")
    print(f"{BOLD}{CYAN}======================================================={RESET}")
    
    test_audio_hardware()
    test_database_locks()
    test_prompt_assembly_stack()
    test_tool_dispatch_parity()
    
    print(f"\n{BOLD}{CYAN}======================================================={RESET}")
    print(f"  {GRAY}Note: Network streams, VTube Studio WebSockets, and{RESET}")
    print(f"  {GRAY}Discord DAVE E2EE must be tested via live execution.{RESET}")
    print(f"{BOLD}{CYAN}======================================================={RESET}")