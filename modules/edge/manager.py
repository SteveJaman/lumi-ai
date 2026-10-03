import os
from modules.edge.core.hardware_detector import (
    is_arm_edge_device,
    get_edge_config,
    resolve_execution_provider
)
from modules.edge.adapters.onnx_embeddings import EdgeEmbeddingAdapter
from modules.edge.adapters.light_whisper import EdgeSTTAdapter

class EdgeManager:
    """Manages hardware acceleration and lightweight adapters for edge devices."""
    def __init__(self):
        self.is_edge = is_arm_edge_device()
        self.config = get_edge_config() if self.is_edge else {}
        self.provider = resolve_execution_provider()
        
        self.stt_adapter = None
        self.embedding_adapter = None
        
        if self.is_edge:
            print(f"\033[92m[lumi-edge] Initializing EdgeManager with provider: {self.provider}\033[0m")
            self._init_adapters()

    def _init_adapters(self):
        """Lazy-loads edge-optimized adapters."""
        try:
            self.stt_adapter = EdgeSTTAdapter(model_size="base")
            self.embedding_adapter = EdgeEmbeddingAdapter()
        except Exception as e:
            print(f"\033[91m[lumi-edge] Error initializing edge adapters: {e}\033[0m")

    def get_transcriber(self):
        """Returns the active STT transcriber (edge or standard fallback)."""
        if self.is_edge and self.stt_adapter:
            return self.stt_adapter
        return None

    def get_embedder(self):
        """Returns the active embedding adapter."""
        if self.is_edge and self.embedding_adapter:
            return self.embedding_adapter
        return None

# Singleton instance for system-wide access
edge_runtime = EdgeManager()