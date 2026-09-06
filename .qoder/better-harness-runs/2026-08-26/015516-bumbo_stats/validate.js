const fs = require('fs');
const p = 'h:/QoderWorkspace/bumbo_stats/.qoder/better-harness/2026-08-26/015516-bumbo_stats/findings.json';
try {
  const d = JSON.parse(fs.readFileSync(p, 'utf8'));
  console.log('VALID JSON');
  console.log('projectName:', d.summary.projectName);
  console.log('supportTrack:', d.summary.supportTrack.id);
  console.log('overview (first 100):', d.summary.overview.slice(0, 100));
  console.log('dimensions:', d.summary.dimensions.length);
  d.summary.dimensions.forEach(x => console.log('  -', x.id, x.score));
  console.log('coverageRows:', d.summary.aiAgentPractice.coverageRows.length);
  console.log('findings:', d.findings.length);
  d.findings.forEach((f, i) => {
    const l = f.title.length;
    console.log(`  [${i + 1}] ${f.severity.padEnd(7)} ${f.id.padEnd(45)} dims=[${f.dimensionRefs.join(',')}]`);
  });
} catch (e) {
  console.error('INVALID:', e.message);
  process.exit(1);
}