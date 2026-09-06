const fs = require('fs');
const p = 'h:/QoderWorkspace/bumbo_stats/.qoder/better-harness/2026-08-26/015516-bumbo_stats/findings.json';
const d = JSON.parse(fs.readFileSync(p, 'utf8'));

// Dump full text of all findings for inspection
console.log('=== Full findings text ===\n');
d.findings.forEach((f, i) => {
  console.log(`\n--- findings[${i}] (id=${f.id}) ---`);
  ['id', 'title', 'severity', 'reason', 'aiFixPrompt'].forEach(field => {
    const v = String(f[field] ?? '');
    console.log(`\n[${field}] (len=${v.length}):`);
    console.log(v);
  });
});
