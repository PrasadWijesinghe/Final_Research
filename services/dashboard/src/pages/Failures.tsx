import { AsyncView, Empty, Page } from "../components/ui";
import { api } from "../lib/api";
import { shortSha, when } from "../lib/risk";
import { Link } from "../lib/router";
import { useAsync } from "../lib/useAsync";

export default function Failures() {
  const [state, reload] = useAsync(() => api.failures(undefined, 100), []);
  return (
    <Page title="Failures" kicker="Diagnoses waiting for review">
      <AsyncView
        state={state}
        reload={reload}
        what="Loading failures"
        isEmpty={(f) => f.length === 0}
        empty={<Empty title="No failures recorded" hint="Failed runs show up here once the router has sent them to an expert." />}
      >
        {(items) => (
          <div className="pq-scroll">
            <table className="pq-table">
              <thead>
                <tr><th>When</th><th>Repo</th><th>Commit</th><th>Job</th><th>Expert</th><th>Diagnoses</th><th>Review</th></tr>
              </thead>
              <tbody>
                {items.map((f) => (
                  <tr key={f.failure_id}>
                    <td className="pq-small">{when(f.created_at)}</td>
                    <td>{f.repo}</td>
                    <td className="pq-mono">{shortSha(f.head_sha)}</td>
                    <td><Link to={`/failures/${f.failure_id}`}>{f.job_name}</Link></td>
                    <td>{f.expert}</td>
                    <td>{f.diagnosis_count}</td>
                    <td>{f.latest_review ?? <span className="pq-dim">not reviewed</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </AsyncView>
    </Page>
  );
}
