"""Configuration: env-driven Settings + YAML-driven AppConfig.

Two layers, loaded once at startup, both validated fail-fast:

- ``Settings`` (pydantic-settings) — credentials and deployment knobs from env.
- ``AppConfig`` (frozen Pydantic models) — behavior from ``defaults.yaml``:
  column mapping, agent defaults, flags, routing indicators.

A user pointing Ctxora at their own key-value database changes only
these two sources. If a use case forces editing Python, that is a design bug.
"""

from functools import lru_cache
from pathlib import Path
from typing import ClassVar, Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_CONFIG_PATH: Path = Path(__file__).parent / "defaults.yaml"


class ConfigError(Exception):
    """Raised when the YAML config cannot be parsed or fails validation."""

    def __init__(self, path: Path, detail: str) -> None:
        """Capture the offending file path and a human-readable detail."""
        self.path: Path = path
        self.detail: str = detail
        super().__init__(f"invalid config {path}: {detail}")


class Settings(BaseSettings):
    """Environment-driven settings. Mutable by design (pydantic-settings)."""

    model_config: ClassVar[SettingsConfigDict] = SettingsConfigDict(env_file=".env", extra="ignore")

    # Metadata store (sessions, history, knowledge, feedback, vectors)
    metadata_db_host: str = "localhost"
    metadata_db_port: int = 5432
    metadata_db_name: str = "ctxora"
    metadata_db_user: str = "ctxora"
    metadata_db_password: str = ""

    # Telemetry store (engine depends on YAML stores.telemetry.adapter)
    telemetry_db_host: str | None = None
    telemetry_db_port: int = 8123
    telemetry_db_name: str = "ctxora"
    telemetry_db_user: str | None = None
    telemetry_db_password: str | None = None

    # LLM (any OpenAI-compatible endpoint)
    llm_api_key: str | None = None
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4o-mini"
    embedding_model: str = "text-embedding-3-small"

    # Admin surface
    feedback_admin_token: str | None = None

    # Auth: AUTH_DISABLED=true keeps dev mode (tenant field, warned); false
    # requires a verified JWT carrying TENANT_CLAIM.
    auth_disabled: bool = True
    tenant_claim: str = "tenant"
    jwt_secret: str | None = None
    jwt_issuer: str | None = None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached process-wide settings (tests construct Settings directly)."""
    return Settings()


# ─── YAML models ────────────────────────────────────────────────────────────
# All frozen and extra-forbidding: a typo in the YAML must fail at boot,
# never silently produce a half-configured service.


class ColumnMapping(BaseModel):
    """Maps logical EAV roles onto physical column names."""

    """Maps logical EAV roles onto the user's physical column names."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    table: str
    tenant_column: str | None = None
    timestamp: str
    entity_id: str
    key: str
    value: str
    extra_dimensions: tuple[str, ...] = ()


class EventsMapping(BaseModel):
    """Maps logical event roles onto physical column names."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    table: str
    tenant_column: str | None = None
    timestamp: str
    event_type: str
    entity_id: str
    payload: str


AdapterName = Literal["clickhouse", "postgres"]


class TelemetryStoreConfig(BaseModel):
    """Telemetry adapter choice plus its column mapping."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    adapter: AdapterName
    mapping: ColumnMapping


class EventsConfig(BaseModel):
    """Events store: optional, disabled by default."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = False
    adapter: AdapterName = "clickhouse"
    mapping: EventsMapping | None = None


class StoresConfig(BaseModel):
    """All storage bindings: telemetry (required), events (optional)."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    telemetry: TelemetryStoreConfig
    events: EventsConfig = EventsConfig()


class AgentConfig(BaseModel):
    """Agent behavior defaults: caps, timeouts, aggregation defaults."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    default_time_window: str = "today"
    row_cap: int = 1000
    query_timeout_s: int = 30
    aggregation_defaults: dict[str, str] = {"*": "average"}
    digest_turn_threshold: int = 10
    title_keywords: tuple[str, ...] = (
        "speed",
        "rpm",
        "temperature",
        "battery",
        "fuel",
        "voltage",
        "location",
    )


class RagConfig(BaseModel):
    """Document RAG behavior: chunking, retrieval, upload limits, advisor."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    chunk_size: int = 800
    chunk_overlap: int = 120
    top_k: int = 5
    max_upload_mb: int = 20
    shared_scope: str = "shared"
    advisor_template: str = (
        "Analyze this incident and explain: probable causes, consequences, "
        "actions, checklist, and whether operation can continue."
    )


class RatelimitConfig(BaseModel):
    """Per-tenant in-memory token bucket."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    requests_per_minute: int = 60
    burst: int = 10


class Flags(BaseModel):
    """Feature flags; every post-v0.1 stage defaults off."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    correction_loop: bool = False
    assume_first: bool = False
    session_digest: bool = False
    streaming: bool = True
    semantic_examples: bool = False
    feedback_capture: bool = False
    followup: bool = False
    greeting: bool = False
    ratelimit: bool = False


class RoutingConfig(BaseModel):
    """Keyword indicators for intent routing (SQL vs RAG)."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    sql_indicators: tuple[str, ...] = (
        "average",
        "maximum",
        "minimum",
        "last",
        "latest",
        "yesterday",
        "today",
        "trend",
        "per",
        "which",
        "when",
        "how many",
        "how much",
        "speed",
        "rpm",
        "temperature",
        "voltage",
        "battery",
        "fuel",
    )
    rag_indicators: tuple[str, ...] = (
        "manual",
        "how do i",
        "specification",
        "troubleshooting",
        "policy",
        "maintenance",
        "acceptable range",
        "what should i do",
        "consequences",
    )


class AppConfig(BaseModel):
    """Root of the YAML behavior config."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    stores: StoresConfig
    agent: AgentConfig = AgentConfig()
    rag: RagConfig = RagConfig()
    ratelimit: RatelimitConfig = RatelimitConfig()
    flags: Flags = Flags()
    routing: RoutingConfig = RoutingConfig()


def _parse_yaml_file(path: Path) -> object:
    """Parse YAML from disk; raise ConfigError on syntax errors.

    ``yaml.safe_load`` is untyped upstream, so its result is narrowed to
    ``object`` at this single boundary and isinstance-checked by the caller.
    """
    try:
        document: object = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        detail = f"cannot parse YAML: {exc}"
        raise ConfigError(path, detail) from exc
    return document


def load_app_config(path: Path = DEFAULT_CONFIG_PATH) -> AppConfig:
    """Parse and validate the YAML config; raise ConfigError with detail."""
    parsed = _parse_yaml_file(path)
    if not isinstance(parsed, dict):
        detail = "top level must be a mapping"
        raise ConfigError(path, detail)

    try:
        return AppConfig.model_validate(parsed)
    except ValidationError as exc:
        fields = ", ".join(".".join(str(p) for p in err["loc"]) for err in exc.errors())
        detail = f"validation failed for: {fields}"
        raise ConfigError(path, detail) from exc
