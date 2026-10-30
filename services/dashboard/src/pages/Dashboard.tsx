import { useMemo } from "react";
import { AsyncView, Empty, Page, RiskCell } from "../components/ui";
import { api } from "../lib/api";
import { alertsOf, EXPERTS, outcomeOf, shortSha, topFor, when } from "../lib/risk";
import { Link } from "../lib/router";
import { useAsync } from "../lib/useAsync";
import type { RunItem } from "../types/api";

function Tiles({ runs }: { runs: RunItem[] }) {
  const s = useMemo(() => {
    const finished = runs.filter((r) => outcomeOf(r) !== "pending");
    return {
      total: runs.length,
      alerted: runs.filter((r) => alertsOf(r).length > 0).length,
      failed: finished.filter((r) => outcomeOf(r) === "failed").length,
      finished: finished.length,
    };
  }, [runs]);
  return (
    <div className="pq-tiles">
      <div className="pq-tile"><span>Recent runs</span><strong>{s.total}</strong></div>
      <div className="pq-tile"><span>With an alert</span><strong>{s.alerted}</strong></div>
      <div className="pq-tile"><span>Failed (of finished)</span><strong>{s.failed}<small>/{s.finished}</small></strong></div>
    </div>
  );
}

export default function Dashboard() {
  const [state, reload] = useAsync(() => api.runs(undefined, 25), []);
  return (
    <Page title="Risk dashboard" kicker="Latest predictions" actions={<button className="pq-btn" onClick={reload}>Refresh</button>}>
      <AsyncView
        state={state}
        reload={reload}
        what="Loading recent runs"
        isEmpty={(r) => r.length === 0}
        empty={<Empty title="No runs yet" hint="Push a commit with the pre-push hook installed, or wait for a webhook." />}
      >
        {(runs) => (
          <>
            <Tiles runs={runs} />
            <p className="pq-note">Each expert predicts a different event, so the percentages are not added together.</p>
            <div className="pq-scroll">
              <table className="pq-table">
                <thead>
                  <tr>
                    <th>Commit</th>
                    <th>Repo</th>
                    {EXPERTS.map((e) => <th key={e}>{e}</th>)}
                    <th>Top alerts</th>
                  </tr>
                </thead>
                <tbody>
                  {runs.map((r) => (
                    <tr key={r.context_id}>
                      <td>
                        <Link to={`/commits/${r.head_sha}?repo=${encodeURIComponent(r.repo)}`} className="pq-mono">{shortSha(r.head_sha)}</Link>
                        <div className="pq-dim pq-small">{when(r.history_cutoff)}</div>
                      </td>
                      <td>{r.repo}</td>
                      {EXPERTS.map((e) => <td key={e}><RiskCell risk={topFor(r.predictions, e)} /></td>)}
                      <td className="pq-small">
                        {alertsOf(r).length === 0
                          ? <span className="pq-dim">none</span>
                          : alertsOf(r).map((a) => a.top_factors?.[0]?.reason ?? `${a.expert} alert`).slice(0, 2).join("; ")}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </AsyncView>
    </Page>
  );
}
