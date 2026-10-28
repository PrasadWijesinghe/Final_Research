"""Label rules of docs/experts/test.md section 3, as set-based SQL (P4.3 v1).

One label per test job attempt. All rules run inside one transaction over temp tables, so a run
either replaces the member's old rule labels completely or changes nothing (idempotent).

Universe (# DEFAULT): passing jobs had no log parsed in P4.2, so a "test job" is any job whose
series (repo, workflow_path, job name falling back to job_key) has at least one attempt that
`testing.log_parses` identified as a test job. Skipped jobs are left out. Attempts whose conclusion
is success, failure or cancelled are in.

Labels (sub_label, rule_name, confidence):
  pass      first_attempt_pass        high  attempt 1 passed; no attempt of the job on SHA failed
  flaky     rerun_pass_same_sha       high  failed; another attempt or run passed on the same SHA
  flaky     silent_docs_only_next     low   failed, never passed; a child commit changed docs only
                                            and the job passed on it
  genuine   fix_commit_touches_file   high  failed, never passed on the SHA; a commit within 7 days
                                            touching the failing test file made it pass
  unclear   unclear, no_log           low   anything else
  not_test  cancelled, non_test_log   low   cancelled, or the log shows no test runner
A passing attempt that follows a failure on the same SHA is a rerun pass and gets no label.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Connection, text

log = logging.getLogger(__name__)

# DEFAULT: v1 cut, "unrelated files" for the silent-flaky rule means docs and project meta files.
DOCS_ONLY_REGEX = (
    r"(\.(md|markdown|mdx|txt|rst|adoc)$|^docs?/|"
    r"(^|/)(LICENSE|LICENCE|CHANGELOG|CODEOWNERS|README)[^/]*$|"
    r"^\.github/(ISSUE_TEMPLATE|PULL_REQUEST_TEMPLATE|FUNDING))"
)
# DEFAULT: 7 days is the window from test.md section 3.
FIX_WINDOW_DAYS = 7
SCHEMA_VERSION = "1.1.0"  # shared.contracts.SCHEMA_VERSION; copied into mirrored payloads

LABELS = ("pass", "flaky", "genuine", "unclear", "not_test")
MIRRORED = ("flaky", "genuine", "unclear")  # failed attempts only; pass / not_test stay local

_STEPS: list[tuple[str, str]] = [
    (
        "series",
        """
create temp table lab_series on commit drop as
select distinct w.repo_id, coalesce(w.workflow_path, '') as wpath,
       coalesce(j.name, j.job_key, '') as jname
from testing.log_parses p
join raw.repos r on r.full_name = p.repo and r.member = :member
join raw.jobs j on j.job_id = p.job_id
join raw.workflow_runs w on w.run_id = j.run_id and w.attempt = j.attempt
where p.is_test_job
""",
    ),
    (
        "attempts",
        """
create temp table lab_att on commit drop as
select j.job_id, j.run_id, j.attempt, w.repo_id, r.full_name as repo, w.head_sha as sha,
       w.head_branch as branch, s.wpath, s.jname, j.conclusion as concl,
       w.created_at as run_created,
       (p.job_id is not null) as p_exists, coalesce(p.is_test_job, false) as p_test,
       l.storage_path
from raw.jobs j
join raw.workflow_runs w on w.run_id = j.run_id and w.attempt = j.attempt
join raw.repos r on r.id = w.repo_id and r.member = :member
join lab_series s on s.repo_id = w.repo_id and s.wpath = coalesce(w.workflow_path, '')
                 and s.jname = coalesce(j.name, j.job_key, '')
left join testing.log_parses p on p.job_id = j.job_id
left join raw.logs l on l.job_id = j.job_id
where j.conclusion in ('success', 'failure', 'cancelled')
""",
    ),
    ("idx_att", "create index on lab_att (repo_id, wpath, jname, sha)"),
    ("idx_att2", "create index on lab_att (repo_id, branch, wpath, jname)"),
    (
        "groups",
        """
