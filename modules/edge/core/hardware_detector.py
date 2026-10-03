import platform
import os
import json

def is_arm_edge_device() -> bool:
    """Returns True if running on an ARM64 / AArch64 edge device (e.g., Surface Pro 11)."""
    arch = platform.machine().lower()
    return arch in ["arm64", "aarch64"]

def get_edge_config() -> dict:
    """Loads edge resource overrides if running on supported ARM hardware."""
    config_path = os.path.join(
        os.path.dirname(__file__), "../configs/edge_overrides.json"
    )
    
    if is_arm_edge_device():
        print("\033[92m[lumi-edge] ARM64 Architecture detected (Surface Pro 11 target).\033[0m")
        if os.path.exists(config_path):
            try:
                with open(config_path, "r") as f:
                    return json.load(f)
            except Exception as e:
                print(f"\033[91m[lumi-edge] Failed to load edge config: {e}\033[0m")
    
    return {}

def resolve_execution_provider() -> str:
    """Returns the optimal ONNX / DirectML execution provider for the hardware."""
    if is_arm_edge_device():
        # DirectML utilizes the integrated Adreno GPU / NPU drivers on Windows ARM
        return "DmlExecutionProvider"
    return "CPUExecutionProvider"