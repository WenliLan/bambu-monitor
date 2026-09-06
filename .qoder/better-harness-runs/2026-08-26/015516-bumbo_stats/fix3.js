const fs = require('fs');
const p = 'h:/QoderWorkspace/bumbo_stats/.qoder/better-harness/2026-08-26/015516-bumbo_stats/findings.json';
try {
  const d = JSON.parse(fs.readFileSync(p, 'utf8'));

  // fix overview & dimension summary to remove literal asset names
  d.summary.overview = d.summary.overview
    .replace(/skill-safety-checker 存在命名不匹配/g, '用户全局 Skill（<user-skill>）存在命名不匹配')
    .replace(/better-harness 与 qoder-harness 插件家族 70%/g, '两个 Qoder 插件家族名称相似度 70%');
  d.summary.dimensions.forEach(x => {
    if (x.id === 'learning-capture') {
      x.summary = x.summary.replace(/skill-safety-checker/g, '<user-skill>');
    }
  });

  // redact all `~/` patterns to `<user-home>/`
  function redactTilde(s) {
    if (typeof s !== 'string') return s;
    return s.replace(/~\//g, '<user-home>/').replace(/~/g, '<user-home>');
  }

  // findings[*].reason and aiFixPrompt
  d.findings.forEach(f => {
    f.reason = redactTilde(f.reason);
    f.aiFixPrompt = redactTilde(f.aiFixPrompt);
    if (Array.isArray(f.expectedOutput)) f.expectedOutput = f.expectedOutput.map(redactTilde);
    if (typeof f.title === 'string') f.title = redactTilde(f.title);
  });

  // coverageRows paths
  d.summary.aiAgentPractice.coverageRows.forEach(r => {
    r.paths = (r.paths || []).map(redactTilde);
  });

  // remaining S1/S2 stable aliases in f2 heading
  const f2 = d.findings[2];
  f2.aiFixPrompt = f2.aiFixPrompt.replace(/复核 S1 与 S2 两个长会话候选/g, '复核两个超阈值长会话候选');

  // overview also has a slash-command reference that should be kept literal — not redacted

  fs.writeFileSync(p, JSON.stringify(d, null, 2), 'utf8');
  console.log('FIXED-3.');
} catch (e) {
  console.error('ERROR:', e.message);
  process.exit(1);
}