create temp table lab_grp on commit drop as
select repo_id, wpath, jname, sha,
       count(*) filter (where concl = 'success') as n_ok,
       count(*) filter (where concl = 'failure') as n_fail,
       string_agg(case concl when 'success' then 'S' when 'failure' then 'F' else 'C' end, ''
                  order by run_created, attempt) as seq
from lab_att group by 1, 2, 3, 4
""",
    ),
    ("idx_grp", "create index on lab_grp (repo_id, wpath, jname, sha)"),
    (
        "failing_files",
        # DEFAULT: P4.2 stores no stack frames, so only the failing test file (failed rows of
        # testing.test_results) is used for the genuine rule, not files from the stack trace.
        """
create temp table lab_ff on commit drop as
select distinct a.repo_id, a.wpath, a.jname, a.sha, t.file,
       regexp_replace(t.file, '^.*/', '') as base
from lab_att a join testing.test_results t on t.job_id = a.job_id
where a.concl = 'failure' and a.p_test and t.outcome = 'failed' and coalesce(t.file, '') <> ''
""",
    ),
    (
        "commit_files",
        """
create temp table lab_cf on commit drop as
select cf.repo_id, cf.sha, cf.path, regexp_replace(cf.path, '^.*/', '') as base,
       c.committed_at
from raw.commit_files cf join raw.commits c on c.repo_id = cf.repo_id and c.sha = cf.sha
where cf.repo_id in (select distinct repo_id from lab_att)
""",
    ),
    ("idx_cf", "create index on lab_cf (repo_id, base)"),
    ("idx_cf2", "create index on lab_cf (repo_id, sha)"),
    (
        "failing_groups",
        # Groups of failed test-job attempts that never passed on their SHA; hts = commit time.
        """
create temp table lab_fg on commit drop as
select distinct g.repo_id, g.wpath, g.jname, g.sha, a.branch,
       coalesce(c.committed_at, a.run_created) as hts
from lab_grp g
join lab_att a on a.repo_id = g.repo_id and a.wpath = g.wpath and a.jname = g.jname
              and a.sha = g.sha and a.concl = 'failure' and a.p_test
left join raw.commits c on c.repo_id = g.repo_id and c.sha = g.sha
where g.n_ok = 0
""",
    ),
    (
        "silent",
        # Child commit(s) touched only docs files and the job passed on that child.
        """
create temp table lab_silent on commit drop as
select distinct fg.repo_id, fg.wpath, fg.jname, fg.sha, ch.sha as next_sha
from lab_fg fg
join raw.commits ch on ch.repo_id = fg.repo_id and fg.sha = any(ch.parent_shas)
where exists (select 1 from lab_cf f where f.repo_id = ch.repo_id and f.sha = ch.sha)
  and not exists (select 1 from lab_cf f where f.repo_id = ch.repo_id and f.sha = ch.sha
                  and f.path !~ :docs_regex)
  and exists (select 1 from lab_att s where s.repo_id = fg.repo_id and s.wpath = fg.wpath
              and s.jname = fg.jname and s.sha = ch.sha and s.concl = 'success')
""",
    ),
    (
        "genuine",
        # A commit on the same branch within the window touched a failing file, the job had not
        # passed on the branch before that commit, and a passing run came at or after it.
        """
create temp table lab_gen on commit drop as
select fg.repo_id, fg.wpath, fg.jname, fg.sha,
       min(cf.sha) as fix_sha, min(ff.file) as fix_file
from lab_fg fg
join lab_ff ff on ff.repo_id = fg.repo_id and ff.wpath = fg.wpath and ff.jname = fg.jname
              and ff.sha = fg.sha
