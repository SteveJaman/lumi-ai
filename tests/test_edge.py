import sys
import os

# Go up one directory to reach the Lumi-master root workspace
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

from modules.edge.manager import edge_runtime
from modules.edge.core.hardware_detector import is_arm_edge_device, resolve_execution_provider

def test_edge_initialization():
    print("=======================================================")
    print("           Lumi Edge Module Verification Test          ")
    print("=======================================================")
    
    print(f"[*] Detected ARM Edge Device: {is_arm_edge_device()}")
    print(f"[*] Resolved Execution Provider: {resolve_execution_provider()}")
    print(f"[*] Edge Manager Active: {edge_runtime.is_edge}")
    
    if edge_runtime.is_edge:
        print("[*] Loading Edge Adapters...")
        stt = edge_runtime.get_transcriber()
        embedder = edge_runtime.get_embedder()
        print(f"    - STT Adapter: {'Loaded' if stt else 'Skipped/Unavailable'}")
        print(f"    - Embedder Adapter: {'Loaded' if embedder else 'Skipped/Unavailable'}")
        print(f"[*] Edge Config Overrides: {edge_runtime.config}")
    else:
        print("[*] Running on Standard Desktop. Edge adapters bypassed as expected.")
        
    print("=======================================================")
    print(" Test completed successfully! No import or syntax errors.")
    print("=======================================================")

if __name__ == "__main__":
    test_edge_initialization()