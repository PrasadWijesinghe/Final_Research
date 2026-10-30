import { AsyncView, Empty, Page, RiskCell } from "../components/ui";
import { api } from "../lib/api";
import { hashQuery } from "../lib/router";
import { pct, when } from "../lib/risk";
import { Link } from "../lib/router";
import { useAsync } from "../lib/useAsync";

export default function CommitPage({ sha }: { sha: string }) {
  const repo = hashQuery(window.location.hash).get("repo") ?? undefined;
  const [state, reload] = useAsync(() => api.commitRisks(sha, repo), [sha, repo]);
  return (
    <Page title={`Commit ${sha.slice(0, 10)}`} kicker={repo ?? "Risks for one commit"} actions={<Link to="/" className="pq-btn">Back</Link>}>
      <AsyncView
        state={state}
        reload={reload}
        what="Loading risks"
        isEmpty={(d) => d.risks.length === 0}
        empty={<Empty title="No predictions for this commit" hint="It may not have been pushed through PipelineIQ yet." />}
      >
        {(d) => (
          <div className="pq-cards">
            {d.risks.map((r) => (
              <article className="pq-card" key={r.prediction_id}>
                <header>
                  <strong>{r.expert}{r.job_key ? ` / ${r.job_key}` : ""}</strong>
                  <RiskCell risk={r} />
                </header>
                <p className="pq-dim pq-small">
                  probability {pct(r.probability)} vs threshold {pct(r.threshold)} · model {r.model_version} · {when(r.created_at)}
                  {r.status !== "ok" && ` · ${r.status}${r.error_message ? `: ${r.error_message}` : ""}`}
                </p>
                {Object.keys(r.sub_risks ?? {}).length > 0 && (
                  <p className="pq-small">
                    {Object.entries(r.sub_risks ?? {}).map(([k, v]) => `${k} ${pct(v as number)}`).join(" · ")}
                  </p>
                )}
                {(r.top_factors ?? []).length > 0 && (
                  <ul className="pq-factors">
                    {(r.top_factors ?? []).map((f) => (
                      <li key={f.feature}>
                        <span className={f.direction === "raises" ? "pq-up" : "pq-down"}>{f.direction === "raises" ? "raises" : "lowers"}</span>{" "}
                        {f.reason}{f.path ? <span className="pq-mono pq-dim"> {f.path}{f.line ? `:${f.line}` : ""}</span> : null}
                      </li>
                    ))}
                  </ul>
                )}
              </article>
            ))}
          </div>
        )}
      </AsyncView>
    </Page>
  );
}
