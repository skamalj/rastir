"""Rastir adapters — response normalization and metadata extraction.

All adapters are auto-registered on import.
"""

from rastir.adapters.anthropic import AnthropicAdapter
from rastir.adapters.azure_openai import AzureOpenAIAdapter
from rastir.adapters.bedrock import BedrockAdapter
from rastir.adapters.cohere import CohereAdapter
from rastir.adapters.crewai import CrewAIAdapter
from rastir.adapters.fallback import FallbackAdapter
from rastir.adapters.gemini import GeminiAdapter
from rastir.adapters.groq import GroqAdapter
from rastir.adapters.langchain import LangChainAdapter
from rastir.adapters.langgraph import LangGraphAdapter
from rastir.adapters.llamaindex import LlamaIndexAdapter
from rastir.adapters.mistral import MistralAdapter
from rastir.adapters.openai import OpenAIAdapter
from rastir.adapters.registry import register
from rastir.adapters.retrieval import RetrievalAdapter
from rastir.adapters.tool import ToolAdapter

# Register all adapters (order doesn't matter — priority-based resolution)
register(LangGraphAdapter())     # framework, priority 260
register(LangChainAdapter())     # framework, priority 250
register(LlamaIndexAdapter())   # framework, priority 240
register(CrewAIAdapter())        # framework, priority 245
register(AzureOpenAIAdapter())   # provider, priority 155
register(GroqAdapter())          # provider, priority 152
register(OpenAIAdapter())        # provider, priority 150
register(AnthropicAdapter())     # provider, priority 150
register(GeminiAdapter())        # provider, priority 150
register(CohereAdapter())        # provider, priority 150
register(MistralAdapter())       # provider, priority 150
register(BedrockAdapter())       # provider, priority 140
register(RetrievalAdapter())     # provider, priority 50
register(ToolAdapter())          # provider, priority 10
register(FallbackAdapter())      # fallback, priority 0
