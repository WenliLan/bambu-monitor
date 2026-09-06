const fs = require('fs');
const p = 'h:/QoderWorkspace/bumbo_stats/.qoder/better-harness/2026-08-26/015516-bumbo_stats/findings.json';
const RE = /(?:~[\\/]|\/(?:Users|home|var|private|tmp)\/|[A-Za-z]:\\(?:Users\\)?|\b[0-9a-f]{8}-[0-9a-f-]{27,}\b|\b(?:sk|ghp|github_pat|xox[abprs])[-_][A-Za-z0-9_-]{8,}\b)/iu;
const d = JSON.parse(fs.readFileSync(p, 'utf8'));
['reason','aiFixPrompt','title'].forEach(field => {
  d.findings.forEach((f,i) => {
    const v = String(f[field] ?? '');
    const m = v.match(RE);
    if (m) {
      console.log(`findings[${i}].${field}: MATCH "${m[0]}"`);
      const idx = v.indexOf(m[0]);
      console.log('  context:', JSON.stringify(v.slice(Math.max(0,idx-30), idx+60)));
    }
  });
});
// also check dimensions and overview
['overview'].forEach(field => {
  const v = String(d.summary[field] ?? '');
  const m = v.match(RE);
  if (m) console.log(`summary.${field}: MATCH "${m[0]}"`);
});
d.summary.dimensions.forEach((x,i) => {
  const v = String(x.summary ?? '');
  const m = v.match(RE);
  if (m) console.log(`summary.dimensions[${i}].summary: MATCH "${m[0]}"`);
});
// coverageRows paths
d.summary.aiAgentPractice.coverageRows.forEach((r,i) => {
  (r.paths || []).forEach(path => {
    const m = String(path).match(RE);
    if (m) console.log(`coverageRows[${i}] ${r.surface}: MATCH "${m[0]}" in ${path}`);
  });
});
// expectedOutput
d.findings.forEach((f,i) => {
  (f.expectedOutput || []).forEach((s,j) => {
    const m = String(s).match(RE);
    if (m) console.log(`findings[${i}].expectedOutput[${j}]: MATCH "${m[0]}"`);
  });
});