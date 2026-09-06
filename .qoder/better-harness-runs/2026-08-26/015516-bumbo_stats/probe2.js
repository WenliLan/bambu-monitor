const fs = require('fs');
const p = 'h:/QoderWorkspace/bumbo_stats/.qoder/better-harness/2026-08-26/015516-bumbo_stats/findings.json';
const d = JSON.parse(fs.readFileSync(p, 'utf8'));

const RE = /(?:~[\\/]|\/(?:Users|home|var|private|tmp)\/|[A-Za-z]:\\(?:Users\\)?|\b[0-9a-f]{8}-[0-9a-f-]{27,}\b|\b(?:sk|ghp|github_pat|xox[abprs])[-_][A-Za-z0-9_-]{8,}\b)/iu;

// Check all findings fields
let totalMatches = 0;
['title', 'reason', 'aiFixPrompt', 'expectedOutput'].forEach(field => {
  if (field === 'expectedOutput') {
    d.findings.forEach((f, i) => {
      (f.expectedOutput || []).forEach((s, j) => {
        const v = String(s);
        const ms = v.match(RE);
        if (ms) {
          totalMatches++;
          console.log(`findings[${i}].expectedOutput[${j}]: MATCH`);
          ms.forEach(m => console.log(`  "${m}" at idx ${v.indexOf(m)}`));
        }
      });
    });
  } else {
    d.findings.forEach((f, i) => {
      const v = String(f[field] ?? '');
      const ms = v.match(RE);
      if (ms) {
        totalMatches++;
        console.log(`findings[${i}].${field}: MATCH`);
        ms.forEach(m => console.log(`  "${m}" at idx ${v.indexOf(m)}`));
      }
    });
  }
});

console.log(`\nTotal matches: ${totalMatches}`);
console.log('\n=== Now also check all d.findings[*] for "kind" field ===');
d.findings.forEach((f, i) => {
  console.log(`findings[${i}].id=${f.id} -> kind=${f.kind ?? 'UNDEFINED'}`);
});
