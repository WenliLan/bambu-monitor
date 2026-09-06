const fs = require('fs');
const p = 'h:/QoderWorkspace/bumbo_stats/.qoder/better-harness/2026-08-26/015516-bumbo_stats/findings.json';
const d = JSON.parse(fs.readFileSync(p, 'utf8'));

// Map of finding id -> kind
const kinds = {
  'agent-task-entrypoint-missing': 'missing-mechanism',
  'post-edit-validation-not-wired': 'missing-mechanism',
  'long-sessions-missing-outcome-review': 'outcome-gap',
  'user-skill-name-mismatch-and-no-progressive-disclosure': 'evidence-gap',
  'plugin-family-name-overlap': 'evidence-gap',
};

d.findings.forEach((f, i) => {
  // 1. Add kind
  if (kinds[f.id]) f.kind = kinds[f.id];

  // 2. Strip Windows absolute paths from any string field
  ['title', 'reason', 'aiFixPrompt'].forEach(field => {
    let v = String(f[field] ?? '');
    // Replace h:\QoderWorkspace\bumbo_stats with <workspace>
    v = v.replaceAll('h:\\QoderWorkspace\\bumbo_stats', '<workspace>');
    v = v.replaceAll('h:/QoderWorkspace/bumbo_stats', '<workspace>');
    v = v.replaceAll('h:\\\\QoderWorkspace\\\\bumbo_stats', '<workspace>');
    // Also replace any "[A-Z]:\\\\?..." patterns just in case
    v = v.replace(/[A-Za-z]:\\\\?QoderWorkspace\\\\?bumbo_stats/g, '<workspace>');
    v = v.replace(/[A-Za-z]:[\/\\]QoderWorkspace[\/\\]bumbo_stats/g, '<workspace>');
    f[field] = v;
  });
});

fs.writeFileSync(p, JSON.stringify(d, null, 2), 'utf8');

console.log('Fix4 applied:');
d.findings.forEach((f, i) => {
  console.log(`  findings[${i}].id=${f.id} kind=${f.kind}`);
});
