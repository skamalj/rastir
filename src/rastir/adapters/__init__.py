"""Rastir adapters — response normalization and metadata extraction.

All adapters are auto-registered on import.
"""

from rastir.adapters.anthropic import AnthropicAdapter
from rastir.adapters.bedrock import BedrockAdapter
from rastir.adapters.fallback import FallbackAdapter
from rastir.adapters.langchain import LangChainAdapter
from rastir.adapters.openai import OpenAIAdapter
from rastir.adapters.registry import register
from rastir.adapters.retrieval import RetrievalAdapter
from rastir.adapters.tool import ToolAdapter

# Register all adapters (order doesn't matter — priority-based resolution)
register(LangChainAdapter())  # framework, priority 250
register(OpenAIAdapter())  # provider, priority 150
register(AnthropicAdapter())  # provider, priority 150
register(BedrockAdapter())  # provider, priority 140
register(RetrievalAdapter())  # provider, priority 50
register(ToolAdapter())  # provider, priority 10
register(FallbackAdapter())  # fallback, priority 0
