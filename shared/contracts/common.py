"""Shared building blocks for every contract.

Change rule: anything in shared/contracts/ needs approval from all four members.
Bump SCHEMA_VERSION on every change (major = breaking, minor = new optional field).
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "1.1.0"


class Part(BaseModel):
    """Base for nested objects: unknown fields are rejected and objects are immutable."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ContractModel(Part):
    """Base for top-level messages passed between services. Carries the schema version."""

    schema_version: str = SCHEMA_VERSION


class Expert(str, Enum):
    DEPENDENCY = "dependency"  # Member 1
    BUILD = "build"  # Member 2
    TEST = "test"  # Member 3
    INFRA = "infra"  # Member 4


class FailureCategory(str, Enum):
    """The shared failure taxonomy. One primary category per failed job."""

    DEPENDENCY = "dependency"
    BUILD = "build"
    TEST = "test"
    INFRA = "infra"
    OTHER = "other"  # a real failure outside the four categories
    UNKNOWN = "unknown"  # not enough evidence to decide


Probability = Annotated[float, Field(ge=0.0, le=1.0)]
CommitSha = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
# Salted SHA-256 of a GitHub login. Never store the raw login.
PersonHash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
# Pointer to a file in shared storage, e.g. "logs/mongoose/123456/7.log.gz".
StorageRef = Annotated[str, Field(min_length=1, max_length=500)]


class RepoRef(Part):
    owner: str
    name: str

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
