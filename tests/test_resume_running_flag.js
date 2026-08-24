// 回归测试（jsdom）：复现「继续」棋局时 g1 不显示的竞态根因。
//
// 根因：ef3162f 给 run_end 的 250ms 重载加了守卫 `if (runningBySid[curSid]) return;`，
// 但该守卫依赖 runningBySid[curSid]===true。runningBySid 置 true 的唯一来源是 startSession，
// 而「继续」按钮走 ctrl('resume') 从不置位，后端也不发 run_start —— 守卫对续接路径永远失效，
// 续接首个 agent(g1) 刚流式渲染的内容会被重载抹掉（g1 输出消失、刷新才见）。
//
// 本测试锁住：ctrl('resume') 必须立即把 runningBySid[sid] 置 true（与 startSession 一致），
// 这样上一次 run_end 已调度的 250ms 重载会被守卫跳过，g1 不被抹掉。
//
// 运行：
//   NODE_PATH=<managed node workspace>/node_modules node tests/test_resume_running_flag.js
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

// 会话含 g1/g2/g3 历史，证明打开后三方都能渲染
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

  const chat0 = doc.getElementById('chat');
  check(chat0.textContent.indexOf('黑e5') >= 0, '打开会话后 g1 历史(黑e5)应可见');
  check(chat0.textContent.indexOf('白d5') >= 0, 'g2 历史(白d5)应可见');
  check(chat0.textContent.indexOf('校验通过') >= 0, 'g3 历史(校验通过)应可见');

  // ---- 复现续接竞态：上一次 run_end 已调度 250ms 重载，用户在窗口内点「继续」 ----
  w.handleEvent({ type: 'run_end', mode: 'debate', sid: 's_x' }); // 调度 reload(+250ms)
  w.ctrl('resume');                                               // 点「继续」按钮
  check(w.runningBySid['s_x'] === true,
    'ctrl(resume) 必须立即置 runningBySid[s_x]=true；否则 250ms 重载守卫失效、g1 被抹（修复前此处为 false）');

  // ---- 续接 run 流式：g1 新一轮发言，run 结束 ----
  w.handleEvent({ type: 'turn_start', agent: 'g1', atype: 'claude-code', sid: 's_x' });
  w.handleEvent({ type: 'token', agent: 'g1', text: '黑b3', sid: 's_x' });
  w.handleEvent({ type: 'run_end', mode: 'debate', sid: 's_x' });

  await tick(320); // 等 250ms 重载定时器触发
  const chat1 = doc.getElementById('chat');
  check(chat1.textContent.indexOf('黑e5') >= 0, '续接后 g1 内容(黑e5)应仍可见，未被重载抹掉');
  check(w.runningBySid['s_x'] === false, 'run_end 后 runningBySid 应复位为 false（按钮恢复可点）');

  if (!ok) { console.error('resume running 标志回归测试失败'); process.exit(1); }
  console.log('resume running 标志回归测试通过 ✓');
  process.exit(0);
}, 600);
