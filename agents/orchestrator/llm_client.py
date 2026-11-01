"""The decision model for every agent: qwen3:14b served by Ollama.

The model is treated as untrusted: it only sees trusted system prompts plus masked, wrapped data,
and everything it returns is checked by code (allowlists, schemas, the Output Validator).
"""

from __future__ import annotations

from langchain_ollama import ChatOllama

from agents.config import get_settings


def make_llm(*, think: bool = False, temperature: float | None = None) -> ChatOllama:
    s = get_settings()
    return ChatOllama(
        model=s.model,
        base_url=s.ollama_url,
        temperature=s.temperature if temperature is None else temperature,
        num_ctx=s.num_ctx,
        reasoning=think,          # qwen3 "thinking" off by default: faster, cleaner tool calls
    )
