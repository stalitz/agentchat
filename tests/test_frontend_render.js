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

  // ---- 点「本项目下没有会话的 Team」应显示空列表（而非保留上次过滤结果）----
  dom.window.curProjSessions = [
    { id: 's_1', title: '双核甲', team_id: 'tm_dual', team_name: '双核Team', message_count: 1 },
    { id: 's_2', title: '双核乙', team_id: 'tm_dual', team_name: '双核Team', message_count: 1 },
  ];
  dom.window.teamFilter = ''; dom.window.renderSessions();
  dom.window.selectTeam('tm_fake');   // tm_fake 是合法 Team，但本项目下无会话
  check(doc.getElementById('sessionList').querySelectorAll('.sess-card').length === 0,
    '点「本项目无会话的 Team」应显示空列表（而非上次过滤的 2 条），实际 '
    + doc.getElementById('sessionList').querySelectorAll('.sess-card').length);
  check(doc.getElementById('sessionList').textContent.indexOf('暂无会话') >= 0, '空列表应给出「暂无会话」提示');
  check(doc.querySelectorAll('.node.team.selected').length === 1
    && doc.querySelector('.node.team.selected').textContent.indexOf('调试Team') >= 0,
    '被点的无会话 Team 应在左栏高亮（确认点击有反应）');
  check(fSel.value === 'tm_fake', '中栏过滤下拉应选中该空 Team（计数 0）');
  check(doc.getElementById('sessCount').textContent.indexOf('0 / 2') >= 0,
    '计数应显示「0 / 2」，实际 ' + doc.getElementById('sessCount').textContent);
  check(doc.getElementById('status').textContent.indexOf('没有') >= 0, '应给出「当前项目下没有属于该 Team 的会话」提示');
  // 过滤到「无会话的 Team」时右栏也应清空（不再显示旧会话）—— 用户反馈：过滤中栏后会话栏没一起变
  check(dom.window.curSid === null, '过滤到无会话的 Team 时，右栏会话应被清空（curSid 置空），实际 ' + dom.window.curSid);
  check(doc.getElementById('chat').textContent.indexOf('暂无会话') >= 0, '右栏清空后应给出「暂无会话」提示');
  dom.window.selectTeam('tm_dual');   // 还原
  dom.window.curProjSessions = [demoSession]; dom.window.teamFilter = ''; dom.window.renderSessions();

  // ---- 已有对话的会话「Team 不可改」（FR18）+ 左栏点 Team 过滤中栏 ----
  dom.window.curProjSessions = [
    { id: 's_1', title: '双核甲', team_id: 'tm_dual', team_name: '双核Team', message_count: 2 },
    { id: 's_2', title: '双核乙', team_id: 'tm_dual', team_name: '双核Team', message_count: 1 },
    { id: 's_3', title: '假甲', team_id: 'tm_fake', team_name: '调试Team', message_count: 1 },
    { id: 's_4', title: '无team', team_id: '', team_name: '', message_count: 1 },
  ];
  dom.window.teamFilter = ''; dom.window.renderSessions();
  const selTeam = doc.getElementById('selTeam');
  const lockTag = doc.getElementById('teamLockTag');
  dom.window.openSession('s_1');     // fetch 桩对该会话返回 2 条记录
  await tick();
  check(selTeam.disabled === true, '已有对话的会话应把 composer 的 Team 下拉置灰（Team 不可改）');
  check(lockTag.style.display !== 'none', '应显示「Team 已锁定（已有对话）」标记');
  check(lockTag.textContent.indexOf('已锁定') >= 0, '锁定标记文案应说明已锁定');
  check(selTeam.value === 'tm_dual', '锁定时下拉仍显示本会话的 Team（不因锁定而清空）');
  // 点左栏 Team 名 ⇒ 中栏过滤该 Team 的会话（而不是只改 composer）
  dom.window.selectTeam('tm_fake');
  check(doc.getElementById('sessionList').querySelectorAll('.sess-card').length === 1,
    '点左栏 Team 应把中栏过滤到该 Team 的会话，实际 ' + doc.getElementById('sessionList').querySelectorAll('.sess-card').length);
  check(doc.querySelectorAll('.node.team.selected').length === 1
    && doc.querySelector('.node.team.selected').textContent.indexOf('调试Team') >= 0,
    '被过滤的 Team 节点应在左栏高亮');
  check(selTeam.value === 'tm_fake', '点左栏其他 Team 过滤后，右栏切到该 Team 的会话（composer 跟随显示 s_3 的 Team「调试Team」），实际 ' + selTeam.value);
  check(doc.getElementById('status').textContent.indexOf('锁定') >= 0, '锁定状态下点其他 Team 应给出可读提示');
  // 中栏过滤下拉反向联动左栏高亮
  fSel.value = 'tm_dual'; fSel.dispatchEvent(new dom.window.Event('change'));
  check(doc.querySelector('.node.team.selected') != null
    && doc.querySelector('.node.team.selected').textContent.indexOf('双核Team') >= 0,
    '中栏切过滤时左栏 Team 高亮应同步');
  // 没有发言的新会话：Team 可自由选择（首轮写回），点左栏 Team 照旧预选
  const realFetch = dom.window.fetch;
  dom.window.fetch = function () {
    return Promise.resolve({ ok: true, status: 200, json: function () { return Promise.resolve(
      { id: 's_new', title: '空会话', team_id: 'tm_dual', messages: [], agent_sessions: {}, exists: true, think: true }); } });
  };
  dom.window.openSession('s_new');
  await tick();
  check(selTeam.disabled === false, '还没有任何发言的会话应可自由选 Team（Team 只在首轮写回）');
  check(lockTag.style.display === 'none', '未锁定的会话不应显示锁定标记');
  dom.window.selectTeam('tm_fake');
  check(selTeam.value === 'tm_fake', '未锁定会话点左栏 Team 仍应预选进 composer（保留原语义）');
  dom.window.fetch = realFetch;
  dom.window.curProjSessions = [demoSession]; dom.window.teamFilter = ''; dom.window.renderSessions();
  // 还原 composer 的 Team：上面点过单 agent 的「调试Team」会把轮次置灰成 1，影响后续断言
  dom.window.curSid = null; dom.window.curTeamLocked = false;
  dom.window.selectTeam('tm_dual');

  // ---- 「思考」开关显示**生效值**（历史 bug：agent 级被设成关时，开关仍按"会话级 ?? 全局"
  //      渲染成勾着 ⇒ 用户以为思考开着，实际不产思考。后端下发 think_session/think_agents/
  //      think_effective 三件套，前端 applyThinkState 据此显示真实状态与来源）----
  const realFetch2 = dom.window.fetch;
  const thinkDetail = {
    id: 's_t', title: '思考态', team_id: 'tm_dual', messages: [], agent_sessions: {}, exists: true,
    think: true, think_session: null, think_effective: false,
    think_agents: [{ id: 'ag_arch', name: '架构师', think: false, source: 'agent' }],
  };
  dom.window.fetch = function () {
    return Promise.resolve({ ok: true, status: 200, json: function () { return Promise.resolve(thinkDetail); } });
  };
  dom.window.openSession('s_t'); await tick();
  const chkThink = doc.getElementById('chkThink');
  const hintTh = doc.getElementById('thinkHint');
  check(chkThink.checked === false, '会话级未设 + agent 级关 ⇒ 开关应显示生效值「关」（不再假装勾着）');
  check(hintTh.textContent.indexOf('生效：关') >= 0, '提示应写「生效：关」，实际 ' + JSON.stringify(hintTh.textContent));
  check(hintTh.className.indexOf('off') >= 0, '生效为关时提示应带 off 高亮类，实际 ' + JSON.stringify(hintTh.className));
  check(hintTh.title.indexOf('agent 级') >= 0, 'tooltip 应写明来源（agent 级），实际 ' + JSON.stringify(hintTh.title));

  // 会话级显式设过（开）→ 压过 agent 级 false
  thinkDetail.think_session = true; thinkDetail.think_effective = true;
  thinkDetail.think_agents = [{ id: 'ag_arch', name: '架构师', think: true, source: 'session' }];
  dom.window.applyThinkState(thinkDetail);
  check(chkThink.checked === true, '会话级为开时应压过 agent 级 false（勾选）');
  check(hintTh.textContent.indexOf('本会话已设') >= 0, '会话级已设时提示应写「本会话已设」，实际 ' + JSON.stringify(hintTh.textContent));

  // 各 agent 取值不一致 → 生效值未知，提示「各 agent 不同」，开关回退旧逻辑（think=true ⇒ 勾选）
  thinkDetail.think_session = null; thinkDetail.think_effective = null; thinkDetail.think = true;
  thinkDetail.think_agents = [
    { id: 'ag_arch', name: '架构师', think: false, source: 'agent' },
    { id: 'ag_impl', name: '实现者', think: true, source: 'global' },
  ];
  dom.window.applyThinkState(thinkDetail);
  check(hintTh.textContent.indexOf('各 agent 不同') >= 0, '多 agent 不一致时应提示「各 agent 不同」，实际 ' + JSON.stringify(hintTh.textContent));
  check(chkThink.checked === true, '各 agent 不一致时开关回退「会话级 ?? 全局」（此处全局开 ⇒ 勾选）');

  // 切换会话级开关后：提示应立刻变成「本会话已设」（不再让 agent 级决定显示）
  thinkDetail.think_session = true; thinkDetail.think_effective = true;
  dom.window.applyThinkState(thinkDetail);
  dom.window.toggleThink(false); await tick();
  check(chkThink.checked === false, 'toggleThink(false) 后开关应为未勾选');
  check(hintTh.textContent.indexOf('本会话已设') >= 0, 'toggleThink 成功后提示应刷新为「本会话已设」，实际 ' + JSON.stringify(hintTh.textContent));
  dom.window.fetch = realFetch2;

  // ---- 过滤后右栏应随中栏切换（用户反馈：过滤中栏后会话栏没一起变）----
  dom.window.curProjSessions = [
    { id: 's_a', title: '甲A', team_id: 'tm_dual', team_name: '双核Team', message_count: 1 },
    { id: 's_b', title: '甲B', team_id: 'tm_dual', team_name: '双核Team', message_count: 1 },
    { id: 's_c', title: '乙C', team_id: 'tm_fake', team_name: '调试Team', message_count: 1 },
  ];
  dom.window.teamFilter = ''; dom.window.renderSessions();
  dom.window.openSession('s_a'); await tick();
  check(dom.window.curSid === 's_a', '前置：先打开甲队会话 s_a，实际 ' + dom.window.curSid);
  // 过滤到 tm_fake（乙队）→ 右栏应切到该队第一个会话 s_c，中栏只显示 1 条
  dom.window.selectTeam('tm_fake'); await tick();
  check(dom.window.curSid === 's_c', '过滤到 tm_fake 后右栏应切换到该队第一个会话 s_c，实际 ' + dom.window.curSid);
  check(doc.getElementById('sessionList').querySelectorAll('.sess-card').length === 1,
    '过滤后中栏只显示乙队 1 条，实际 ' + doc.getElementById('sessionList').querySelectorAll('.sess-card').length);
  // 还原（避免单 agent Team 把「最大轮次」置灰成 1，影响后续断言）
  dom.window.curProjSessions = [demoSession]; dom.window.teamFilter = ''; dom.window.renderSessions();
  dom.window.curSid = null; dom.window.curTeamLocked = false;
  dom.window.curTid = 'tm_dual';
  var _sel = doc.getElementById('selTeam'); if (_sel) _sel.value = 'tm_dual';
  dom.window.updateRoundControl();

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
    // HITL 交互过程发言（interim）：内容可见，但标注「未转述给其他 agent」（对方只拿最后结论）
    dom.window.renderHistoryEntry({ speaker: 'g1', content: '又问了一次', interim: true, blocks: [] });
    var im = doc.querySelectorAll('#chat .bubble.interim');
    check(im.length === 1, '交互过程发言气泡应带 interim 样式类（历史渲染）');
    check((im[0].textContent || '').indexOf('未转述') >= 0, '交互过程气泡应标明未转述给对方 agent');
    // 实时流：turn_end.interim 与历史渲染走同一标记，刷新前后一致
    dom.window.chat.innerHTML = '';
    dom.window.curSid = 's_test';
    dom.window.handleEvent({ type: 'turn_start', agent: 'g1', sid: 's_test' });
    dom.window.handleEvent({ type: 'token', agent: 'g1', text: '实时又问了一次', sid: 's_test' });
    dom.window.handleEvent({ type: 'turn_end', agent: 'g1', interim: true, sid: 's_test' });
    check(doc.querySelectorAll('#chat .bubble.interim').length === 1, '实时 turn_end.interim 也应打标（与历史一致）');
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

    // ---- 同一 Team 被多项目共用：点哪个项目的 Team 只高亮那个，并切到所属项目（用户反馈）----
    const realCfg = dom.window.cfg;
    const realFetch2 = dom.window.fetch;
    const cfgMulti = {
      projects: [
        { id: 'pr_A', name: '项目A', path: '/tmp/a', team_ids: ['tm_shared', 'tm_a_only'] },
        { id: 'pr_B', name: '项目B', path: '/tmp/b', team_ids: ['tm_shared', 'tm_b_only'] },
      ],
      teams: [
        { id: 'tm_shared', name: '共享Team', agent_ids: ['ag_arch'] },
        { id: 'tm_a_only', name: 'A独享Team', agent_ids: ['ag_impl'] },
        { id: 'tm_b_only', name: 'B独享Team', agent_ids: ['ag_fake'] },
      ],
      agents: fakeConfig.agents,
      settings: fakeConfig.settings,
    };
    dom.window.cfg = cfgMulti;
    dom.window.expanded.projects = { pr_A: true, pr_B: true };
    dom.window.renderTree();
    const sharedNodes = Array.from(doc.querySelectorAll('.node.team')).filter((n) => n.textContent.indexOf('共享Team') >= 0);
    check(sharedNodes.length === 2, '共享 Team 应在 A、B 两个项目下各渲染一次，实际 ' + sharedNodes.length);

    // 手动置「点了 A 下的共享 Team」状态：只 A 的实例高亮
    dom.window.curPid = 'pr_A';
    dom.window.teamFilter = 'tm_shared';
    dom.window.curFilterPid = 'pr_A';
    dom.window.renderTree();
    let hl = Array.from(doc.querySelectorAll('.node.team.selected')).filter((n) => n.textContent.indexOf('共享Team') >= 0);
    check(hl.length === 1, '共用 Team 只应高亮「点中的项目」下那一个实例，实际 ' + hl.length);

    // 切到「点 B 下的共享 Team」：高亮应移到 B 的实例，A 的不亮
    dom.window.teamFilter = 'tm_shared';
    dom.window.curFilterPid = 'pr_B';
    dom.window.renderTree();
    const hlB = Array.from(doc.querySelectorAll('.node.team.selected'));
    check(hlB.length === 1 && hlB[0].textContent.indexOf('共享Team') >= 0, '高亮切到 B 后应只剩 B 下的共享 Team 实例');
    check(Array.from(doc.querySelectorAll('.node.team.selected')).filter((n) => n.textContent.indexOf('A独享Team') >= 0).length === 0,
      '切到 B 后 A 下的 Team 不应再高亮（跨项目误高亮 bug 修复）');

    // 点 B 项目下的共享 Team：应切到项目 B + 按该 Team 过滤中栏 + composer 落在该 Team
    dom.window.curPid = 'pr_A';
    dom.window.curSid = null; dom.window.curTid = null;
    dom.window.curProjSessions = [];
    dom.window.teamFilter = ''; dom.window.curFilterPid = null;
    dom.window.selectTeam('tm_shared', 'pr_B');   // selectProject 同步设置 curPid/curFilterPid/teamFilter 后才异步拉取会话
    check(dom.window.curPid === 'pr_B', '点 B 项目下的共享 Team 应切到项目 B（同步），实际 ' + dom.window.curPid);
    check(dom.window.curFilterPid === 'pr_B', 'curFilterPid 应记为 pr_B（点中的项目，而非 owningProjectOf 的第一个），实际 ' + dom.window.curFilterPid);
    check(dom.window.teamFilter === 'tm_shared', 'teamFilter 应设为被点的 Team，实际 ' + dom.window.teamFilter);
    const hlAfter = Array.from(doc.querySelectorAll('.node.team.selected')).filter((n) => n.textContent.indexOf('共享Team') >= 0);
    check(hlAfter.length === 1, '切到 B 后只应高亮 B 下的共享 Team 实例，实际 ' + hlAfter.length);
    // 中栏按 B 项目加载会话是 selectProject→loadProjectSessions 的异步分支（单项目场景已由上方测试覆盖），
    // 此处只验证同步层面的「切项目 + 高亮归属精确到项目 + composer 落到该 Team」已正确（核心修复点）。
    check(doc.getElementById('selTeam').value === 'tm_shared', 'composer 下拉应落到被点的共享 Team，实际 ' + doc.getElementById('selTeam').value);
    dom.window.fetch = realFetch2;
    dom.window.cfg = realCfg;

    // ---- 回归：点「空 team」时 composer 必须立即落到该 team，而非残留上一个 team（曾因 disabled 守卫跳过赋值，需点两次）----
    // selectProject 同步填充 composer 下拉（含 tm_fake 空 team 选项），不依赖异步拉取。
    dom.window.selectProject('pr_demo');
    // 模拟「已打开一个锁定 Team 的会话」：手动锁定 composer（applyTeamFilter 在 disabled 时也曾被守卫跳过赋值）
    dom.window.curSid = 's_1';
    dom.window.curTid = 'tm_dual';
    dom.window.curTeamLocked = true;
    dom.window.applyTeamLock();
    check(doc.getElementById('selTeam').disabled === true, '锁定态下 composer Team 下拉应禁用');
    var prevVal = doc.getElementById('selTeam').value;
    dom.window.selectTeam('tm_fake', 'pr_demo');   // tm_fake 在本项目下无会话（空 team）
    check(doc.getElementById('selTeam').value === 'tm_fake',
      '点空 team 后 composer 应直接落到该空 team（不残留 ' + prevVal + '），实际 ' + doc.getElementById('selTeam').value);
    check(doc.getElementById('selTeam').disabled === false,
      '点空 team 清空会话后锁定应解除，实际 disabled=' + doc.getElementById('selTeam').disabled);

    // ---- 自动滚动跟随：仅当用户「贴在底部」时追加内容才自动下滚；上拉查看历史后不得被拉回 ----
    // （jsdom 的 scrollHeight/clientHeight 恒为 0，故用 defineProperty 造出可变的真实布局尺寸）
    var chatEl = doc.getElementById('chat');
    var _sc = { h: 2000, c: 400 };
    Object.defineProperty(chatEl, 'scrollHeight', { configurable: true, get: function () { return _sc.h; } });
    Object.defineProperty(chatEl, 'clientHeight', { configurable: true, get: function () { return _sc.c; } });
    chatEl.dispatchEvent(new dom.window.Event('scroll'));
    // 贴底态（2000 - 1600 - 400 = 0）
    chatEl.scrollTop = 1600;
    chatEl.dispatchEvent(new dom.window.Event('scroll'));
    check(dom.window.chatStickBottom === true, '在底部时状态应为「跟随」（实际 ' + dom.window.chatStickBottom + '）');
    _sc.h = 2400;   // 模拟流式追加内容撑高
    dom.window.appendThinking('A', '思考内容', 'claude-code');
    check(chatEl.scrollTop === 2400, '贴底时追加内容应自动滚到底，实际 ' + chatEl.scrollTop);
    // 用户上拉查看历史
    chatEl.scrollTop = 300;
    chatEl.dispatchEvent(new dom.window.Event('scroll'));
    check(dom.window.chatStickBottom === false, '上拉离开底部后应停止跟随（实际 ' + dom.window.chatStickBottom + '）');
    var keepTop = chatEl.scrollTop;
    _sc.h = 3000;
    dom.window.appendToken('A', '后续正文', 'claude-code');
    check(chatEl.scrollTop === keepTop, '用户上拉后追加内容不得把滚动条拉回底部，实际 ' + chatEl.scrollTop);
    dom.window.appendToolCall('bash', { cmd: 'ls' }, 'claude-code');
    check(chatEl.scrollTop === keepTop, '用户上拉后工具块到达也不得抢滚动，实际 ' + chatEl.scrollTop);
    // 重新拉回底部 ⇒ 恢复跟随
    chatEl.scrollTop = 2600;   // 3000 - 2600 - 400 = 0
    chatEl.dispatchEvent(new dom.window.Event('scroll'));
    check(dom.window.chatStickBottom === true, '拉回底部后应恢复跟随（实际 ' + dom.window.chatStickBottom + '）');
    // 自己发消息：强制回到底部（用户主动动作）
    chatEl.scrollTop = 300;
    chatEl.dispatchEvent(new dom.window.Event('scroll'));
    dom.window.addRow('human', '我', '新任务');
    check(chatEl.scrollTop === 3000, '自己发消息应强制回到底部，实际 ' + chatEl.scrollTop);

    // ---- HITL：agent 请求人工介入面板（request_human_input → 暂停等人，自由文本回复）----
    const hitlPanel = doc.getElementById('hitlPanel');
    check(hitlPanel.style.display === 'none', 'HITL 面板默认应隐藏');
    // 选项列表 UI 已移除：不再渲染可点选按钮，用户用自然语言自由回复
    check(doc.getElementById('hitlOptions') === null, '选项容器 hitlOptions 应已移除（不再出列表让用户选）');
    // 非当前会话的请求：不应弹面板，也不应泄漏进当前聊天区（并行会话互不污染）
    dom.window.curSid = 's_b';
    dom.window.handleEvent({ type: 'human_input_required', sid: 's_other', agent: '架构师',
      question: '选哪个方案？', options: [] });
    check(hitlPanel.style.display === 'none', '非当前会话的 human_input_required 不应弹面板');
    check(doc.getElementById('chat').textContent.indexOf('选哪个方案') < 0, '非当前会话请求不得泄漏进当前聊天区');
    // 当前会话请求：弹面板 + 渲染提问 agent / 问题（无选项按钮）
    dom.window.handleEvent({ type: 'human_input_required', sid: 's_b', agent: '架构师',
      question: '选哪个方案？', options: [] });
    check(hitlPanel.style.display === 'block', '当前会话 human_input_required 应弹出 HITL 面板');
    check(doc.getElementById('hitlAgent').textContent.indexOf('架构师') >= 0, '面板应显示提问 agent 名');
    check(doc.getElementById('hitlQuestion').textContent.indexOf('选哪个方案') >= 0, '面板应显示问题文本');
    // 用户自由输入回答（不再点选按钮）
    const ansEl = doc.getElementById('hitlAnswer');
    ansEl.value = '按方案A推进，但预算砍一半';
    // 捕获 WS 发送，提交回答
    const captured = [];
    dom.window.WS.send = function (m) { captured.push(m); };
    dom.window.submitHumanInput();
    check(captured.length === 1, '提交应发送 1 条 WS 消息，实际 ' + captured.length);
    let sent = null; try { sent = JSON.parse(captured[0]); } catch (e) {}
    check(sent && sent.action === 'human_input', '提交 WS 动作应为 human_input，实际 ' + (sent && sent.action));
    check(sent && sent.text === '按方案A推进，但预算砍一半', '提交内容应为自由文本回答，实际 ' + (sent && sent.text));
    check(sent && sent.session_id === 's_b', '提交应带当前会话 id，实际 ' + (sent && sent.session_id));
    check(hitlPanel.style.display === 'none', '提交回答后面板应隐藏');
    // 切会话（composeClear）应关闭残留面板
    dom.window.handleEvent({ type: 'human_input_required', sid: 's_b', agent: '实现者', question: '另一个问题', options: [] });
    check(hitlPanel.style.display === 'block', '再次收到当前会话请求应重新弹面板');
    dom.window.composeClear();
    check(hitlPanel.style.display === 'none', '切会话（composeClear）应关闭残留的 HITL 面板');

    // ---- 气泡颜色按「发言者名」解析 agent 类型；名字不在配置里 → 中性灰 unknown（不伪装成 claude 蓝）----
    // 历史 bug：导入/改名过的历史会话 speaker 名对不上配置，回落 claude-code，
    // 导致 codex 会话显示成 claude 蓝气泡、fake 陪练也显示成蓝的，看上去像 claude 在说话。
    const chatEl2 = doc.getElementById('chat');
    chatEl2.innerHTML = '';
    dom.window.renderHistoryEntry({ round: 0, speaker: '架构师', role: '架构师', content: '先定义接口', ts: '' });
    dom.window.renderHistoryEntry({ round: 1, speaker: '实现者', role: '实现者', content: '我来写', ts: '' });
    dom.window.renderHistoryEntry({ round: 2, speaker: 'H-Probe Codex', role: '', content: '旧探针名（配置里已无此 agent）', ts: '' });
    const bubs = Array.from(chatEl2.querySelectorAll('.bubble'));
    check(bubs.length === 3, '应渲染 3 个气泡，实际 ' + bubs.length);
    check(bubs[0] && bubs[0].classList.contains('claude-code'), '名字命中 claude-code agent → 气泡类 claude-code');
    check(bubs[1] && bubs[1].classList.contains('codex'), '名字命中 codex agent → 气泡类 codex（绿）');
    check(bubs[2] && bubs[2].classList.contains('unknown') && !bubs[2].classList.contains('claude-code'),
      '名字不在配置里 → 气泡类 unknown，不得回落 claude-code，实际 ' + (bubs[2] && bubs[2].className));
    check(bubs[1] && cs(bubs[1]).backgroundColor === 'rgb(230, 246, 236)',
      'codex 气泡背景应为绿(#e6f6ec)，实际 ' + (bubs[1] && cs(bubs[1]).backgroundColor));
    check(bubs[2] && cs(bubs[2]).backgroundColor === 'rgb(242, 243, 247)',
      'unknown 气泡背景应为中性灰(#f2f3f7)，实际 ' + (bubs[2] && cs(bubs[2]).backgroundColor));
    check(bubs[2] && cs(bubs[2]).backgroundColor !== cs(bubs[0]).backgroundColor,
      'unknown 气泡颜色必须与 claude-code 明显不同（避免误读成 claude 发言）');
    // 实时流同理：turn_start 未带 atype 时不得默认成 claude 蓝
    chatEl2.innerHTML = '';
    dom.window.cur = null; dom.window.agentNameColor = {};
    dom.window.handleEvent({ type: 'turn_start', agent: '未登记的发言者', sid: 's_b' });
    dom.window.handleEvent({ type: 'token', agent: '未登记的发言者', text: '实时流正文', sid: 's_b' });
    const live = chatEl2.querySelector('.bubble');
    check(live && live.classList.contains('unknown'),
      '实时流：turn_start 未带 atype 时气泡应为 unknown，实际 ' + (live && live.className));

    if (!ok) { console.error('前端渲染回归测试失败'); process.exit(1); }
    console.log('前端渲染回归测试通过 ✓');
    process.exit(0);
  }, 80);
}, 600);
