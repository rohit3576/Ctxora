"""Request/response models: the generic envelope every endpoint returns."""

from typing import ClassVar, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict

T = TypeVar("T")


class Envelope(BaseModel, Generic[T]):
    """Uniform response wrapper (blueprint doc section 14)."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    status: Literal["Success", "Failure"]
    message: str
    data: T | None = None


class HealthLive(BaseModel):
    """Liveness payload: process is up; no database involved."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    status: Literal["ok"]


class HealthReady(BaseModel):
    """Readiness payload: dependencies answer."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    status: Literal["ready", "unavailable"]
    metadata_db: Literal["ok", "unreachable"]
