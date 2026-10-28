// Turns the API's PredictResponse into readable lines. Pure functions, no I/O.

const pct = (p) => `${Math.round(p * 100)}%`;

/** True when at least one ready expert raised an alert. */
export function hasAlerts(response) {
  return alertRisks(response).length > 0;
}

function alertRisks(response) {
  return (response?.results ?? [])
    .filter((r) => r.state === "ready")
    .flatMap((r) => r.risks ?? [])
    .filter((risk) => risk.alert);
}

/** Lines to print for a response; the first line is a header. */
export function formatReport(response) {
  const lines = [];
  const alerts = alertRisks(response);
  if (alerts.length === 0) {
    lines.push("PipelineIQ: no risks flagged for this push.");
  } else {
    lines.push(`PipelineIQ: ${alerts.length} risk${alerts.length === 1 ? "" : "s"} flagged for this push`);
    for (const r of alerts) {
      const where = r.scope === "job" ? ` (${r.workflow_path} / ${r.job_key})` : "";
      lines.push(`  ! ${r.expert}${where}: ${pct(r.probability)} chance of failure (alert at ${pct(r.threshold)})`);
      for (const f of (r.top_factors ?? []).slice(0, 3)) {
        const at = f.path ? ` [${f.path}${f.line ? `:${f.line}` : ""}]` : "";
        lines.push(`      - ${f.reason}${at}`);
      }
    }
  }
  const notReady = (response?.results ?? []).filter((r) => r.state !== "ready");
  for (const r of notReady) {
    lines.push(`  (${r.expert}: not ready${r.reason ? `, ${r.reason}` : ""})`);
  }
  return lines;
}
