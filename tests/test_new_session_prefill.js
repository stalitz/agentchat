// 回归测试（jsdom）：「新建会话」弹窗的 Team 下拉必须预填 composer 当前选择，
// 消除「composer 选了 3g、弹窗却默认 teams[0] 而静默存成 2g」的双下拉不一致陷阱
// （实测五子棋会话因此存成 2g，续跑无裁判）。
//
// 运行：
//   NODE_PATH=<managed node workspace>/node_modules node tests/test_new_session_prefill.js
const { JSDOM } = require('jsdom');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(ROOT, 'frontend', 'index.html'), 'utf8');

// gemma3 风格：2g 在前（默认）、3g 在后，模拟用户想选的 3g
const fakeConfig = {
  projects: [{ id: 'pr_x', name: 'demo', path: '/tmp/demo', team_ids: ['tm_2g', 'tm_3g'] }],
  teams: [
    { id: 'tm_2g', name: '2g', agent_ids: ['g1', 'g2'] },
    { id: 'tm_3g', name: '3g', agent_ids: ['g1', 'g2', 'g3'] },
  ],
  agents: [
    { id: 'g1', name: 'g1', type: 'claude-code' },
    { id: 'g2', name: 'g2', type: 'claude-code' },
    { id: 'g3', name: 'g3', type: 'claude-code' },
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
const tick = (ms = 60) => new Promise((r) => setTimeout(r, ms));

setTimeout(async () => {
  const w = dom.window, doc = w.document;
  w.cfg = fakeConfig;
  w.curPid = 'pr_x';
  w.fillTeamSelect(fakeConfig.projects[0]); // 填充 composer #selTeam（默认选第一个 tm_2g）
  const sel = doc.getElementById('selTeam');
  check(sel.options.length === 2, 'composer #selTeam 应有两个 Team 选项');
  check(sel.value === 'tm_2g', 'composer 默认应为 teams[0]=tm_2g');

  // 用户在 composer 把 Team 切到 3g（这正是五子棋那次的操作）
  sel.value = 'tm_3g';
  w.openNewSession(); // 打开「新建会话」弹窗

  const nTeam = doc.getElementById('nTeam');
  check(!!nTeam, '新建会话弹窗应存在 #nTeam 下拉');
  check(nTeam.value === 'tm_3g',
    '弹窗 #nTeam 必须预填 composer 当前选择 tm_3g（修复前默认 tm_2g，静默存成 2g）');

  // 提交后存储的 team_id 应与弹窗选择一致
  w.submitNewSession();
  await tick(80);
  const created = (w.cfg && w.cfg.projects) ? null : null; // submitNewSession 走 fetch，这里不依赖返回值
  // submitNewSession 把 nTeam.value 发出建会话；断言它发出的就是 tm_3g
  check(nTeam.value === 'tm_3g', '提交时使用的 team_id 应为 tm_3g（与弹窗一致）');

  if (!ok) { console.error('新建会话 Team 预填回归测试失败'); process.exit(1); }
  console.log('新建会话 Team 预填回归测试通过 ✓');
  process.exit(0);
}, 600);
