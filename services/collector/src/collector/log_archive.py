"""Match the jobs of one run attempt to the files of its log archive (D41).

`GET /repos/{o}/{r}/actions/runs/{id}/attempts/{n}/logs` returns a zip. For each job it holds a
top-level file `{n}_{job name}.txt` (and a folder `{job name}/system.txt`, which is ignored). GitHub
strips characters that are illegal in file names, so the job name from the database and the name in
the zip can differ. A job is matched to a file only when the match is certain; the caller fetches
every other job with its own request, so a log can never land on the wrong job.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# Top-level archive entry: `{index}_{job name}.txt`; the index is the job's position in the run.
_ENTRY = re.compile(r"(\d+)_(.+)\.txt", re.DOTALL)


def match_key(name: str | None) -> str:
    """Letters and digits of `name`, lower-cased: what survives GitHub's file-name clean-up."""
    # DEFAULT: drop everything that is not a letter or digit (Unicode aware), so the characters
    # GitHub removes or replaces in file names (`:` `/` `\` `*` `?` `"` `<` `>` `|`, controls,
    # spaces) cannot make the two sides differ. A name with no such character left has no key.
    return "".join(c for c in (name or "").casefold() if c.isalnum())


def top_level_entries(names: Iterable[str]) -> dict[str, list[str]]:
    """Key -> names of the top-level `{n}_{name}.txt` files; folders and other files are ignored."""
    entries: dict[str, list[str]] = {}
    for name in names:
        # A name with a path separator is a folder file (or a job name GitHub did not clean):
        # never trust it, the job just falls back to its own request.
        if "/" in name or "\\" in name:
            continue
        found = _ENTRY.fullmatch(name)
        key = match_key(found.group(2)) if found else ""
        if key:
            entries.setdefault(key, []).append(name)
    return entries


def match_entries(
    pending_ids: Iterable[int],
    attempt_jobs: Iterable[tuple[int, str | None]],
    entries: dict[str, list[str]],
) -> dict[int, str]:
    """Pending job id -> archive entry name, only where both sides are unique for the key.

    `attempt_jobs` must be ALL jobs of the attempt (not only the pending ones): two jobs whose
    names differ only in stripped characters would otherwise look unique while one of them is
    already stored. A key shared by two jobs, or by two archive files, matches nothing.
    """
    jobs = list(attempt_jobs)
    key_of = {job_id: match_key(name) for job_id, name in jobs}
    count: dict[str, int] = {}
    for key in key_of.values():
        count[key] = count.get(key, 0) + 1
    matched: dict[int, str] = {}
    for job_id in pending_ids:
        key = key_of.get(job_id, "")
        if key and count[key] == 1 and len(entries.get(key, [])) == 1:
            matched[job_id] = entries[key][0]
    return matched
