"""OpenAI-compatible relay used for optional Russian copy and image generation."""

from .client import AiRelayClient, AiRelayError

__all__ = ["AiRelayClient", "AiRelayError"]
