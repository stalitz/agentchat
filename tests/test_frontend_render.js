// 前端渲染回归测试（需 jsdom）：用真实 DOM 加载 frontend/index.html，
// 桩 fetch 返回 /api/config 形态的数据，断言左侧项目树、中栏会话列表能正确渲染。
//
// 此测试锁住历史 bug：loadConfig 曾用 `cfg = d` 别名，随后 `cfg.agents = {}`
// 把 d.agents 也清空，导致 agents 永远渲染不出来。
//
// 运行：
//   NODE_PATH=<managed node workspace>/node_modules node tests/test_frontend_render.js
// run.py 已自动在存在 node + jsdom 时调用本文件。
const { JSDOM } = require('jsdom');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(ROOT, 'frontend', 'index.html'), 'utf8');

// 模拟后端 /api/config：agents 为数组（含未分组 agent），teams/projects 为数组
const fakeConfig = {
  projects: [
    { id: 'pr_demo', name: 'demo', path: '/tmp/demo', team_ids: ['tm_dual', 'tm_fake'] },
  ],
  teams: [
    { id: 'tm_dual', name: '双核Team', agent_ids: ['ag_arch', 'ag_impl'] },
    { id: 'tm_fake', name: '调试Team', agent_ids: ['ag_fake'] },
  ],
  agents: [
    { id: 'ag_arch', name: '架构师', type: 'claude-code', base_url: 'http://localhost:4000', api_key: '', model: 'ollama/gemma' },
    { id: 'ag_impl', name: '实现者', type: 'codex' },
    { id: 'ag_def', name: '默认Claude', type: 'claude-code' }, // 未配模型/网关 → 应显示全局默认模型名
    { id: 'ag_fake', name: '假Agent', type: 'fake' },
    { id: 'ag_orphan', name: '孤立Agent', type: 'fake' }, // 不在任何 team 中
  ],
  // 全局设置：default_max_rounds 用 20 以便与硬编码 12 区分，验证 composer 默认值确实来自全局设置
  // default_model / default_model_codex 用真实格式串，验证「默认」agent 显示的是模型名而非「默认」占位
  settings: {
    default_timeout: 300, default_max_rounds: 20, default_model: 'claude-sonnet-4-20250514', default_model_codex: 'deepseek-v4-flash',
    // 提示词默认模板（后端返回，前端据此显示默认/恢复默认）
    prompt_template_defaults: { debate: { consensus: '【默认共识】先总结再确认', extra_rules: '' } },
  },
};

const demoSession = {
  id: 's_1', title: '首次会话',
  project_id: 'pr_demo', project_name: 'demo',
  team_id: 'tm_dual', team_name: '双核Team',
  created_at: '2026-08-10 10:00:00', updated_at: '2026-08-10 10:05:00',
  message_count: 2, preview: '先定义接口',
};

const dom = new JSDOM(html, {
  runScripts: 'dangerously',
  beforeParse(window) {
    window.WebSocket = function () { this.send = () => {}; this.close = () => {}; };
    window.fetch = function (url) {
      if (typeof url === 'string' && url.indexOf('/api/fs') >= 0) {
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({
          path: '/tmp/demo', parent: '/tmp', dirs: [{ name: 'sub1', path: '/tmp/demo/sub1' }],
        }) });
      }
      if (typeof url === 'string' && url.indexOf('/api/session/') >= 0) {
        // 会话详情
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({
          id: 's_1', title: '首次会话',
          team_id: 'tm_dual',
          messages: [
            { round: 0, speaker: '架构师', role: '架构师', content: '先定义接口' },
            { round: 1, speaker: '实现者', role: '实现者', content: '好的，我来写' },
          ],
          agent_sessions: { 架构师: 'a1b2c3d4-', 实现者: 'e5f6a7b8-' },
          exists: true,
        }) });
      }
      if (url === '/api/sessions/pr_demo') {
        // 项目级会话列表（中栏用）：按 updated_at 倒序，最新在前
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ sessions: [demoSession] }) });
      }
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(fakeConfig) });
    };
  },
});

let ok = true;
function check(cond, msg) {
  if (!cond) { console.error('FAIL: ' + msg); ok = false; }
  else { console.log('PASS: ' + msg); }
}

// 真实点击触发的是异步链（fetch -> 渲染），断言前要让出事件循环
const tick = (ms = 60) => new Promise((r) => setTimeout(r, ms));

