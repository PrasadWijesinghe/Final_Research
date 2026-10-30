import { AsyncView, Empty, Page } from "../components/ui";
import { ReviewForm } from "../components/ReviewForm";
import { useAuth } from "../lib/auth";
import { api } from "../lib/api";
import { pct, shortSha, when } from "../lib/risk";
import { Link } from "../lib/router";
import { useAsync } from "../lib/useAsync";
import type { Diagnosis, FailureView } from "../types/api";
import type { BehaviourDetails } from "../types/contracts.gen";

const VERDICT_BADGE: Record<BehaviourDetails["verdict"], string> = { genuine: "high", flaky: "medium", unclear: "low" };

function TriagePanel({ t }: { t: BehaviourDetails }) {
  return (
    <div className="pq-triage">
      <p>
        <span className={`pq-badge pq-${VERDICT_BADGE[t.verdict]}`}>{t.verdict}</span>{" "}
        <b>{pct(t.verdict_confidence)}</b> sure
        {t.suggest_rerun && <> · <span className="pq-badge pq-low">re-run suggested</span></>}
      </p>
      <p className="pq-dim pq-small">
        {t.framework} · changed code in stack: {t.stack_touches_changed_code ? "yes" : "no"}
        {t.pre_run_flaky_risk != null && <> · pre-run flaky risk {pct(t.pre_run_flaky_risk)}</>}
      </p>
      <p className="pq-small">Failing tests: {t.failing_tests.slice(0, 5).map((n, i) => <code key={i}>{n}{i < Math.min(t.failing_tests.length, 5) - 1 ? ", " : ""}</code>)}{t.failing_tests.length > 5 && ` +${t.failing_tests.length - 5} more`}</p>
    </div>
  );
}

function DiagnosisCard({ d, view, onSaved }: { d: Diagnosis; view: FailureView; onSaved: () => void }) {
  const { auth } = useAuth();
  const reviews = view.reviews.filter((r) => r.diagnosis_id === d.diagnosis_id);
  return (
    <article className="pq-card">
      <header>
        <strong>{d.expert} expert</strong>
        <span className={`pq-badge pq-${d.status === "diagnosed" ? "low" : d.status === "error" ? "high" : "medium"}`}>{d.status}</span>
      </header>
      {d.status === "error" && <p className="pq-err">{d.error_message}</p>}
      {d.status === "misrouted" && <p>This expert says the failure belongs to <b>{d.suggested_category}</b>.</p>}
      {d.status === "diagnosed" && (
        <>
          {d.details?.expert === "test" && <TriagePanel t={d.details} />}
          <p className="pq-cause">{d.root_cause}</p>
          <p className="pq-dim pq-small">confidence {pct(d.confidence)} · {d.llm_model ?? "no model"} · {when(d.created_at)}</p>
          <h4>Evidence</h4>
          <ul className="pq-evidence">
            {(d.evidence ?? []).map((e, i) => (
              <li key={i}><span className="pq-tag">{e.source}</span> <code>{e.text}</code>{e.path && <span className="pq-mono pq-dim"> {e.path}{e.line ? `:${e.line}` : ""}</span>}</li>
            ))}
          </ul>
          {d.remediation && (
            <>
              <h4>Suggested fix</h4>
              <p>{d.remediation.summary}</p>
              {(d.remediation.steps ?? []).length > 0 && <ol>{(d.remediation.steps ?? []).map((s, i) => <li key={i}>{s}</li>)}</ol>}
              {d.remediation.suggested_patch && <pre className="pq-pre">{d.remediation.suggested_patch}</pre>}
            </>
          )}
          {(d.retrieved_cases ?? []).length > 0 && (
            <>
              <h4>Similar past cases</h4>
              <ul>{(d.retrieved_cases ?? []).map((c) => <li key={c.case_id}>{c.summary} <span className="pq-dim">({pct(c.similarity)}) fix: {c.fix_summary}</span></li>)}</ul>
            </>
          )}
        </>
      )}
      {reviews.length > 0 && (
        <>
          <h4>Past reviews</h4>
          <ul>
            {reviews.map((r) => (
              <li key={r.decision_id}>
                <b>{r.decision}</b> {r.cause_confirmed ? "(cause confirmed)" : "(cause not confirmed)"}
                {r.seconds_to_decide != null && ` after ${r.seconds_to_decide}s`}
                {r.comment && <span className="pq-dim"> - {r.comment}</span>}
                <span className="pq-dim pq-small"> {when(r.decided_at)}</span>
              </li>
            ))}
          </ul>
        </>
      )}
      {d.status === "diagnosed" && auth.status === "authed" && (
        <ReviewForm diagnosis={d} reviewerHash={auth.me.user_hash} onSaved={onSaved} />
      )}
    </article>
  );
}

/** Diagnoses grouped per expert, newest first; a re-run adds a diagnosis instead of replacing one. */
function byExpert(diagnoses: Diagnosis[]): Diagnosis[][] {
  const groups = new Map<string, Diagnosis[]>();
  for (const d of [...diagnoses].sort((a, b) => b.created_at.localeCompare(a.created_at))) {
    groups.set(d.expert, [...(groups.get(d.expert) ?? []), d]);
  }
  return [...groups.values()];
}

export default function FailureDetail({ id }: { id: string }) {
  const [state, reload] = useAsync(() => api.failure(id), [id]);
  return (
    <Page title="Failure" kicker="Diagnosis and review" actions={<Link to="/failures" className="pq-btn">All failures</Link>}>
      <AsyncView state={state} reload={reload} what="Loading failure">
        {(v) => (
          <>
            <section className="pq-card">
              <header>
                <strong>{v.failure.job_name}</strong>
                <span className="pq-badge pq-medium">{v.failure.predicted_category} ({pct(v.failure.routing_confidence)})</span>
              </header>
              <p className="pq-dim pq-small">
                {v.failure.repo.owner}/{v.failure.repo.name} · <Link to={`/commits/${v.failure.head_sha}?repo=${encodeURIComponent(`${v.failure.repo.owner}/${v.failure.repo.name}`)}`}>{shortSha(v.failure.head_sha)}</Link> · run {v.failure.run_id} attempt {v.failure.run_attempt} · sent to {v.failure.sent_to.join(", ")}
              </p>
              <p className="pq-small">Failed steps: {v.failure.failed_steps.map((s) => `${s.number}. ${s.name} (${s.conclusion})`).join("; ")}</p>
              <pre className="pq-pre">{v.failure.log_excerpt}</pre>
            </section>
            {v.diagnoses.length === 0 ? (
              <Empty title="No diagnosis yet" hint="The expert is still working, or has not answered." />
            ) : (
              <div className="pq-cards">
                {byExpert(v.diagnoses).map(([latest, ...older]) => (
                  <div key={latest.diagnosis_id}>
                    <DiagnosisCard d={latest} view={v} onSaved={reload} />
                    {older.length > 0 && (
                      <details className="pq-older">
                        <summary className="pq-dim pq-small">Earlier diagnoses from the {latest.expert} expert ({older.length})</summary>
                        {older.map((d) => <DiagnosisCard key={d.diagnosis_id} d={d} view={v} onSaved={reload} />)}
                      </details>
                    )}
                  </div>
                ))}
              </div>
            )}
          </>
        )}
      </AsyncView>
    </Page>
  );
}
