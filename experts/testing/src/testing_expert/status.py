"""What is real and what is a fallback in this process (P4.12): shown by /health and the worker."""

from __future__ import annotations

import os
from dataclasses import dataclass

from .kb.retrieve import count_cases
from .rag.gateway import gateway_url
from .serving import get_bundle
from .serving import resolve_bundle_path as resolve_model_path
from .triage.runtime import get_model
from .triage.runtime import resolve_bundle_path as resolve_triage_path


@dataclass(frozen=True)
class Readiness:
    """The parts of the expert that are loaded, and the one summary word for them."""

    model_version: str | None  # pre-run model bundle, None = /predict answers with the stub
    triage_version: str | None  # triage bundle, None = the diagnosis is the stub
    kb_cases: int  # 0 = no database or an empty knowledge base
    llm_gateway: bool  # URL set (the gateway may still be down or run the mock provider)
    full_logs: bool  # DATA_DIR set, so triage can read the full log instead of the excerpt

    @property
    def mode(self) -> str:
        """`real` (model and triage loaded), `partial` (one of them) or `stub` (neither)."""
        loaded = (self.model_version is not None) + (self.triage_version is not None)
        return ("stub", "partial", "real")[loaded]


def readiness(load: bool = True) -> Readiness:
    """Look at the bundles, the knowledge base and the environment.

    `load=True` loads the bundles (the API process does, once). `load=False` only looks for the
    folders and never loads a model: the RQ worker forks a child per job, and a LightGBM model
    loaded in the parent before the fork deadlocks the child (OpenMP threads do not survive a
    fork), so the worker banner must not load anything.
    """
    if load:
        bundle = get_bundle()
        triage = get_model()
        model_version = bundle.version if bundle else None
        triage_version = triage[1] if triage else None
    else:
        model_path = resolve_model_path()
        triage_path = resolve_triage_path()
        model_version = f"lightgbm-{model_path.name}" if model_path else None
        triage_version = f"triage-{triage_path.name}" if triage_path else None
    return Readiness(
        model_version=model_version,
        triage_version=triage_version,
        kb_cases=count_cases(),
        llm_gateway=gateway_url() is not None,
        full_logs=bool(os.environ.get("DATA_DIR", "").strip()),
    )
