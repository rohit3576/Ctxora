"""LLM provider presets and embeddings request-body construction."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from config.settings import Settings
from llm.openai_compat import embed_body


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run from an empty CWD so the repo's .env cannot leak into Settings."""
    monkeypatch.chdir(tmp_path)
    for name in ("LLM_PROVIDER", "LLM_BASE_URL", "LLM_MODEL", "EMBEDDING_MODEL"):
        monkeypatch.delenv(name, raising=False)


class TestProviderPresets:
    def test_default_is_plain_openai(self, isolated: None) -> None:
        settings = Settings()

        assert settings.llm_base_url == "https://api.openai.com/v1"
        assert settings.llm_model == "gpt-4o-mini"
        assert settings.embedding_dimensions is None

    def test_gemini_preset_derives_endpoint_models_dimensions(self, isolated: None) -> None:
        settings = Settings(llm_provider="gemini")

        assert settings.llm_base_url == ("https://generativelanguage.googleapis.com/v1beta/openai/")
        assert settings.llm_model == "gemini-2.5-flash"
        assert settings.embedding_model == "gemini-embedding-001"
        assert settings.embedding_dimensions == 1536

    def test_explicit_fields_beat_the_preset(self, isolated: None) -> None:
        settings = Settings(llm_provider="gemini", llm_model="gemini-2.5-pro")

        assert settings.llm_model == "gemini-2.5-pro"
        assert settings.llm_base_url == ("https://generativelanguage.googleapis.com/v1beta/openai/")

    def test_unknown_provider_fails_fast(self, isolated: None) -> None:
        with pytest.raises(ValidationError, match="unknown LLM_PROVIDER"):
            Settings(llm_provider="azure")


class TestEmbedBody:
    def test_dimensions_omitted_by_default(self, isolated: None) -> None:
        settings = Settings()

        body = embed_body(settings, ["hello"])

        assert body == {"model": "text-embedding-3-small", "input": ["hello"]}

    def test_dimensions_included_when_configured(self, isolated: None) -> None:
        settings = Settings(llm_provider="gemini")

        body = embed_body(settings, ["hello", "world"])

        assert body["model"] == "gemini-embedding-001"
        assert body["dimensions"] == 1536
        assert body["input"] == ["hello", "world"]
