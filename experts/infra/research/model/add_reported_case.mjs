// Add a source-linked, independently human-reviewable public failure candidate.
import fs from 'node:fs/promises';
import {
  workflowSignals, patchSignals, branchCategory, stableId, cleanWorkflowPath,
} from '../../../../../../Dataset/collect_github_actions_dataset.mjs';

const filename = process.argv[2];
if (!filename) throw new Error('provide a batch JSON path');
const state = JSON.parse(await fs.readFile(filename, 'utf8'));
const repo = 'open-telemetry/opentelemetry-js';
const runId = 26234999606;
const headers = {Accept:'application/vnd.github+json', 'User-Agent':'PipelineIQ-Infra-Research'};
if (process.env.GITHUB_TOKEN) headers.Authorization = `Bearer ${process.env.GITHUB_TOKEN}`;
async function get(url, json = false) {
  const response = await fetch(url, {headers, signal:AbortSignal.timeout(30000)});
  if (!response.ok) return null;
  return json ? response.json() : response.text();
}
const [run,meta] = await Promise.all([
  get(`https://api.github.com/repos/${repo}/actions/runs/${runId}`,true),
  get(`https://api.github.com/repos/${repo}`,true),
]);
if (!run || run.conclusion !== 'failure') throw new Error('public failure metadata unavailable');
const runKey = stableId('RUN',`${repo}:${run.id}:${run.run_attempt||1}`);
if (state.records.some(r=>r.run_key===runKey)) throw new Error('run already in dataset');
const workflowPath = cleanWorkflowPath(run.path);
const headRepo = run.head_repository?.full_name || repo;
const workflowURL = `https://raw.githubusercontent.com/${headRepo}/${run.head_sha}/${workflowPath.split('/').map(encodeURIComponent).join('/')}`;
const patchURL = `https://github.com/${headRepo}/commit/${run.head_sha}.patch`;
const [workflow,patch] = await Promise.all([get(workflowURL),get(patchURL)]);
const validWorkflow = workflow && !/^\s*<!doctype html/i.test(workflow) ? workflow : '';
const validPatch = patch?.includes('diff --git') ? patch : '';
const issue = 'https://github.com/open-telemetry/community/issues/3461';
const record = {
  record_id:Math.max(...state.records.map(r=>Number(r.record_id)))+1, run_key:runKey,
  sampling_stratum:'Public issue-reported failure; not random', repository_group:'R28',
  repository:repo, repository_url:`https://github.com/${repo}`, run_id:run.id,
  run_attempt:run.run_attempt||1, incident_id:stableId('INC',`${repo}:${run.head_sha}`),
  workflow_id:run.workflow_id, workflow_name:run.name||'', workflow_file:workflowPath,
  trigger_event:run.event, head_branch:run.head_branch, head_sha:run.head_sha,head_repository:headRepo,
  run_created_at_utc:run.created_at,run_started_at_utc:run.run_started_at||run.created_at,
  run_updated_at_utc:run.updated_at,run_conclusion:run.conclusion,run_url:run.html_url,
  executed_job_count:null, runner_labels_observed:'',failed_job:'',failed_job_id:null,failed_step:'',
  error_evidence:`Public issue reports no space left on device for linked job: ${issue}`,
  reviewer_evidence:'',manual_category:'',manual_subtype:'',
  category:'Infrastructure & Deployment',subtype:'Runner storage capacity',provisional_infra:1,
  final_infra:null,label_status:'Needs manual review',
  is_pull_request:['pull_request','pull_request_target'].includes(run.event)?'Yes':'No',
  is_scheduled:run.event==='schedule'?'Yes':'No',
  is_default_branch:meta ? (run.head_branch===meta.default_branch?'Yes':'No') : null,
  branch_category:meta ? branchCategory(run.head_branch,meta.default_branch) : 'Unknown',
  repo_prior_runs_20:null,repo_prior_failure_rate_20:null,workflow_prior_runs_10:null,
  workflow_prior_failure_rate_10:null,hours_since_previous_workflow_run:null,
  ...workflowSignals(validWorkflow),...patchSignals(validPatch),
  workflow_snapshot_status:validWorkflow?'Captured at head SHA; regex feature extraction':'Unavailable at head SHA',
  patch_status:validPatch?'Captured; head commit patch':'Unavailable or empty patch',
  workflow_source_url:workflowURL,patch_source_url:patchURL,feature_collection_version:'4.0',
  collected_at_utc:new Date().toISOString(),collection_cohort:'Public issue case 2026-10-28',
  evidence_status:'Maintainer issue links specific failed job; independent human review pending',
  collection_notes:`Issue evidence: ${issue}. Review job/attempt and executed workflow revision independently. Issue-selected case; not representative. Historical features unknown.`,
};
state.records.push(record);
state.repositories.push({code:'R28',name:repo});
state.scope_audit.repository_additions.push({repository:repo,added:1});
state.scope_audit.limitations.push('One issue-reported case is intentionally selected for evidence and is not a random failure sample.');
state.sampling_strategy += ' Includes one issue-reported runner storage case from open-telemetry/opentelemetry-js.';
await fs.writeFile(filename,JSON.stringify(state,null,2));
console.log(JSON.stringify({record_id:record.record_id,repository:repo,run_url:record.run_url,
  issue,workflow_captured:!!validWorkflow,patch_captured:!!validPatch,final_label:null},null,2));