join lab_cf cf on cf.repo_id = fg.repo_id and cf.base = ff.base
              and (cf.path = ff.file or right(cf.path, length(ff.file) + 1) = '/' || ff.file
                   or right(ff.file, length(cf.path) + 1) = '/' || cf.path)
              and cf.committed_at > fg.hts
              and cf.committed_at <= fg.hts + make_interval(days => :window)
where exists (select 1 from lab_att x where x.repo_id = cf.repo_id and x.sha = cf.sha
              and x.branch = fg.branch)
  and exists (select 1 from lab_att s where s.repo_id = fg.repo_id and s.branch = fg.branch
              and s.wpath = fg.wpath and s.jname = fg.jname and s.concl = 'success'
              and s.run_created >= cf.committed_at
              and s.run_created <= fg.hts + make_interval(days => :window))
  and not exists (select 1 from lab_att s where s.repo_id = fg.repo_id and s.branch = fg.branch
                  and s.wpath = fg.wpath and s.jname = fg.jname and s.concl = 'success'
                  and s.run_created > fg.hts and s.run_created < cf.committed_at)
group by 1, 2, 3, 4
""",
    ),
    ("idx_gen", "create index on lab_gen (repo_id, wpath, jname, sha)"),
    ("idx_sil", "create index on lab_silent (repo_id, wpath, jname, sha)"),
    (
        "outcome",
        """
create temp table lab_out on commit drop as
select a.job_id, a.run_id, a.attempt, a.repo, a.sha, a.branch, a.wpath, a.jname,
  case
    when a.concl = 'cancelled' then 'not_test'
    when a.concl = 'success' then 'pass'
    when not a.p_exists then 'unclear'
    when not a.p_test then 'not_test'
    when g.n_ok > 0 then 'flaky'
    when si.next_sha is not null then 'flaky'
    when ge.fix_sha is not null then 'genuine'
    else 'unclear' end as sub_label,
  case
    when a.concl = 'cancelled' then 'cancelled'
    when a.concl = 'success' then 'first_attempt_pass'
    when not a.p_exists then 'no_log'
    when not a.p_test then 'non_test_log'
    when g.n_ok > 0 then 'rerun_pass_same_sha'
    when si.next_sha is not null then 'silent_docs_only_next'
    when ge.fix_sha is not null then 'fix_commit_touches_file'
    else 'unclear' end as rule_name,
  case
    when a.concl = 'success' then 'high'
    when a.concl = 'failure' and a.p_test and g.n_ok > 0 then 'high'
    when a.concl = 'failure' and a.p_test and si.next_sha is not null then 'low'
    when a.concl = 'failure' and a.p_test and ge.fix_sha is not null then 'high'
    else 'low' end as confidence,
  jsonb_strip_nulls(jsonb_build_object(
    'sha', a.sha, 'branch', a.branch, 'workflow_path', a.wpath, 'job_name', a.jname,
    'same_sha_outcomes', g.seq, 'next_commit', si.next_sha,
    'fix_commit', ge.fix_sha, 'fix_file', ge.fix_file)) as evidence,
  a.storage_path
from lab_att a
join lab_grp g on g.repo_id = a.repo_id and g.wpath = a.wpath and g.jname = a.jname
              and g.sha = a.sha
left join (select repo_id, wpath, jname, sha, min(next_sha) as next_sha
           from lab_silent group by 1, 2, 3, 4) si
       on si.repo_id = a.repo_id and si.wpath = a.wpath and si.jname = a.jname
      and si.sha = a.sha and a.concl = 'failure' and g.n_ok = 0
left join lab_gen ge on ge.repo_id = a.repo_id and ge.wpath = a.wpath and ge.jname = a.jname
                    and ge.sha = a.sha and a.concl = 'failure' and g.n_ok = 0
