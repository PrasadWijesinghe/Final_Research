import assert from 'node:assert/strict';
import { classifyEvidence } from '../model/refine_public_evidence.mjs';

const warning = 'Process completed with exit code 1.\nThe ubuntu-latest label will migrate to Ubuntu 26.';
assert.equal(classifyEvidence(warning).provisional_infra, null);
assert.equal(classifyEvidence('Docker pull failed with exit code 1').subtype, 'Container pull');
assert.equal(classifyEvidence('Error: no space left on device').subtype, 'Runner storage capacity');
assert.equal(classifyEvidence('npm test failed: expected 1 but received 2').provisional_infra, null);
