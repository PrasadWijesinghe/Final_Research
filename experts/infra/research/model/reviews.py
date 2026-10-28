"""Apply independently reviewed labels without trusting unreviewed failure labels."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd

from shared.sanitise import sanitise


def apply_reviews(source: pd.DataFrame, path: Path, source_sha256: str) -> pd.DataFrame:
    """Retain observed successes and failure labels with complete human review evidence."""
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    reviewed = [record for record in records if record.get("human_verified") is True]
    if not reviewed:
        raise ValueError("review file has no independently human-verified labels")
    result = source.copy()
    failed = ~result.Label_Status.eq("Observed success")
    result.loc[failed, "Infra_Failure"] = float("nan")
    seen = set()
    for record in reviewed:
        run_key = record.get("run_key")
        if not isinstance(run_key, str) or run_key in seen:
            raise ValueError("review records must have unique run keys")
        seen.add(run_key)
        if record.get("source_sha256") != source_sha256:
            raise ValueError(f"review source checksum mismatch for {run_key}")
        rows = result.index[result.Run_Key.eq(run_key)].tolist()
        if len(rows) != 1 or not failed.loc[rows[0]]:
            raise ValueError(f"review does not match one failure candidate: {run_key}")
        target = record.get("verified_target")
        if type(target) is not int or target not in (0, 1):
            raise ValueError(f"verified target must be integer 0 or 1 for {run_key}")
        for name in ("reviewer_id", "reviewed_at_utc", "evidence_url", "sanitised_evidence"):
            value = record.get(name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} is required for {run_key}")
            if sanitise(value).text != value:
                raise ValueError(f"{name} must be sanitised for {run_key}")
        url = urlparse(record["evidence_url"])
        if url.scheme != "https" or url.hostname != "github.com" or url.username or url.password:
            raise ValueError(f"evidence URL must be a public GitHub HTTPS link for {run_key}")
        timestamp = pd.to_datetime(record["reviewed_at_utc"], errors="coerce", utc=True)
        if pd.isna(timestamp):
            raise ValueError(f"review date must be a valid UTC timestamp for {run_key}")
        if record.get("workflow_revision_verified") is not True:
            raise ValueError(f"workflow revision must be verified for {run_key}")
        subtype = record.get("verified_subtype")
        if target == 1 and (not isinstance(subtype, str) or not subtype.strip()):
            raise ValueError(f"positive label requires a verified subtype for {run_key}")
        result.loc[rows[0], "Infra_Failure"] = target
        result.loc[rows[0], "Label_Status"] = "Independently human reviewed"
        if isinstance(subtype, str) and subtype.strip():
            result.loc[rows[0], "Infra_Subtype"] = subtype.strip()
    return result
