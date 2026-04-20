from __future__ import annotations

import textwrap
from typing import Sequence

from openai import AzureOpenAI

from agent_cv.config import settings

# Chunk size in characters (≈ 300 tokens at 4 chars/token)
CHUNK_SIZE = 1200
CHUNK_OVERLAP = 200


def _get_client() -> AzureOpenAI:
    return AzureOpenAI(
        azure_endpoint=settings.azure_openai_endpoint,
        api_key=settings.azure_openai_api_key,
        api_version=settings.azure_openai_api_version,
    )


def chunk_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Split text into overlapping character chunks."""
    if not text or not text.strip():
        return []
    text = " ".join(text.split())  # normalise whitespace
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        chunks.append(text[start:end])
        if end == len(text):
            break
        start += chunk_size - overlap
    return chunks


def embed_texts(texts: Sequence[str]) -> list[list[float]]:
    """Return embeddings for a batch of texts using Azure OpenAI."""
    if not texts:
        return []
    client = _get_client()
    response = client.embeddings.create(
        input=list(texts),
        model=settings.azure_openai_embedding_deployment,
    )
    return [item.embedding for item in response.data]
