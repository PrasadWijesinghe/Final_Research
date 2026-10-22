"""Shared contracts between the four experts and the shared services.

Import from here, never from another expert's package:
    from shared.contracts import CommitContext, RiskOutput, RoutedFailure, Diagnosis
"""

from .commit_context import ChangedFile, CommitContext, FileSnapshot, FileTag, WorkflowJob
from .common import SCHEMA_VERSION, Expert, FailureCategory, RepoRef
from .diagnosis import (
    BehaviourDetails,
    BuildDetails,
    BuildLocation,
    DependencyDetails,
    Diagnosis,
    EvidenceLine,
    InfraDetails,
    Remediation,
    RetrievedCase,
)
from .failure_label import FailureLabel
from .review import ReviewDecision, VerifiedCase
from .risk_output import RiskFactor, RiskOutput
from .routed_failure import FailedStep, RoutedFailure

# Top-level messages, in pipeline order. Used by export_schemas.py.
MESSAGES = [
    CommitContext,
    RiskOutput,
    RoutedFailure,
    Diagnosis,
    ReviewDecision,
    VerifiedCase,
    FailureLabel,
]

__all__ = [
    "SCHEMA_VERSION",
    "MESSAGES",
    "Expert",
    "FailureCategory",
    "RepoRef",
    "CommitContext",
    "ChangedFile",
    "FileSnapshot",
    "FileTag",
    "WorkflowJob",
    "RiskOutput",
    "RiskFactor",
    "RoutedFailure",
    "FailedStep",
    "Diagnosis",
    "EvidenceLine",
    "RetrievedCase",
    "Remediation",
    "DependencyDetails",
    "BuildDetails",
    "BuildLocation",
    "BehaviourDetails",
    "InfraDetails",
    "ReviewDecision",
    "VerifiedCase",
    "FailureLabel",
]
