"""Store build rule labels in the shared `platform.failure_labels` table (DATA.md section 6)."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import Engine, delete
from sqlalchemy.orm import Session

from shared.contracts import FailureLabel
from shared.db import models as m

# DEFAULT: rows per DELETE/INSERT batch.
CHUNK = 5_000


class SqlLabelSink:
    """Replaces the Build rules' labels for the jobs of one dataset build.

    Only rows with category "build" and labelled_by "rule" are touched, so manual labels and
    other members' labels stay. Rows are written with is_final=false: a job claimed by two
    members goes to manual review (DECISIONS D15), and that review sets the final row.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def replace_build_labels(self, job_ids: Sequence[int], labels: Sequence[FailureLabel]) -> int:
        """Delete this rule set's earlier rows for `job_ids`, then insert `labels`."""
        ids = sorted(set(job_ids))
        with Session(self._engine) as s, s.begin():
            for i in range(0, len(ids), CHUNK):
                s.execute(
                    delete(m.FailureLabelRow).where(
                        m.FailureLabelRow.job_id.in_(ids[i : i + CHUNK]),
                        m.FailureLabelRow.category == "build",
                        m.FailureLabelRow.labelled_by == "rule",
                    )
                )
            for i in range(0, len(labels), CHUNK):
                s.add_all(
                    m.FailureLabelRow(
                        run_id=lb.run_id,
                        attempt=lb.run_attempt,
                        job_id=lb.job_id,
                        category=lb.category.value,
                        sub_label=lb.sub_label,
                        labelled_by=lb.labelled_by,
                        is_final=False,
                        payload=lb.model_dump(mode="json"),
                    )
                    for lb in labels[i : i + CHUNK]
                )
        return len(labels)
