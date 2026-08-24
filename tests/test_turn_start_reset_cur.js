// 回归测试（jsdom）：复现「上一轮被打断未发 turn_end，下一轮内容合并进旧气泡」的竞态。
//
// 根因：handleEvent 的 turn_start 分支此前只置 runningBySid + showLoading，从不把 cur 复位 null。
// 新气泡靠「首个 thinking/token 事件时 if(!cur) openTurn」懒建，依赖每轮 cur 为 null。
// 若上一轮（如 g2）在流式中途被用户介入打断、其 turn_end 没发出，cur 仍指向 g2 气泡；
// 下一轮 turn_start(g1) 不清 cur，于是 g1 的 token 被 append 进 g2 气泡（实测：g1 内容跑到 g2 气泡里）。
//
// 本测试锁住：turn_start 必须把 cur/curThinking/curAnswer 等游标复位，使本轮首个 token
// 落入独立的新气泡，绝不污染上一轮气泡。
//
// 运行：
//   NODE_PATH=<managed node workspace>/node_modules node tests/test_turn_start_reset_cur.js
const { JSDOM } = require('jsdom');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(ROOT, 'frontend', 'index.html'), 'utf8');

const fakeConfig = {
  projects: [{ id: 'pr_x', name: 'demo', path: '/tmp/demo', team_ids: ['tm_x'] }],
  teams: [{ id: 'tm_x', name: '三核Team', agent_ids: ['g1', 'g2', 'g3'] }],
  agents: [
    { id: 'g1', name: 'g1', type: 'claude-code' },
    { id: 'g2', name: 'g2', type: 'claude-code' },
    { id: 'g3', name: 'g3', type: 'claude-code' },
  ],
  settings: { default_timeout: 300, default_max_rounds: 20 },
};

// 会话含历史，证明打开后聊天区可用
const sessionDetail = {
  id: 's_x', title: '五子棋', team_id: 'tm_x',
  messages: [
    { round: 0, speaker: 'g1', role: '黑', content: '黑e5' },
    { round: 0, speaker: 'g2', role: '白', content: '白d5' },
    { round: 0, speaker: 'g3', role: '裁判', content: '校验通过' },
  ],
  agent_sessions: {}, exists: true,
};

const dom = new JSDOM(html, {
  runScripts: 'dangerously',
  beforeParse(window) {
    window.WebSocket = function () { this.send = () => {}; this.close = () => {}; };
    window.fetch = function (url) {
      if (typeof url === 'string' && url.indexOf('/api/session/') >= 0 && url.indexOf('/tools') >= 0) {
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ tools: [] }) });
      }
      if (typeof url === 'string' && url.indexOf('/api/session/') >= 0) {
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(sessionDetail) });
      }
      if (typeof url === 'string' && url.indexOf('/api/sessions/') >= 0) {
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ sessions: [{ id: 's_x', title: '五子棋', project_id: 'pr_x', team_id: 'tm_x', message_count: 3 }] }) });
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
const tick = (ms = 60) => new Promise((r) => setTimeout(r, ms));

setTimeout(async () => {
  const w = dom.window, doc = w.document;
  w.curPid = 'pr_x';
  await w.loadProjectSessions('pr_x');
  w.curSid = 's_x';
  w.openSession('s_x');
  await tick(80);
  const chat = doc.getElementById('chat');

  // 模拟「上一轮 g2 流式中途被打断、其 turn_end 从未发出」→ cur 残留指向 g2 气泡
  w.handleEvent({ type: 'turn_start', agent: 'g2', atype: 'claude-code', sid: 's_x' });
  w.handleEvent({ type: 'token', agent: 'g2', text: 'STALE_G2', sid: 's_x' });
  // 注意：此处刻意不调用 turn_end(g2)，模拟被打断

  // 新一轮：turn_start(g1) 随后 g1 流式
  w.handleEvent({ type: 'turn_start', agent: 'g1', atype: 'claude-code', sid: 's_x' });
  w.handleEvent({ type: 'token', agent: 'g1', text: 'FRESH_G1', sid: 's_x' });

  const bubbles = Array.from(chat.querySelectorAll('.bubble'));
  const fresh = bubbles.find((b) => b.textContent.indexOf('FRESH_G1') >= 0);
  const stale = bubbles.find((b) => b.textContent.indexOf('STALE_G2') >= 0);

  check(!!fresh, '应存在含 FRESH_G1 的气泡');
  check(!!stale, '应存在含 STALE_G2 的气泡');
  check(fresh && fresh.querySelector('.who').textContent === 'g1',
    'FRESH_G1 所在气泡的发言者必须是 g1（修复前会落在 g2 气泡，此处断言失败）');
  check(stale && stale.querySelector('.who').textContent === 'g2',
    'STALE_G2 所在气泡的发言者必须是 g2');
  check(fresh !== stale, 'g1 与 g2 必须是两个不同气泡，不得合并到同一气泡');

  if (!ok) { console.error('turn_start 复位 cur 回归测试失败'); process.exit(1); }
  console.log('turn_start 复位 cur 回归测试通过 ✓');
  process.exit(0);
}, 600);