-- a pass that follows a failure on the same SHA is a rerun pass: no label
where not (a.concl = 'success' and (a.attempt <> 1 or g.n_fail > 0))
""",
    ),
]


_TEMP_TABLES = (
    "lab_out", "lab_gen", "lab_silent", "lab_fg", "lab_cf", "lab_ff", "lab_grp", "lab_att",
    "lab_series",
)  # fmt: skip


def run_rules(conn: Connection, member: str) -> dict[str, int]:
    """Compute and store the labels for `member`'s repos. Replaces earlier rule labels."""
    params: dict[str, Any] = {
        "member": member,
        "docs_regex": DOCS_ONLY_REGEX,
        "window": FIX_WINDOW_DAYS,
    }
    for table in _TEMP_TABLES:  # allow a second run inside one transaction (tests)
        conn.execute(text(f"drop table if exists {table}"))
    for name, sql in _STEPS:
        log.info("label step", extra={"step": name})
        conn.execute(text(sql), params)
    in_member = (
        "select j.job_id from raw.jobs j join raw.workflow_runs w on w.run_id = j.run_id "
        "and w.attempt = j.attempt join raw.repos r on r.id = w.repo_id where r.member = :member"
    )
    conn.execute(
        text(f"delete from testing.labels where labelled_by = 'rule' and job_id in ({in_member})"),
        params,
    )
    conn.execute(
        text(
            "delete from platform.failure_labels where category = 'test' and labelled_by = 'rule' "
            f"and job_id in ({in_member})"
        ),
        params,
    )
    conflict = (
        "exists (select 1 from platform.failure_labels f "
        "where f.job_id = o.job_id and f.category <> 'test')"
    )
    # DEFAULT: a label is final unless another category claimed the job (DATA.md section 6) or
    # the label is "unclear" (that one waits for manual review).
    conn.execute(
        text(f"""
insert into testing.labels (run_id, attempt, job_id, sub_label, rule_name, confidence,
                            labelled_by, is_final, payload)
select o.run_id, o.attempt, o.job_id, o.sub_label, o.rule_name, o.confidence, 'rule',
       not ({conflict}) and o.sub_label <> 'unclear',
       o.evidence || jsonb_build_object('repo', o.repo, 'claimed_by_other_category', {conflict})
from lab_out o
"""),
        params,
    )
    conn.execute(
        text(f"""
insert into platform.failure_labels (run_id, attempt, job_id, category, sub_label, labelled_by,
                                     is_final, payload)
select o.run_id, o.attempt, o.job_id, 'test', o.sub_label, 'rule',
       not ({conflict}) and o.sub_label <> 'unclear',
       jsonb_build_object(
         'schema_version', cast(:schema_version as text),
         'repo', jsonb_build_object('owner', split_part(o.repo, '/', 1),
                                    'name', split_part(o.repo, '/', 2)),
         'run_id', o.run_id, 'run_attempt', o.attempt, 'job_id', o.job_id,
         'category', 'test', 'sub_label', o.sub_label, 'labelled_by', 'rule',
         'rule_name', o.rule_name, 'labeller_hash', null, 'confidence', o.confidence,
         'notes', null, 'labelled_at', cast(:labelled_at as text))
from lab_out o where o.sub_label in ('flaky', 'genuine', 'unclear')
"""),
        {
            **params,
            "schema_version": SCHEMA_VERSION,
            "labelled_at": datetime.now(UTC).isoformat(),
        },
    )
    return universe_counts(conn)


def universe_counts(conn: Connection) -> dict[str, int]:
    """Attempts covered by the universe and how many got a label (must run after run_rules)."""
    row = conn.execute(
        text(
            "select (select count(*) from lab_series), (select count(*) from lab_att), "
            "(select count(*) from lab_att where concl = 'success'), "
            "(select count(*) from lab_att where concl = 'failure'), "
            "(select count(*) from lab_att where concl = 'cancelled'), "
            "(select count(*) from lab_out)"
        )
    ).one()
    keys = ("series", "attempts", "success", "failure", "cancelled", "labelled")
    return {k: int(v) for k, v in zip(keys, row, strict=True)}
