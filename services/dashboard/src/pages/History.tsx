import { type FormEvent, useState } from "react";
import { AsyncView, Empty, LevelBadge, Page } from "../components/ui";
import { api } from "../lib/api";
import { alertsOf, outcomeOf, pct, shortSha, when } from "../lib/risk";
import { Link } from "../lib/router";
import { useAsync } from "../lib/useAsync";

export default function History() {
  const [repo, setRepo] = useState("");
  const [applied, setApplied] = useState("");
  const [state, reload] = useAsync(() => api.runs(applied || undefined, 100), [applied]);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    setApplied(repo.trim());
  };
  return (
    <Page title="Pipeline history" kicker="Prediction vs actual outcome">
      <form className="pq-filter" onSubmit={submit}>
        <input aria-label="Repository filter" placeholder="owner/name (all repos if empty)" value={repo} onChange={(e) => setRepo(e.target.value)} />
        <button className="pq-btn" type="submit">Filter</button>
      </form>
      <AsyncView
        state={state}
        reload={reload}
        what="Loading history"
        isEmpty={(r) => r.length === 0}
        empty={<Empty title="No runs match" hint={applied ? `Nothing recorded for ${applied}.` : "Nothing recorded yet."} />}
      >
        {(runs) => (
          <div className="pq-scroll">
            <table className="pq-table">
              <thead>
                <tr><th>When</th><th>Repo</th><th>Commit</th><th>Predicted</th><th>Actual</th><th>Match</th></tr>
              </thead>
              <tbody>
                {runs.map((r) => {
                  const alerts = alertsOf(r);
                  const outcome = outcomeOf(r);
                  const max = Math.max(0, ...r.predictions.map((p) => p.probability ?? 0));
                  const match = outcome === "pending" ? null : (alerts.length > 0) === (outcome === "failed");
                  return (
                    <tr key={r.context_id}>
                      <td className="pq-small">{when(r.history_cutoff)}</td>
                      <td>{r.repo}</td>
                      <td><Link to={`/commits/${r.head_sha}?repo=${encodeURIComponent(r.repo)}`} className="pq-mono">{shortSha(r.head_sha)}</Link></td>
                      <td>
                        {r.predictions.length === 0 ? <span className="pq-dim">no prediction</span> : (
                          <LevelBadge level={alerts.length ? "high" : "low"}>
                            {alerts.length ? `alert: ${alerts.map((a) => a.expert).join(", ")}` : "no alert"} (max {pct(max)})
                          </LevelBadge>
                        )}
                      </td>
                      <td>
                        {outcome === "pending" ? <span className="pq-dim">pending</span> : (
                          <LevelBadge level={outcome === "failed" ? "high" : "low"}>{r.run_conclusion}</LevelBadge>
                        )}
                      </td>
                      <td>{match === null ? <span className="pq-dim">-</span> : match ? "yes" : "no"}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </AsyncView>
    </Page>
  );
}
