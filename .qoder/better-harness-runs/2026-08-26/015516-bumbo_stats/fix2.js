const fs = require('fs');
const p = 'h:/QoderWorkspace/bumbo_stats/.qoder/better-harness/2026-08-26/015516-bumbo_stats/findings.json';
try {
  const d = JSON.parse(fs.readFileSync(p, 'utf8'));
  // fix dimension labels to canonical Chinese
  const labelMap = {
    'task-understanding': '任务理解',
    'controlled-execution': '可控执行',
    'change-validation': '改动验证',
    'reliable-delivery': '可靠交付',
    'learning-capture': '经验沉淀'
  };
  d.summary.dimensions.forEach(x => { x.label = labelMap[x.id] || x.label; });
  // findings[2] (long sessions): scrub S1/S2 stable aliases
  const f2 = d.findings[2];
  let t = f2.reason;
  t = t.replace(/S1（子 Agent 候选）估算活跃约 936\.2 分钟 ≈15\.6 小时、S2（子 Agent 候选）约 86\.2 分钟/g, '两个样本为子 Agent 候选，估算活跃时长分别约 936.2 分钟（≈15.6 小时）和 86.2 分钟');
  t = t.replace(/S1 与 S2/g, '两个长会话样本');
  t = t.replace(/S1、S2/g, '两个长会话样本');
  t = t.replace(/S1, S2/g, '两个长会话样本');
  f2.reason = t;
  let pp = f2.aiFixPrompt;
  pp = pp.replace(/S1 估算活跃 ≈936\.2 分钟（子 Agent 候选），S2 估算活跃 ≈86\.2 分钟（子 Agent 候选）/g, '两个长会话样本：估算活跃分别约 936.2 分钟和 86.2 分钟（均为子 Agent 候选）');
  pp = pp.replace(/对 两个长会话样本 各写一条/g, '对每个长会话样本各写一条');
  pp = pp.replace(/只在 `<run>\/report\.md` 保留匿名别名 两个长会话样本/g, '只在 `<run>/report.md` 保留匿名别名');
  pp = pp.replace(/展示别名数（2）/g, '展示别名数（2 个样本）');
  f2.aiFixPrompt = pp;
  f2.expectedOutput = [
    '两个长会话样本各完成任务族/结果/摩擦分类，并形成 reviewed source 校验通过的报告。'
  ];
  // findings[3] (skill): scrub stable asset names
  const f3 = d.findings[3];
  f3.title = f3.title.replace(/skill-safety-checker/g, '<user-skill>');
  f3.reason = f3.reason
    .replace(/skill-safety-checker/g, '<user-skill>')
    .replace(/skill-security-check/g, '<skill-name-a>')
    .replace(/`\/skill-safety-checker`/g, '`/<user-skill>`');
  f3.aiFixPrompt = f3.aiFixPrompt
    .replace(/skill-safety-checker/g, '<user-skill>')
    .replace(/skill-security-check/g, '<skill-name-a>');
  f3.expectedOutput = f3.expectedOutput.map(s => s.replace(/skill-safety-checker/g, '<user-skill>').replace(/skill-security-check/g, '<skill-name-a>'));
  // findings[4] (plugin): scrub stable plugin identifiers
  const f4 = d.findings[4];
  f4.title = f4.title.replace(/better-harness 与 qoder-harness 插件家族/g, '两个 Qoder 插件家族');
  const pluginListOld = '两个 Plugin 均在 `~/.qoder/plugins/cache/qoder-bundler/` 下：\n  - better-harness@qoder-bundler 0.2.9\n  - qoder-harness@qoder-bundler 0.1.71';
  const pluginListNew = '两个 Plugin 均在 `~/.qoder/plugins/cache/qoder-bundler/` 下：\n  - <plugin-a>@qoder-bundler <ver-a>\n  - <plugin-b>@qoder-bundler <ver-b>';
  f4.reason = f4.reason
    .replace(/better-harness@qoder-bundler \(0\.2\.9\) 与 qoder-harness@qoder-bundler \(0\.1\.71\)/g, '<plugin-a>@qoder-bundler（<ver-a>） 与 <plugin-b>@qoder-bundler（<ver-b>）')
    .replace(/better-harness 走 evidence-bundle\/analyze\/render 流程，qoder-harness 走 task-loop 流程/g, '<plugin-a> 走 evidence-bundle/analyze/render 流程，<plugin-b> 走 task-loop 流程')
    .replace(/bumbo_stats 当前使用的就是 better-harness/g, 'bumbo_stats 当前由 <plugin-a> 提供服务')
    .replace(/用户也安装了 qoder-harness/g, '用户也安装了 <plugin-b>')
    .replace(/`\/better-harness` 调用即由其提供/g, '`/<plugin-a>` 调用即由其提供')
    .replace(/本次 `\/better-harness` 调用由其提供/g, '本次 `/<plugin-a>` 调用由其提供');
  f4.aiFixPrompt = f4.aiFixPrompt
    .replace(/better-harness 与 qoder-harness 两个 Qoder Plugin/g, '两个 Qoder Plugin（<plugin-a> 与 <plugin-b>）')
    .replace(pluginListOld, pluginListNew)
    .replace(/若 better-harness 是 qoder-harness 的替代/g, '若 <plugin-a> 是 <plugin-b> 的替代')
    .replace(/若为重命名遗留：删除 qoder-harness 缓存目录/g, '若为重命名遗留：删除 <plugin-b> 缓存目录')
    .replace(/当前 `\/better-harness` 调用由 better-harness 提供/g, '当前 `/<plugin-a>` 调用由 <plugin-a> 提供');
  f4.expectedOutput = f4.expectedOutput.map(s => s.replace(/两个 Plugin/g, '两个插件').replace(/installed\.json/g, '<installed.json>'));
  fs.writeFileSync(p, JSON.stringify(d, null, 2), 'utf8');
  console.log('FIXED-2. findings:', d.findings.length);
  d.summary.dimensions.forEach(x => console.log('  -', x.id, '->', x.label, x.score));
  d.findings.forEach((f, i) => console.log(`  [${i+1}] ${f.severity.padEnd(7)} ${f.id}`));
} catch (e) {
  console.error('ERROR:', e.message);
  process.exit(1);
}