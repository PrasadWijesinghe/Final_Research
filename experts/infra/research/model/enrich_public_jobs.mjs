// Add failed-job details and public check annotations to a small review batch.
import fs from 'node:fs/promises';
import { classifyEvidence } from './refine_public_evidence.mjs';
import { sanitizeLog } from '../../../../../../Dataset/collect_github_actions_dataset.mjs';

const filename = process.argv[2];
if (!filename) throw new Error('provide a collected batch JSON path');
const state = JSON.parse(await fs.readFile(filename, 'utf8'));
const added = state.records.slice(state.scope_audit.original_rows);
const repos = [...new Set(added.map(row => row.repository))];
const headers = {Accept:'application/vnd.github+json','User-Agent':'PipelineIQ-Infra-Research'};
if (process.env.GITHUB_TOKEN) headers.Authorization = `Bearer ${process.env.GITHUB_TOKEN}`;
let remaining = null;
let requests = 0;
const failures = [];
async function api(route) {
  const response = await fetch(`https://api.github.com${route}`, {
    headers, signal:AbortSignal.timeout(30000),
  });
  requests++;
  remaining = Number(response.headers.get('x-ratelimit-remaining'));
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}
function safe(value) {
  return sanitizeLog(String(value || ''))
    .replace(/\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b/gi, '[REDACTED_EMAIL]')
    .replace(/\b(?:\d{1,3}\.){3}\d{1,3}\b/g, '[REDACTED_IP]').slice(0,3000);
}
for (const repo of repos) {
  if (remaining !== null && remaining < 5) break;
  const candidates = added.filter(row => row.repository===repo && row.run_conclusion==='failure');
  candidates.sort((a,b) => Number(/docker|container|image|deploy/i.test(b.workflow_name)) -
    Number(/docker|container|image|deploy/i.test(a.workflow_name)));
  const row = candidates[0];
  if (!row) continue;
  try {
    const jobs = await api(`/repos/${repo}/actions/runs/${row.run_id}/attempts/${row.run_attempt}/jobs?per_page=100`);
    row.executed_job_count = jobs.total_count;
    const job = (jobs.jobs || []).find(item => ['failure','timed_out','startup_failure'].includes(item.conclusion));
    if (!job) { failures.push({repo, run_id:row.run_id, error:'No failed job in first page'}); continue; }
    row.failed_job = safe(job.name);
    row.failed_job_id = job.id;
    row.failed_step = safe((job.steps || []).filter(step=>step.conclusion==='failure').map(step=>step.name).join('; '));
    row.runner_labels_observed = (job.labels || []).map(safe).join('; ');
    const prefix = `https://api.github.com/repos/${repo}/check-runs/`;
    const checkId = (job.check_run_url || '').startsWith(prefix) ? job.check_run_url.slice(prefix.length) : '';
    if (/^\d+$/.test(checkId)) {
      try {
        const annotations = await api(`/repos/${repo}/check-runs/${checkId}/annotations?per_page=100`);
        const evidence = [...new Set([row.error_evidence, ...annotations.map(item=>safe(item.message))].filter(Boolean))];
        row.error_evidence = safe(evidence.join('\n'));
        if (row.error_evidence) row.evidence_status = 'Public annotations captured; attempt and cause require review';
        Object.assign(row,classifyEvidence(row.error_evidence));
      } catch (error) { failures.push({repo,run_id:row.run_id,error:`Annotation ${error.message}`}); }
    }
    row.collection_notes += ' Failed-job details observed for recorded attempt. Check annotations may aggregate attempts; verify before labelling.';
    console.log(`${repo}: run ${row.run_id}, failed step captured; API remaining ${remaining}`);
  } catch (error) { failures.push({repo,run_id:row.run_id,error:`Jobs ${error.message}`}); }
}
state.scope_audit.api_requests += requests;
state.scope_audit.api_remaining = remaining;
state.scope_audit.collection_errors.push(...failures.map(item=>`${item.repo} ${item.run_id}: ${item.error}`));
await fs.writeFile(filename,JSON.stringify(state,null,2));
console.log(JSON.stringify({requests,remaining,failed_job_details:added.filter(r=>r.failed_job).length,
  provisional_hints:added.filter(r=>r.provisional_infra===1).length,failures},null,2));
