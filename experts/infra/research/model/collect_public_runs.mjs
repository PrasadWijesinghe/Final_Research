// Collect a new, outcome-stratified batch of public GitHub Actions runs.
// New failed runs are always left unlabelled until independent human review.
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { classifyEvidence } from './refine_public_evidence.mjs';
import {
  workflowSignals, patchSignals, branchCategory, provisionalClassification,
  stableId, cleanWorkflowPath, mapLimit, sanitizeLog,
} from '../../../../../../Dataset/collect_github_actions_dataset.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const research = path.resolve(here, '..');
const workspace = path.resolve(here, '../../../../../..');
const input = path.join(workspace, 'Dataset/github_actions_dataset_3000_state.json');
const output = path.join(research, 'data/raw/github_actions_public_batch_20261028.json');
const repositories = [
  {code:'R21', name:'microsoft/playwright'},
  {code:'R22', name:'storybookjs/storybook'},
  {code:'R23', name:'remix-run/react-router'},
  {code:'R24', name:'vitejs/vite'},
  {code:'R25', name:'supabase/supabase'},
  {code:'R26', name:'calcom/cal.com'},
  {code:'R27', name:'payloadcms/payload'},
];
// DEFAULT: 12 distinct-head runs per outcome/repository. The batch is deliberately
// failure-enriched for case discovery, and must not be used for prevalence estimates.
const perOutcome = 12;
const headers = {Accept:'application/vnd.github+json', 'User-Agent':'PipelineIQ-Infra-Research'};
if (process.env.GITHUB_TOKEN) headers.Authorization = `Bearer ${process.env.GITHUB_TOKEN}`;
let apiRequests = 0;
let apiRemaining = null;
const collectionErrors = [];
async function api(route) {
  const response = await fetch(`https://api.github.com${route}`, {
    headers, signal: AbortSignal.timeout(30000),
  });
  apiRequests++;
  apiRemaining = Number(response.headers.get('x-ratelimit-remaining'));
  if (!response.ok) throw new Error(`GitHub API ${response.status}: ${route}`);
  return response.json();
}
async function publicText(url) {
  try {
    const response = await fetch(url, {
      headers: {'User-Agent':'PipelineIQ-Infra-Research'}, signal: AbortSignal.timeout(30000),
    });
    return response.ok ? await response.text() : '';
  } catch { return ''; }
}
function safe(text) {
  return sanitizeLog(text)
    .replace(/\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b/gi, '[REDACTED_EMAIL]')
    .replace(/\b(?:\d{1,3}\.){3}\d{1,3}\b/g, '[REDACTED_IP]')
    .slice(0, 3000);
}
function plain(s) {
  return s.replace(/<[^>]*>/g,' ')
    .replace(/&#x([0-9a-f]+);/gi,(_,n)=>String.fromCodePoint(parseInt(n,16)))
    .replace(/&#(\d+);/g,(_,n)=>String.fromCodePoint(+n))
    .replace(/&amp;/g,'&').replace(/&lt;/g,'<').replace(/&gt;/g,'>')
    .replace(/&quot;/g,'"').replace(/&#39;/g,"'").replace(/\s+/g,' ').trim();
}
function score(run) {
  const name = `${run.name} ${run.path}`;
  return /docker|container|image|deploy|release|publish|infra|kubernetes/i.test(name) ? 1 : 0;
}
const source = JSON.parse(await fs.readFile(input, 'utf8'));
const existing = new Set(source.records.map(r => r.run_key));
const added = [];
const additions = [];
let nextId = Math.max(...source.records.map(r => Number(r.record_id))) + 1;
for (const repo of repositories) {
  let meta;
  try { meta = await api(`/repos/${repo.name}`); }
  catch (error) { collectionErrors.push(String(error.message)); continue; }
  let count = 0;
  for (const outcome of ['failure','success']) {
    let listing;
    try { listing = await api(`/repos/${repo.name}/actions/runs?status=${outcome}&per_page=100`); }
    catch (error) { collectionErrors.push(String(error.message)); continue; }
    const seenHeads = new Set();
    const candidates = (listing.workflow_runs || []).filter(run =>
      run.conclusion === outcome && ['push','pull_request','pull_request_target','workflow_dispatch'].includes(run.event)
      && run.path && run.head_sha && !existing.has(stableId('RUN',`${repo.name}:${run.id}:${run.run_attempt||1}`))
    ).sort((a,b) => score(b)-score(a));
    for (const run of candidates) {
      if (seenHeads.has(run.head_sha)) continue;
      seenHeads.add(run.head_sha);
      const key = stableId('RUN',`${repo.name}:${run.id}:${run.run_attempt||1}`);
      existing.add(key);
      const classification = provisionalClassification(run.conclusion, '', '', '');
      added.push({
        record_id:nextId++, run_key:key, sampling_stratum:`Public outcome-stratified ${outcome}`,
        repository_group:repo.code, repository:repo.name, repository_url:`https://github.com/${repo.name}`,
        run_id:run.id, run_attempt:run.run_attempt||1, incident_id:stableId('INC',`${repo.name}:${run.head_sha}`),
        workflow_id:run.workflow_id, workflow_name:run.name||'', workflow_file:cleanWorkflowPath(run.path),
        trigger_event:run.event, head_branch:run.head_branch, head_sha:run.head_sha,
        head_repository:run.head_repository?.full_name||repo.name, run_created_at_utc:run.created_at,
        run_started_at_utc:run.run_started_at||run.created_at, run_updated_at_utc:run.updated_at,
        run_conclusion:run.conclusion, run_url:run.html_url, executed_job_count:null,
        runner_labels_observed:'', failed_job:'', failed_job_id:null, failed_step:'', error_evidence:'',
        manual_category:'', manual_subtype:'', reviewer_evidence:'', ...classification,
        is_pull_request:['pull_request','pull_request_target'].includes(run.event)?'Yes':'No',
        is_scheduled:'No', is_default_branch:run.head_branch===meta.default_branch?'Yes':'No',
        branch_category:branchCategory(run.head_branch,meta.default_branch),
        // The sampled history is outcome-stratified, so its rates are deliberately unknown.
        repo_prior_runs_20:null, repo_prior_failure_rate_20:null, workflow_prior_runs_10:null,
        workflow_prior_failure_rate_10:null, hours_since_previous_workflow_run:null,
        collection_cohort:'Public outcome-stratified 2026-10-28', collected_at_utc:new Date().toISOString(),
        collection_notes:'Outcome-stratified sample, not population prevalence. Head workflow is a proxy and may differ from execution. History is unavailable. Failed cause requires independent human review.',
        evidence_status:outcome==='success'?'Observed run conclusion':'Failure cause unresolved',
      });
      count++;
      if (seenHeads.size >= perOutcome) break;
    }
  }
  additions.push({repository:repo.name, added:count});
  console.log(`${repo.name}: ${count} runs; GitHub API remaining ${apiRemaining}`);
}

let completed = 0;
await mapLimit(added, 6, async r => {
  const workflowURL = `https://raw.githubusercontent.com/${r.head_repository}/${r.head_sha}/${r.workflow_file.split('/').map(encodeURIComponent).join('/')}`;
  const patchURL = `https://github.com/${r.head_repository}/commit/${r.head_sha}.patch`;
  const [workflow, patch, html] = await Promise.all([
    publicText(workflowURL), publicText(patchURL),
    r.run_conclusion==='failure' ? publicText(r.run_url) : Promise.resolve(''),
  ]);
  const validWorkflow = workflow && !/^\s*<!doctype html/i.test(workflow) ? workflow : '';
  const validPatch = patch.includes('diff --git') ? patch : '';
  Object.assign(r, workflowSignals(validWorkflow), patchSignals(validPatch), {
    workflow_snapshot_status:validWorkflow?'Captured at head SHA; regex feature extraction':'Unavailable at head SHA',
    patch_status:validPatch?'Captured; head commit patch':'Unavailable or empty patch',
    workflow_source_url:workflowURL, patch_source_url:patchURL, feature_collection_version:'4.0',
  });
  if (html) {
    const blocks = [...html.matchAll(/<annotation-message\b[^>]*>([\s\S]*?)<\/annotation-message>/gi)];
    const annotations = [...new Set(blocks.map(match => {
      const content = match[1].match(/<div[^>]*data-target="annotation-message\.annotationContainer"[^>]*>([\s\S]*?)(?:<button|$)/i);
      return safe(plain(content ? content[1] : match[1]));
    }).filter(Boolean))].slice(0, 10);
    r.error_evidence = annotations.join('\n');
    if (annotations.length) r.evidence_status = 'Public annotations captured; attempt and cause require review';
    Object.assign(r, classifyEvidence(r.error_evidence));
  }
  completed++;
  if (completed % 25 === 0) console.log(`Captured pre-run inputs and available evidence: ${completed}/${added.length}`);
});

// Original rows are preserved byte-for-byte in their fields and retain their stable Record_IDs.
const limitations = [
  'New cases are outcome-stratified, failure-enriched and not representative of population prevalence.',
  'All newly collected failure targets remain blank until independent human verification.',
  'Public annotations can include attempts other than the recorded attempt; verify attempt and root cause.',
  'Head workflow and commit patch are proxies; the actual execution revision and merge context may differ.',
  'Historical rates for the new stratum are unknown because an outcome-stratified sample is biased.',
  'Observed success is a negative control, not proof every conditional job ran.',
  ...(source.scope_audit?.limitations || []),
];
const result = {...source, schema_version:'4.0', collected_at_utc:new Date().toISOString(),
  repositories:[...source.repositories, ...repositories.filter(r=>additions.some(a=>a.repository===r.name))],
  sampling_strategy:'Preserved original records; added failure-enriched public workflow runs from new repositories. New failures remain unlabelled.',
  records:[...source.records, ...added],
  scope_audit:{original_rows:source.records.length, repository_additions:additions, limitations,
    assessment:{distinct_incidents:new Set([...source.records,...added].map(r=>r.incident_id)).size},
    api_requests:apiRequests, api_remaining:apiRemaining, collection_errors:collectionErrors},
};
await fs.mkdir(path.dirname(output),{recursive:true});
await fs.writeFile(output, JSON.stringify(result,null,2), {encoding:'utf8',flag:'wx'});
console.log(JSON.stringify({output,original:source.records.length,added:added.length,
  successes:added.filter(r=>r.run_conclusion==='success').length,
  failures_pending_review:added.filter(r=>r.run_conclusion==='failure').length,
  failures_with_annotations:added.filter(r=>r.run_conclusion==='failure'&&r.error_evidence).length,
  workflow_snapshots:added.filter(r=>r.workflow_snapshot_status.startsWith('Captured')).length,
  patches:added.filter(r=>r.patch_status.startsWith('Captured')).length,
  apiRequests,apiRemaining,collectionErrors},null,2));
