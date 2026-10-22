"""The only expert-specific spot of this package (the rest is identical for all four experts)."""

import os

from shared.contracts import Expert, InfraDetails, RoutedFailure
from shared.contracts.diagnosis import ExpertDetails

# --- Expert-specific values (copied from experts/_template) ----------------------------------
EXPERT = Expert.INFRA  # which expert this is; also names the queue `diagnose:<value>`
DEFAULT_PORT = 8104  # DEFAULT: docs/experts/README.md table (dependency 8101 ... infra 8104)
DB_SCHEMA = "infra"  # this expert's Postgres schema (docker/postgres/init/01-init.sql)


def make_details(failure: RoutedFailure) -> ExpertDetails:
    """Build this expert's detail model for a stub diagnosis (fixed values)."""
    return InfraDetails(area="other")


# --------------------------------------------------------------------------------------------

MODEL_VERSION = "stub"
QUEUE_NAME = f"diagnose:{EXPERT.value}"


def port() -> int:
    """Port the API listens on, from EXPERT_PORT."""
    return int(os.environ.get("EXPERT_PORT", str(DEFAULT_PORT)))
