// 回归测试（jsdom）：「新建/编辑 Agent」弹窗的类型下拉必须能真正切走 claude-code。
//
// 原 bug（用户实测）：新建 Agent 时类型只能选 Claude Code，选 codex/fake 会立刻弹回 claude-code。
// 根因：openAgentForm 的 sync 用「原始实体 a.type」作比较基准，新建时 a 为空 → 基准恒为
// 'claude-code'，故选 codex/fake 时 `t !== 基准` 成立 → 走 openAgentForm(a) 重渲染，
// 而 a 为空又渲染回 claude-code 默认值，形成"选了自动变回 claude"的现象（且顺手清空已填的 name/role）。
// 修复：基准改为「本表单渲染时的 curType」，并传 agentDraft（保留已填值 + 目标类型）重渲染。
//
// 运行：
//   /Users/stalitz/.workbuddy/binaries/node/versions/22.22.2-2/bin/node tests/test_agent_type_switch.js
const { JSDOM } = require('jsdom');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(ROOT, 'frontend', 'index.html'), 'utf8');

const fakeConfig = {
  projects: [{ id: 'pr_x', name: 'demo', path: '/tmp/demo', team_ids: ['tm_2g'] }],
  teams: [{ id: 'tm_2g', name: '2g', agent_ids: ['g1', 'g2'] }],
  agents: [
    { id: 'g1', name: 'g1', type: 'claude-code' },
    { id: 'g2', name: 'g2', type: 'codex', model: 'deepseek-v4-flash' },
  ],
  settings: { default_timeout: 300, default_max_rounds: 20 },
};

const dom = new JSDOM(html, {
  runScripts: 'dangerously',
  beforeParse(window) {
    window.WebSocket = function () { this.send = () => {}; this.close = () => {}; };
    window.fetch = function () {
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(fakeConfig) });
    };
  },
});

let ok = true;
function check(cond, msg) {
  if (!cond) { console.error('FAIL: ' + msg); ok = false; }
  else { console.log('PASS: ' + msg); }
}

setTimeout(() => {
  const w = dom.window, doc = w.document;
  w.cfg = fakeConfig;

  // 切换 select 的值并触发 change（真实用户操作路径）
  function setType(val) {
    const el = doc.getElementById('type');
    el.value = val;
    el.dispatchEvent(new w.Event('change'));
    return doc.getElementById('type');
  }

  // ---- 场景 1：新建 Agent，切到 codex ----
  w.openAgentForm();
  check(doc.getElementById('type').value === 'claude-code', '新建 Agent 默认类型为 claude-code');

  let t = setType('codex');
  check(t.value === 'codex', '新建 Agent 选 Codex 后类型应保持 codex（修复前被弹回 claude-code）');
  check(!!doc.getElementById('model'), '切到 Codex 后应显示「模型」字段');
  check(!doc.getElementById('modelSrc'), '切到 Codex 后不应再显示「模型来源」字段');

  // ---- 场景 2：继续切到 fake ----
  t = setType('fake');
  check(t.value === 'fake', '新建 Agent 选 Fake 后类型应保持 fake');
  check(!doc.getElementById('model'), 'Fake 类型不应显示模型字段');

  // ---- 场景 3：切类型不能清空已填的名称 ----
  const nameEl = doc.getElementById('name');
  nameEl.value = '裁判';
  t = setType('codex');
  check(doc.getElementById('name').value === '裁判', '切类型后已填的名称应保留（修复前被重渲染清空）');
  check(t.value === 'codex', '保留输入的同时类型仍应为 codex');

  // ---- 场景 4：编辑已有 claude-code agent，也能切到 codex ----
  w.openAgentForm({ id: 'g1', name: 'g1', type: 'claude-code' });
  check(doc.getElementById('type').value === 'claude-code', '编辑 g1 时类型显示为其原类型 claude-code');
  t = setType('codex');
  check(t.value === 'codex', '编辑已有 Agent 也允许切到 codex（修复前同样弹回）');
  check(!!doc.getElementById('model'), '编辑态切 Codex 后应显示模型字段');

  // ---- 场景 5：切类型不得丢失 id（否则保存会新建一个 agent 而非更新） ----
  check(w.editingId === 'g1', '编辑态切类型后 editingId 必须仍是 g1（否则保存会新建而非更新）');

  if (!ok) { console.error('Agent 类型切换回归测试失败'); process.exit(1); }
  console.log('Agent 类型切换回归测试通过 ✓');
  process.exit(0);
}, 600);
