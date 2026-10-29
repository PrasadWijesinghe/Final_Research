// Restrict provisional hints to explicit evidence; final failure labels stay blank.
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

export function classifyEvidence(evidence) {
  const useful = String(evidence || '').split('\n').filter(line =>
    !/ubuntu-latest label will migrate|capacity constraints.*queue times|node\.js \d+ is deprecated/i.test(line)
  ).join('\n');
  const rules = [
    [/no space left on device|disk (?:is )?full|ENOSPC/i, 'Runner storage capacity'],
    [/(?:docker pull failed|failed to pull (?:image|container)|error pulling image|manifest unknown)/i, 'Container pull'],
    [/(?:ENOTFOUND|EAI_AGAIN|could not resolve host|network is unreachable|connection refused)/i, 'Network or external service'],
    [/(?:deployment .* (?:failed|rolled back)|deploy(?:ment)? failed|terraform apply failed)/i, 'Deployment or infrastructure'],
    [/(?:permission denied|unauthorized|forbidden|resource not accessible by integration)/i, 'Permissions or credentials'],
  ];
  const matched = rules.find(([pattern]) => pattern.test(useful));
  return matched
    ? {category:'Infrastructure & Deployment', subtype:matched[1], provisional_infra:1}
    : {category:'Unknown', subtype:'Unknown', provisional_infra:null};
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const filename = process.argv[2];
  if (!filename) throw new Error('provide a collected batch JSON path');
  const state = JSON.parse(await fs.readFile(filename, 'utf8'));
  const cutoff = state.scope_audit.original_rows;
  for (const row of state.records.slice(cutoff)) {
    if (row.run_conclusion !== 'success') Object.assign(row, classifyEvidence(row.error_evidence));
  }
  await fs.writeFile(filename, JSON.stringify(state,null,2));
  const added = state.records.slice(cutoff);
  console.log(JSON.stringify({failures:added.filter(r=>r.run_conclusion==='failure').length,
    provisional_infra_hints:added.filter(r=>r.provisional_infra===1).length,
    final_labels_added:added.filter(r=>r.run_conclusion==='failure'&&r.final_infra!==null).length}));
}
