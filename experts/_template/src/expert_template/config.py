"""The only expert-specific spot: a copy of this package changes just this file (plus its names).

Everything else in the package is identical for all four experts.
"""

import os

from shared.contracts import DependencyDetails, Expert, RoutedFailure
from shared.contracts.diagnosis import ExpertDetails

# --- Change these when copying the template -------------------------------------------------
EXPERT = Expert.DEPENDENCY  # which expert this is; also names the queue `diagnose:<value>`
DEFAULT_PORT = 8101  # DEFAULT: docs/experts/README.md table (dependency 8101 ... infra 8104)
DB_SCHEMA = "dependency"  # this expert's Postgres schema (docker/postgres/init/01-init.sql)


def make_details(failure: RoutedFailure) -> ExpertDetails:
    """Build this expert's detail model for a stub diagnosis (fixed values)."""
    return DependencyDetails(kind="other")


# --------------------------------------------------------------------------------------------

MODEL_VERSION = "stub"
QUEUE_NAME = f"diagnose:{EXPERT.value}"


def port() -> int:
    """Port the API listens on, from EXPERT_PORT."""
    return int(os.environ.get("EXPERT_PORT", str(DEFAULT_PORT)))
