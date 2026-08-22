"""OpenAI-compatible LLM client over httpx2 (chat + embeddings).

Responses cross the trust boundary exactly once: Pydantic models parse
them into typed values; everything downstream is typed.
"""

import json
import socket
from collections.abc import Sequence
from typing import ClassVar, Final

import httpx2
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from config.settings import Settings
from llm.client import GenResult

_LIMITS: Final = httpx2.Limits(
    max_connections=50, max_keepalive_connections=20, keepalive_expiry=60.0
)
_TIMEOUT: Final = httpx2.Timeout(connect=10.0, read=120.0, write=10.0, pool=10.0)
_SOCKET_OPTIONS: Final = [(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)]
_HTTP_OK: Final = 200


class LLMError(Exception):
    """The LLM endpoint failed or returned an unusable payload."""

    def __init__(self, detail: str) -> None:
        """Describe the failure."""
        self.detail: str = detail
        super().__init__(detail)


class _Message(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="ignore")

    content: str


class _Choice(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="ignore")

    message: _Message


class _Usage(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="ignore")

    prompt_tokens: int = 0
    completion_tokens: int = 0


class _ChatPayload(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="ignore")

    choices: list[_Choice] = Field(min_length=1)
    usage: _Usage | None = None


class _EmbeddingItem(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="ignore")

    embedding: list[float]


class _EmbeddingsPayload(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="ignore")

    data: list[_EmbeddingItem]


def _client(base_url: str, api_key: str) -> httpx2.Client:
    transport = httpx2.HTTPTransport(
        http2=True, retries=2, limits=_LIMITS, socket_options=_SOCKET_OPTIONS
    )
    return httpx2.Client(
        transport=transport,
        timeout=_TIMEOUT,
        base_url=base_url,
        headers={"Authorization": f"Bearer {api_key}"},
        follow_redirects=True,
    )


class OpenAICompatibleClient:
    """Sync client for any OpenAI-compatible /chat/completions + /embeddings."""

    def __init__(self, settings: Settings) -> None:
        """Bind endpoint, model names, and credentials from settings."""
        self.settings: Settings = settings

    def generate(self, system: str, user: str, *, temperature: float) -> GenResult:
        """One chat completion returning raw content with token counts."""
        body: dict[str, object] = {
            "model": self.settings.llm_model,
            "temperature": temperature,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        try:
            payload = _ChatPayload.model_validate_json(self._post("/chat/completions", body))
        except ValidationError as exc:
            detail = f"unparseable chat response: {exc.error_count()} errors"
            raise LLMError(detail) from exc
        usage = payload.usage
        return GenResult(
            sql="",
            raw=payload.choices[0].message.content,
            prompt_tokens=usage.prompt_tokens if usage else 0,
            completion_tokens=usage.completion_tokens if usage else 0,
        )

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """One embedding vector per input text."""
        body: dict[str, object] = {"model": self.settings.embedding_model, "input": list(texts)}
        try:
            payload = _EmbeddingsPayload.model_validate_json(self._post("/embeddings", body))
        except ValidationError as exc:
            detail = f"unparseable embeddings response: {exc.error_count()} errors"
            raise LLMError(detail) from exc
        vectors = [item.embedding for item in payload.data]
        if len(vectors) != len(texts):
            detail = f"expected {len(texts)} embeddings, got {len(vectors)}"
            raise LLMError(detail)
        return vectors

    def _post(self, path: str, body: dict[str, object]) -> str:
        api_key = self.settings.llm_api_key
        if not api_key:
            detail = "LLM_API_KEY is not configured"
            raise LLMError(detail)
        with _client(self.settings.llm_base_url, api_key) as client:
            response = client.post(
                path, content=json.dumps(body), headers={"Content-Type": "application/json"}
            )
        if response.status_code != _HTTP_OK:
            detail = f"LLM endpoint returned {response.status_code}: {response.text[:200]}"
            raise LLMError(detail)
        return response.text
