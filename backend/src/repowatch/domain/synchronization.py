from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RequiredCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=250)
    source: str = Field(min_length=1, max_length=250)
    kind: Literal["check", "status"] = "check"
    ref: Literal["head", "merge", "base"] = "head"
    accepted: list[Literal["success", "neutral", "skipped"]] = Field(
        default_factory=lambda: ["success"]
    )


class SynchronizationPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    active_seconds: int = Field(default=7200, strict=True, ge=60, le=2592000)
    idle_seconds: int = Field(default=86400, strict=True, ge=60, le=2592000)
    lookback_seconds: int = Field(default=604800, strict=True, ge=60, le=31536000)
    overlap_seconds: int = Field(default=300, strict=True, ge=1, le=86400)
    full_refresh_seconds: int = Field(default=604800, strict=True, ge=60, le=2592000)
    ci_configured: bool = Field(default=False, strict=True)
    required_checks: list[RequiredCheck] = Field(default_factory=list, max_length=100)
