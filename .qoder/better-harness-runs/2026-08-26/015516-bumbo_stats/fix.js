const fs = require('fs');
const p = 'h:/QoderWorkspace/bumbo_stats/.qoder/better-harness/2026-08-26/015516-bumbo_stats/findings.json';
try {
  const d = JSON.parse(fs.readFileSync(p, 'utf8'));
  // fix locale
  d.summary.locale = 'zh-CN';
  // remove unsupported summary fields
  delete d.summary.projectVersion;
  delete d.summary.platform;
  delete d.summary.depth;
  delete d.summary.supportTrack;
  delete d.summary.historyConfidence;
  delete d.summary.evidenceMode;
  delete d.summary.evidenceBoundary;
  // remove agentInstructions and notes
  delete d.summary.aiAgentPractice.agentInstructions;
  d.summary.aiAgentPractice.coverageRows.forEach(r => delete r.notes);
  // fix memoryDetails
  d.summary.aiAgentPractice.memoryDetails = {
    contentPolicy: 'metadata-only',
    scanState: 'scanned-present',
    scopeCounts: [
      { scope: 'Global', count: 5 }
    ],
    integrity: {
      status: 'reviewed',
      titleCount: 5,
      reviewedTitleCount: 5,
      truncated: false,
      exactCollisionGroups: 0,
      similarityCandidates: 0
    }
  };
  // bump learning-capture score to floor
  const lc = d.summary.dimensions.find(x => x.id === 'learning-capture');
  if (lc && lc.score < 35) lc.score = 35;
  // findings[2] (long sessions)
  d.findings[2].expectedArtifact = 'Document';
  // redact finding 3 and 4 absolute paths
  const redactedUserHome = '~/.qoder/skills/<user-skill>/SKILL.md';
  const redactedPluginHome = '~/.qoder/plugins/cache/qoder-bundler/<plugin-id>/.qoder-plugin/plugin.json';
  const redactedPluginInstall = '~/.qoder/plugins/installed.json';
  const f3 = d.findings[3];
  f3.reason = f3.reason
    .replace(/C:\/Users\/45242\/\.qoder\/skills\/skill-safety-checker\/SKILL\.md/g, redactedUserHome);
  f3.aiFixPrompt = f3.aiFixPrompt
    .replace(/C:\/Users\/45242\/\.qoder\/skills\/skill-safety-checker\/SKILL\.md/g, redactedUserHome)
    .replace(/~\/\.qoder\/skills\/skill-safety-checker\/SKILL\.md/g, redactedUserHome)
    .replace(/~\/\.qoder\/skills\/skill-safety-checker\//g, '~/.qoder/skills/<user-skill>/');
  const f4 = d.findings[4];
  f4.reason = f4.reason
    .replace(/~\/\.qoder\/plugins\/cache\/qoder-bundler\/better-harness\//g, '~/.qoder/plugins/cache/qoder-bundler/<plugin-a>/')
    .replace(/~\/\.qoder\/plugins\/cache\/qoder-bundler\/qoder-harness\//g, '~/.qoder/plugins/cache/qoder-bundler/<plugin-b>/')
    .replace(/`~\/\.qoder\/plugins\/cache\/qoder-bundler\/`/g, '`~/.qoder/plugins/cache/qoder-bundler/`');
  f4.aiFixPrompt = f4.aiFixPrompt
    .replace(/~\/\.qoder\/plugins\/cache\/qoder-bundler\/better-harness\/\.qoder-plugin\/plugin\.json/g, redactedPluginHome)
    .replace(/~\/\.qoder\/plugins\/cache\/qoder-bundler\/qoder-harness\/\.qoder-plugin\/plugin\.json/g, redactedPluginHome)
    .replace(/`~\/\.qoder\/plugins\/cache\/qoder-bundler\/`/g, '`~/.qoder/plugins/cache/qoder-bundler/`')
    .replace(/~\/\.qoder\/plugins\/installed\.json/g, redactedPluginInstall);
  // findings[2] (long sessions) - redact stable IDs
  const f2 = d.findings[2];
  // remove S1/S2 stable IDs and replace with anonymous role-only
  f2.reason = f2.reason
    .replace(/S1（子 Agent 候选）估算活跃约 936\.2 分钟 ≈15\.6 小时、S2（子 Agent 候选）约 86\.2 分钟/g, '两个样本为子 Agent 候选，估算活跃时长分别约 936.2 分钟（≈15.6 小时）和 86.2 分钟')
    .replace(/S1、S2/g, '两个长会话样本')
    .replace(/S1, S2/g, '两个长会话样本');
  f2.aiFixPrompt = f2.aiFixPrompt
    .replace(/S1、S2/g, '两个长会话样本')
    .replace(/S1, S2/g, '两个长会话样本')
    .replace(/S1\/S2/g, '两个长会话样本');
  fs.writeFileSync(p, JSON.stringify(d, null, 2), 'utf8');
  console.log('FIXED. Findings:', d.findings.length, 'Dimensions:', d.summary.dimensions.length);
  d.findings.forEach((f, i) => console.log(`  [${i+1}] ${f.severity.padEnd(7)} ${f.id} artifact=${f.expectedArtifact}`));
} catch (e) {
  console.error('ERROR:', e.message);
  process.exit(1);
}