setTimeout(async () => {
  const doc = dom.window.document;
  const tree = doc.getElementById('tree');
  const cs = dom.window.getComputedStyle;
  const agents = tree.querySelectorAll('.agent');
  const projHeaders = Array.from(tree.querySelectorAll('.proj')).map((n) => n.textContent);
  const orphanShown = projHeaders.some((t) => t.indexOf('未分组的 Agent') >= 0);

  // ---- 左侧项目树渲染 ----
  check(agents.length === 5, '项目树应渲染 5 个 agent（含未分组：ag_arch/ag_impl/ag_fake + 默认claude ag_def + 孤子 ag_orphan），实际 ' + agents.length);
  check(orphanShown, '应出现「未分组的 Agent」分区（孤立 agent 也要显示）');

  // ---- 分级缩进对齐（历史 bug：agent 无 chevron 占位，margin-left(28px) 视觉上浅于 team(14px+chevron)，与分级相反）----
  const team0 = tree.querySelector('.node.team');
  check(team0, '应存在 Team 节点（用于缩进对比）');
  if (agents[0] && team0) {
    const aM = parseInt(cs(agents[0]).marginLeft) || 0;
    const tM = parseInt(cs(team0).marginLeft) || 0;
    check(agents[0].querySelector('.chev') != null,
      'agent 节点应含占位折叠箭头，使其缩进与 Team/项目对齐（修复 agent 缩进比 team 小的 bug）');
    check(aM > tM, 'agent 的 margin-left(' + aM + 'px) 应严格大于 team(' + tM + 'px)，确保视觉上更深一级');
  }
  check(dom.window.cfg && dom.window.cfg.agents['ag_orphan'] != null, '孤立 agent 必须进入 cfg.agents');
  check(dom.window.cfg && dom.window.cfg.agents['ag_arch'] != null, 'team 内 agent 必须进入 cfg.agents');

  // ---- 「未分组」只按归属判定，折叠不可触发（历史 bug：折叠 project 后其 team 被误归入「未分组的 Team」）----
  dom.window.expanded.projects['pr_demo'] = false;
  dom.window.renderTree();
  var headersCollapsed = Array.from(tree.querySelectorAll('.proj')).map((n) => n.textContent);
  check(!headersCollapsed.some((t) => t.indexOf('未分组的 Team') >= 0),
    '折叠 project 后，其 team 不应被归入「未分组的 Team」（仅隐藏，归属不变）');
  dom.window.expanded.projects['pr_demo'] = true;
  dom.window.expanded.teams['tm_dual'] = false;
  dom.window.renderTree();
  // 折叠 tm_dual 后：ag_arch/ag_impl 隐藏且不得泄漏到未分组；仅 tm_fake 的 ag_fake + 默认claude ag_def + 真孤子 ag_orphan 可见
  var agentsAfterTeamCollapse = tree.querySelectorAll('.agent');
  check(agentsAfterTeamCollapse.length === 3,
    '折叠 team 后，其 agent 不应误归「未分组的 Agent」，应仅剩 3 个（tm_fake 的 ag_fake + 默认claude ag_def + 孤子 ag_orphan），实际 ' + agentsAfterTeamCollapse.length);
  dom.window.expanded.teams['tm_dual'] = true;
  dom.window.renderTree();

  // ---- 类型徽章 ----
  check(doc.querySelectorAll('.b-proj').length >= 1, '项目节点应有「项目」徽章');
  check(doc.querySelectorAll('.b-team').length >= 1, 'Team 节点应有「Team」徽章');
  check(doc.querySelectorAll('.b-agent').length >= 1, 'Agent 节点应有「Agent」徽章');

  // ---- agent 节点应直接显示所连模型（名称不被模型绑定）----
  // 配了 base_url 的 claude-code agent：显示本地网关模型（model-local）
  const localTags = Array.from(doc.querySelectorAll('.tag.model-local')).map((n) => n.textContent);
  check(localTags.indexOf('ollama/gemma') >= 0,
    '配了 base_url 的 agent 应直接显示本地模型「ollama/gemma」（model-local 标签），实际 ' + JSON.stringify(localTags));
  // 未配置本地模型的 agent：
  // - claude-code 类型 → 显示全局默认模型名（claude-sonnet-4-…），不是「默认」占位
  // - codex 类型 → 显示全局默认模型名（deepseek-v4-flash），不是「默认」占位
  // - fake 等无探测类型 → 仍显示「默认」
  const defaultTags = Array.from(doc.querySelectorAll('.tag.model-default')).map((n) => n.textContent);
  check(defaultTags.indexOf('claude-sonnet-4-20250514') >= 0,
    '未配模型的 claude-code agent 应显示全局默认模型名（claude-sonnet-4-20250514），而非「默认」占位，实际 ' + JSON.stringify(defaultTags));
  check(defaultTags.indexOf('deepseek-v4-flash') >= 0,
    '未配模型的 codex agent 应显示全局默认模型名（deepseek-v4-flash），而非「默认」占位，实际 ' + JSON.stringify(defaultTags));
  check(defaultTags.indexOf('默认') >= 0,
    'fake 等无探测类型仍显示「默认」（model-default 标签），实际 ' + JSON.stringify(defaultTags));

  // ---- 徽章颜色不应被提示词日志的 .badge 规则污染（历史回归：第二条 .badge 与 .b-proj/.b-team 同特异性且靠后，会全变成橙色）----
  const bProj = doc.querySelector('.b-proj');
  const bTeam = doc.querySelector('.b-team');
  check(bProj && cs(bProj).backgroundColor === 'rgb(223, 231, 255)',
    '项目徽章背景应为蓝(#dfe7ff)，不应被提示词日志 .badge 覆盖成橙色，实际 ' + (bProj && cs(bProj).backgroundColor));
  check(bTeam && cs(bTeam).backgroundColor === 'rgb(227, 245, 234)',
    'Team 徽章背景应为绿(#e3f5ea)，实际 ' + (bTeam && cs(bTeam).backgroundColor));

  // ---- 编辑入口（行内 ✎ 图标），不再靠点整行编辑 ----
  check(tree.querySelectorAll('.edit-btn').length >= 4, '左侧节点应提供行内编辑图标（≥4），实际 ' + tree.querySelectorAll('.edit-btn').length);

  // ---- 点项目行内「编辑」图标 -> 弹出预填的编辑表单 ----
  const projEdit = doc.querySelector('.node.proj .edit-btn');
  projEdit.click();
  const card = doc.getElementById('modalCard');
  check(doc.getElementById('modal').classList.contains('show'), '点项目编辑图标应弹出编辑弹窗');
  check(card.innerHTML.indexOf('编辑 项目') >= 0, '编辑弹窗标题应为「编辑 项目」');
  check(doc.getElementById('name') && doc.getElementById('name').value === 'demo', '编辑表单应预填项目名 demo');
  // ---- 现代多选（项目 Teams）：替代原生 <select multiple> ----
  const mTeams = doc.getElementById('mTeams');
  check(mTeams && mTeams.classList.contains('ms-list'), '项目表单 Teams 应为现代多选(.ms-list 容器)，实际 ' + (mTeams && mTeams.className));
  check(mTeams && mTeams.querySelectorAll('input[type=checkbox]').length === 2, '项目表单 Teams 应渲染 2 个 team 选项(checkbox)，实际 ' + (mTeams && mTeams.querySelectorAll('input[type=checkbox]').length));
  check(mTeams && mTeams.querySelectorAll('input[checked]').length === 2, '项目表单 Teams 应预选 2 个 team(tm_dual/tm_fake)');
  check(!doc.getElementById('modalCard').querySelector('select[multiple]'), '项目表单不应再含原生 <select multiple>');
  dom.window.closeModal();

  // ---- Team 成员现代多选 ----
  const teamEdit = doc.querySelector('.node.team .edit-btn');
  check(!!teamEdit, 'Team 节点应提供行内编辑图标');
  if (teamEdit) {
    teamEdit.click();
    const mAgents = doc.getElementById('mAgents');
    check(mAgents && mAgents.classList.contains('ms-list'), 'Team 表单成员应为现代多选(.ms-list)');
    check(mAgents && mAgents.querySelectorAll('input[type=checkbox]').length === 5, 'Team 表单成员应渲染 5 个 agent 选项，实际 ' + (mAgents && mAgents.querySelectorAll('input[type=checkbox]').length));
    check(mAgents && mAgents.querySelectorAll('input[checked]').length === 2, 'Team 表单成员应预选该 team 的 2 个 agent');
    check(!doc.getElementById('modalCard').querySelector('select[multiple]'), 'Team 表单不应再含原生 <select multiple>');
    dom.window.closeModal();
  }

  // ---- 点项目名 -> 选中项目并加载其会话（中栏）----
  const projNm = doc.querySelector('.node.proj .nm');
  projNm.click();
  await tick();
  check(doc.getElementById('composerProj').textContent.indexOf('demo') >= 0, '选中项目后 composer 应显示该项目名');
  check(doc.getElementById('sessionList').querySelectorAll('.sess-card').length >= 1, '选中项目后中栏应渲染该项目会话列表');

  // ---- 会话卡片 + 自动恢复 ----
  const chat0 = doc.getElementById('chat');
  check(doc.getElementById('sessionList').querySelectorAll('.sess-card.active').length === 1, '应高亮当前选中的会话');
  check(doc.getElementById('btnNew') != null, '中栏应提供「＋ 新」按钮');
  check(typeof dom.window.openNewSession === 'function', '应暴露 openNewSession（开新会话）函数');
  check(doc.getElementById('sessionList').querySelectorAll('.sess-badges .badge').length >= 1, '会话卡片应带 Team 徽章');
  check(chat0.textContent.indexOf('已恢复会话记录') >= 0, '加载后应选最新会话并自动恢复其记录（刷新不丢）');
  check(chat0.textContent.indexOf('先定义接口') >= 0, '会话记录应包含首轮 agent 发言内容');
  check(doc.getElementById('btnStart').textContent.indexOf('继续讨论') >= 0, '多 agent 且已有记录时按钮应为「继续讨论（已有 N 条）」');

  // ---- 中栏按 team 过滤会话 ----
  // 造 3 组（tm_dual×2 / tm_fake×1 / 无 team×1）⇒ 出现过滤条；选中某组只渲染该组卡片
  dom.window.curProjSessions = [
    { id: 's_1', title: '双核甲', team_id: 'tm_dual', team_name: '双核Team', message_count: 1 },
    { id: 's_2', title: '双核乙', team_id: 'tm_dual', team_name: '双核Team', message_count: 1 },
    { id: 's_3', title: '假甲', team_id: 'tm_fake', team_name: '调试Team', message_count: 1 },
    { id: 's_4', title: '无team', team_id: '', team_name: '', message_count: 1 },
  ];
  dom.window.teamFilter = '';
  dom.window.renderSessions();
  const fWrap = doc.getElementById('sessFilter'), fSel = doc.getElementById('selTeamFilter');
  const cards = () => doc.getElementById('sessionList').querySelectorAll('.sess-card');
  const pick = (v) => { fSel.value = v; fSel.dispatchEvent(new dom.window.Event('change')); };
  check(cs(fWrap).display !== 'none', '多个 team 的项目应显示中栏 team 过滤条');
  check(fSel.querySelectorAll('option').length === 4, '过滤下拉应为「全部 + 3 组」（含未指定 team），实际 ' + fSel.querySelectorAll('option').length);
  check(cards().length === 4, '默认「全部 team」应渲染全部会话，实际 ' + cards().length);
  check(doc.getElementById('sessCount').textContent.indexOf('4') >= 0, '未过滤时计数应显示会话总数');
  pick('tm_dual');
  check(cards().length === 2, '选中 tm_dual 应只渲染该 team 的 2 条会话，实际 ' + cards().length);
  check(doc.getElementById('sessCount').textContent.indexOf('2 / 4') >= 0,
    '过滤时计数应显示「命中 / 总数」，实际 ' + doc.getElementById('sessCount').textContent);
  check(fSel.value === 'tm_dual', '过滤后下拉应保持选中值');
  check(dom.window.curProjSessions.length === 4, '过滤只影响渲染，不应改动 curProjSessions（被筛掉的会话仍可被打开/续接）');
  pick('__none__');
  check(cards().length === 1 && cards()[0].textContent.indexOf('无team') >= 0, '未指定 team 的历史会话应能单独筛出');
  pick('');
  check(cards().length === 4, '切回「全部 team」应恢复全部会话');
  // 选中的 team 已无会话（会话被删 / 换项目）→ 回落「全部」，不留空白列表
  dom.window.teamFilter = 'tm_gone'; dom.window.renderSessions();
  check(dom.window.teamFilter === '' && cards().length === 4, '选中的 team 不存在时应回落「全部 team」');
  // 只有一个 team → 过滤是空操作，整条隐藏（不占中栏高度）
  dom.window.curProjSessions = [{ id: 's_1', title: '独苗', team_id: 'tm_dual', team_name: '双核Team' }];
  dom.window.renderSessions();
  check(cs(fWrap).display === 'none', '只有一个 team 时应隐藏过滤条（无可过滤维度）');
  check(cards().length === 1, '隐藏过滤条时仍应正常渲染全部会话');
  dom.window.curProjSessions = [demoSession];   // 还原，避免影响后续断言
  dom.window.renderSessions();

  // ---- 点 agent 行内编辑图标 -> 预填 + 删除按钮 ----
  const agentEdit = doc.querySelector('.node.agent .edit-btn');
  agentEdit.click();
  check(doc.getElementById('modalCard').innerHTML.indexOf('编辑 Agent') >= 0, '点 agent 编辑图标应弹出「编辑 Agent」');
  check(doc.getElementById('name') && doc.getElementById('name').value === '架构师', 'agent 编辑表单应预填 架构师');
  check(doc.getElementById('modalCard').innerHTML.indexOf('删除') >= 0, '编辑态应显示删除按钮');
  dom.window.closeModal();

  // ---- agent 表单不应含 agent 级 max_rounds / timeout / cwd ----
  doc.querySelector('.node.agent .edit-btn').click();
  const agentCard = doc.getElementById('modalCard').innerHTML;
  check(agentCard.indexOf('最大轮次') < 0, 'Agent 编辑表单不应含「最大轮次」');
  check(agentCard.indexOf('单轮超时(秒)') < 0, 'Agent 编辑表单不应含「单轮超时」');
  check(agentCard.indexOf('项目目录(可选)') < 0, 'Agent 编辑表单不应含「项目目录」');
  dom.window.closeModal();
  check(!!doc.getElementById('maxRounds'), '发起讨论区应提供「最大轮次」输入(maxRounds)');

  // ---- 全局设置：必须有界面入口（历史缺口：函数有、按钮没有，只能手改 config.json）----
  const settingsBtn = doc.getElementById('btnSettings');
  check(!!settingsBtn, '左栏应提供「⚙ 设置」按钮（全局设置界面入口）');
  check(!!doc.getElementById('btnSettingsRail'), '折叠后的图标栏也应提供设置入口');
  settingsBtn.click();
  check(doc.getElementById('modal').classList.contains('show'), '点击「⚙ 设置」应弹出设置弹窗');
  check(doc.getElementById('modalCard').innerHTML.indexOf('全局设置') >= 0, '弹窗标题应为「全局设置」');
  check(!!doc.getElementById('gTimeout'), '全局设置弹窗应提供「单轮静默超时」输入(gTimeout)');
  check(!!doc.getElementById('gMaxRounds'), '全局设置弹窗应提供「辩论模式默认轮次上限」输入(gMaxRounds)');
  check(doc.getElementById('gMaxRounds') && doc.getElementById('gMaxRounds').value === '20',
    'gMaxRounds 应预填全局默认轮次(20)，实际 ' + (doc.getElementById('gMaxRounds') && doc.getElementById('gMaxRounds').value));
  check(!!doc.getElementById('gCtx'), '全局设置应提供 Ollama 上下文长度输入(gCtx)，可从界面控制 num_ctx');
  check(!!doc.getElementById('gMaxOut'), '全局设置应提供 Claude 输出上限输入(gMaxOut)');
  check(!!doc.getElementById('gThink'), '全局设置应提供 Ollama 思考开关(gThink)');
  check(!!doc.getElementById('gConsensus'), '全局设置应提供共识终止提示词编辑(gConsensus)');
  check(doc.getElementById('gConsensus') && doc.getElementById('gConsensus').value === '【默认共识】先总结再确认',
    'gConsensus 应显示当前生效文本（未配置时=内置默认），实际 ' + (doc.getElementById('gConsensus') && doc.getElementById('gConsensus').value));
  check(!!doc.getElementById('gExtraRules'), '全局设置应提供附加规则提示词编辑(gExtraRules)');
  check(!!doc.querySelector('button[onclick="setTemplateDefault(\'gConsensus\')"]'),
    'gConsensus 应有「恢复默认」按钮');
  check(!!doc.querySelector('#modalCard .set-body'), '设置面板应有可滚动内容区(set-body)，长内容不被裁切');
  check((doc.getElementById('modalCard') && doc.getElementById('modalCard').style.maxHeight) || '',
    '设置卡片应限高(maxHeight)，保证滚动生效');
  const mrComposer = doc.getElementById('maxRounds');
  check(mrComposer && mrComposer.value === '20' && !mrComposer.disabled,
    '辩论模式下 composer 的「最大轮次」默认值应等于全局默认(20)，实际 ' + (mrComposer && mrComposer.value));
  // ---- 遮罩点击关闭：拖选文字后松手不应误关设置框（历史 bug：选中文字鼠标滑出设置框即消失）----
  const sModal = doc.getElementById('modal');
  const sSel = doc.defaultView.getSelection();
  const sRg = doc.createRange();
  sRg.selectNodeContents(doc.getElementById('modalCard'));   // modalCard 含「全局设置」等文本
  sSel.removeAllRanges(); sSel.addRange(sRg);
  check(sSel.toString().length > 0, '准备：应为设置框内容建立文字选区');
  sModal.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }));
  check(sModal.classList.contains('show'), '选中文字后点击遮罩不应关闭设置框');
  sSel.removeAllRanges();
  sModal.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }));
  check(!sModal.classList.contains('show'), '无选中文字时点击遮罩应正常关闭设置框');
  dom.window.closeModal();

  // ---- 两栏折叠（项目栏 IDEA 风格「-」-> 图标栏；会话栏可折叠）----
  check(!!doc.getElementById('btnCollapse'), '应提供项目栏折叠按钮');
  check(!!doc.getElementById('btnSessCollapse'), '应提供会话栏折叠按钮');
  check(typeof dom.window.toggleSidebar === 'function', '应暴露 toggleSidebar');
  check(typeof dom.window.toggleSessions === 'function', '应暴露 toggleSessions');
  dom.window.toggleSidebar();
  check(doc.body.classList.contains('sidebar-collapsed'), '折叠后 body 应有 sidebar-collapsed');
  check(doc.getElementById('rail') != null, '折叠后图标栏(rail)应存在');
  dom.window.toggleSidebar(); // 恢复

  // 会话栏折叠后必须有重新展开入口（否则隐藏后找不回，真 bug）
  dom.window.toggleSessions();
  check(doc.body.classList.contains('sessions-collapsed'), '折叠会话栏后 body 应有 sessions-collapsed');
  const sessRail = doc.getElementById('sessRail');
  check(sessRail != null, '会话栏折叠后应存在展开入口 sessRail');
  if (sessRail) {
    check(sessRail.querySelector('button') != null, 'sessRail 应含可点击的展开按钮');
    sessRail.querySelector('button').click(); // 点展开入口
    check(!doc.body.classList.contains('sessions-collapsed'), '点展开入口后会话栏应恢复显示');
  }

  // ---- 项目目录选择器（点「选择文件夹」弹目录树，回填绝对路径）----
  doc.querySelector('.node.proj .edit-btn').click();
  const projForm = doc.getElementById('modalCard').innerHTML;
  check(projForm.indexOf('选择文件夹') >= 0, '项目表单应含「选择文件夹」按钮');
  dom.window.openFsPicker('');
  check(doc.getElementById('fsModal').classList.contains('show'), '调用目录选择器应弹出 fsModal');
  setTimeout(function () {
    const fsCard = doc.getElementById('fsCard').innerHTML;
    check(fsCard.indexOf('选择此目录') >= 0, '目录选择器应提供「选择此目录」按钮');
    check(fsCard.indexOf('/tmp/demo') >= 0, '目录选择器应显示当前路径');
    const chooseBtn = Array.from(doc.querySelectorAll('#fsCard button'))
      .find(function (b) { return b.textContent.indexOf('选择此目录') >= 0; });
    check(!!chooseBtn, '应能找到「选择此目录」按钮');
    chooseBtn.onclick();
    const pathInput = doc.getElementById('path');
    check(pathInput && pathInput.value === '/tmp/demo', '选择后应把绝对路径回填到项目目录输入');
    check(!doc.getElementById('fsModal').classList.contains('show'), '选择后目录选择器应关闭');

    // ---- 单 agent Team：轮次置灰、提示单 agent 模式（由 composer 当前 Team 决定）----
    dom.window.document.getElementById('selTeam').value = '';
    dom.window.curTid = 'tm_fake';       // 该 Team 仅 1 个 agent
    dom.window.updateRoundControl();
    const mrInput = doc.getElementById('maxRounds');
    check(mrInput.disabled === true, '选单 agent Team 时「最大轮次」应置灰禁用');
    check(doc.getElementById('roundHint').textContent.indexOf('单 agent 模式') >= 0,
      '单 agent Team 时提示应切换为「单 agent 模式」');
    // ---- 单 agent = 对话模式：辩论专用控件隐藏，主按钮变「发送」----
    check(doc.body.classList.contains('chat-mode'), '单 agent Team 应进入对话模式(chat-mode)');
    check(doc.getElementById('btnStart').textContent.indexOf('发送') >= 0,
      '对话模式主按钮应为「发送」，而不是语义不符的「继续会话」');
    check(doc.getElementById('modeTag').textContent.indexOf('对话模式') >= 0, '应显示「对话模式（单 agent）」标识');
    check(doc.getElementById('task').placeholder.indexOf('输入消息') >= 0, '对话模式输入框应提示「输入消息」');
    check(!!doc.getElementById('injectRow'), '介入区应存在（辩论模式用）');
    check(!!doc.getElementById('ctrlBtns'), '暂停/继续/停止应集中在 ctrlBtns 内，便于按模式隐藏');
    check(doc.getElementById('injectRow').className.indexOf('debate-only') >= 0
      || dom.window.getComputedStyle(doc.getElementById('injectRow')).display === 'none',
      '对话模式下人工介入区应隐藏（点了也无效，避免误导）');

    // 切回多 agent Team 应恢复可用
    dom.window.curTid = 'tm_dual';
    dom.window.updateRoundControl();
    check(mrInput.disabled === false, '切回多 agent Team 时「最大轮次」应恢复可用');
    check(!doc.body.classList.contains('chat-mode'), '多 agent Team 应回到辩论模式');
    check(doc.getElementById('modeTag').textContent.indexOf('辩论模式') >= 0, '应显示「辩论模式（N agents）」标识');
    check(doc.getElementById('btnStart').textContent.indexOf('讨论') >= 0, '辩论模式主按钮文案应含「讨论」');

    // ---- run_end 事件：结束后有议题则按钮恢复可点；空议题仍禁用（避免 agent 空跑）----
    // ---- 运行中：主按钮槽位变「中止」（turn 级中断，不再禁用——禁用就没法中止死循环）----
    dom.window.curSid = 's_test';
    dom.window.pendingBySid = {};
    dom.window.runningBySid = { s_test: true }; dom.window.refreshSendBtn();
    check(doc.getElementById('btnStart').disabled === false, '运行中主按钮应可点（此时是「中止」）');
    check(doc.getElementById('btnStart').textContent.indexOf('中止') >= 0, '运行中主按钮文案应为「中止」');
    doc.getElementById('task').value = '下一代架构怎么定';   // 有议题才允许继续
    dom.window.handleEvent({ type: 'run_end', mode: 'debate', sid: 's_test' });
    check(doc.getElementById('btnStart').disabled === false, '收到 run_end 且输入议题后主按钮应恢复可点');
    doc.getElementById('task').value = '';
    dom.window.refreshSendBtn();
    check(doc.getElementById('btnStart').disabled === true, '辩论模式空议题时主按钮应禁用（达成一致后空点继续不应让 agent 空跑）');

    // ---- 中止后（有待续接发言）：主按钮变「继续」，且不需要输入内容 ----
    dom.window.pendingBySid = { s_test: { agent: 'g1' } };
    doc.getElementById('task').value = '';
    dom.window.refreshSendBtn();
    check(doc.getElementById('btnStart').disabled === false, '有待续接发言时主按钮应可点（续接不需要输入）');
    check(doc.getElementById('btnStart').textContent.indexOf('继续') >= 0, '有待续接发言时主按钮文案应为「继续」');
    check(doc.getElementById('btnStart').textContent.indexOf('g1') >= 0, '「继续」应标明由哪个 agent 重新发言');
    // 被中止的气泡：作废但仍保留内容可见（灰化 + 「已中止」角标）
    dom.window.renderHistoryEntry({ speaker: 'g1', content: '半截发言', interrupted: true, blocks: [] });
    var bubbles = doc.querySelectorAll('#chat .bubble.interrupted');
    check(bubbles.length === 1, '被中止的发言气泡应带 interrupted 样式类');
    check((bubbles[0].textContent || '').indexOf('已中止') >= 0, '被中止的气泡应显示「已中止」标记');
    dom.window.pendingBySid = {};

    // ---- 多会话并行：其他会话在跑时当前会话按钮不受影响；其他会话事件不渲染到当前聊天区 ----
    dom.window.curSid = 's_b';
    dom.window.runningBySid = { s_a: true };   // 会话 A 正在跑，当前是 B
    doc.getElementById('task').value = 'B 的议题';
    dom.window.refreshSendBtn();
    check(doc.getElementById('btnStart').disabled === false, '其他会话运行时，当前会话主按钮应可点（并行）');
    dom.window.chat.innerHTML = '';
    dom.window.handleEvent({ type: 'token', agent: 'A', text: 'A 的发言', sid: 's_a' });
    check(doc.getElementById('chat').innerHTML === '', '其他会话的流式事件不应渲染到当前聊天区');
    dom.window.handleEvent({ type: 'run_end', mode: 'debate', sid: 's_a' });
    check(dom.window.runningBySid['s_a'] === false, '其他会话 run_end 应更新其 running 状态');

    // ---- 布局拖拽：列宽 + 聊天/输入区高度可调 ----
    const fire = (el, type, props) => el.dispatchEvent(new dom.window.MouseEvent(type, Object.assign({ bubbles: true }, props)));
    check(!!doc.getElementById('splitSidebar'), '应存在项目栏竖向拖拽分隔条');
    check(!!doc.getElementById('splitSessions'), '应存在会话栏竖向拖拽分隔条');
    check(!!doc.getElementById('splitChat'), '应存在聊天区/输入区横向拖拽分隔条');
    check(html.indexOf('body.sidebar-collapsed #splitSidebar { display:none; }') >= 0, '折叠项目栏时应隐藏对应分隔条（避免误拖）');
    // 模拟拖动项目栏分隔条：起始回退 264，右移 50 → 314px
    fire(doc.getElementById('splitSidebar'), 'mousedown', { clientX: 100 });
    fire(doc, 'mousemove', { clientX: 150 });
    fire(doc, 'mouseup', {});
    check(doc.body.style.getPropertyValue('--sidebar-w') === '314px', '拖动项目栏分隔条应更新 --sidebar-w');
    // 模拟拖动聊天/输入区分隔条：预置 200，上移 40 → 240px（向上拖 → 输入区更高）
    doc.body.style.setProperty('--composer-h', '200px');
    fire(doc.getElementById('splitChat'), 'mousedown', { clientY: 300 });
    fire(doc, 'mousemove', { clientY: 260 });
    fire(doc, 'mouseup', {});
    check(doc.body.style.getPropertyValue('--composer-h') === '240px', '拖动聊天/输入区分隔条应更新 --composer-h');

    // ---- 系统消息（终止原因 / 会话结束）应渲染进聊天区，run_end 重载后不丢失 ----
    dom.window.renderHistoryEntry({ speaker: 'system', role: '系统', content: 'A 标记 TERMINATE，会话收敛' });
    check(doc.getElementById('chat').textContent.indexOf('会话收敛') >= 0,
      '系统消息（终止原因）应渲染进聊天区（落库后 run_end 重载 / 刷新仍可见）');

    // ---- 空思考块跳过（CLI 原生 thinking:"" 的回合不该显示「💭 … 0 字」空折叠块）----
    dom.window.chat.innerHTML = '';
    dom.window.renderHistoryEntry({ speaker: 'g1', round: 0, content: '带空思考块的一轮', thinking: '',
      blocks: [{ type: 'thinking', thinking: '' }, { type: 'text', text: '带空思考块的一轮' }] });
    check(doc.querySelectorAll('#chat .turn-block.thinking').length === 0,
      'blocks 里的空思考块（thinking:""）不应渲染出折叠块');
    check(doc.getElementById('chat').textContent.indexOf('0 字') < 0, '不应出现「0 字」的思考块标题');
    check(doc.getElementById('chat').textContent.indexOf('带空思考块的一轮') >= 0, '正文仍应正常渲染');
    // 非空思考块照旧渲染（避免修成「一律不显示思考」）
    dom.window.chat.innerHTML = '';
    dom.window.renderHistoryEntry({ speaker: 'g1', round: 0, content: '正常一轮', thinking: '真的思考了',
      blocks: [{ type: 'thinking', thinking: '真的思考了' }, { type: 'text', text: '正常一轮' }] });
    check(doc.querySelectorAll('#chat .turn-block.thinking').length === 1, '非空思考块仍应正常渲染');
    // 回退路径（无 blocks，只有顶层 thinking 字段）同样跳过空白
    dom.window.chat.innerHTML = '';
    dom.window.renderHistoryEntry({ speaker: 'g1', round: 0, content: '仅空白思考', thinking: '   ' });
    check(doc.querySelectorAll('#chat .turn-block.thinking').length === 0,
      '无 blocks 的回退路径下，空白 thinking 也不应渲染折叠块');

    if (!ok) { console.error('前端渲染回归测试失败'); process.exit(1); }
    console.log('前端渲染回归测试通过 ✓');
    process.exit(0);
  }, 80);
}, 600);
