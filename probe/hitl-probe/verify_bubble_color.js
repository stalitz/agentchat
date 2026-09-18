// 气泡颜色「真数据 + 真 DOM」验证（jsdom）：拉 8777 的真实 /api/config 与真实会话，
// 用 frontend/index.html 的真实渲染函数渲染，断言每个发言者的气泡颜色类正确。
//
// 背景（2026-09-19）：导入的探针会话 speaker 名（H-Probe Codex / Probe Fake …）在项目配置里
// 不存在 ⇒ 前端 cfg_agents_by_name 查不到 ⇒ 回落到 claude-code（蓝），codex 会话显示成蓝气泡、
// fake 陪练也显示成蓝的。修复：① 导入脚本按名改名；② 前端未知名改中性灰 unknown（不再伪装 claude）。
//
// 运行（需 jsdom）：
//   NODE_PATH=<managed node workspace>/node_modules \
//   node probe/hitl-probe/verify_bubble_color.js
const http = require('http');
const path = require('path');
const fs = require('fs');
const { JSDOM } = require('jsdom');

const ROOT = path.resolve(__dirname, '..', '..');
const HTML = fs.readFileSync(path.join(ROOT, 'frontend', 'index.html'), 'utf8');
const BASE = 'http://127.0.0.1:8777';
const PID = 'pr_0335148e';

// 要验证的会话：sid → 期望的「发言者名 → 气泡颜色类」
const CASES = [
  { sid: 's_0449ef1d', note: 'HITL 探针 · codex 单壳', expect: { 'Codex (本地 gemma)': 'codex' } },
  { sid: 's_a3d8022f', note: 'HITL 探针 · 双壳',
    expect: { 'Codex (本地 gemma)': 'codex', 'Claude Code (本地 gemma)': 'claude-code' } },
  { sid: 's_54eb6e23', note: 'HITL 探针 · claude 单壳',
    expect: { 'Claude Code 单壳 (本地 gemma)': 'claude-code' } },
  { sid: 's_3100f43b', note: '网关复现 · claude vs Fake 陪练',
    expect: { 'Claude Code (本地 gemma)': 'claude-code', 'Fake 陪练（调试 · 不烧 token）': 'fake' } },
];

function get(url) {
  return new Promise((resolve, reject) => {
    http.get(url, (res) => {
      let b = '';
      res.on('data', (c) => { b += c; });
      res.on('end', () => { try { resolve(JSON.parse(b)); } catch (e) { reject(e); } });
    }).on('error', reject);
  });
}

let ok = true;
function check(cond, msg) {
  if (!cond) { console.error('FAIL: ' + msg); ok = false; } else { console.log('PASS: ' + msg); }
}

(async () => {
  const cfg = await get(`${BASE}/api/config`);
  const details = {};
  for (const c of CASES) {
    details[c.sid] = await get(`${BASE}/api/session/${PID}/${c.sid}`);
  }

  const html = HTML;
  const dom = new JSDOM(html, {
    runScripts: 'dangerously',
    beforeParse(window) {
      window.WebSocket = function () { this.send = () => {}; this.close = () => {}; };
      window.fetch = function (url) {
        if (typeof url === 'string' && url.indexOf('/api/session/') >= 0) {
          const sid = url.split('/').pop();
          const d = Object.values(details).find((x) => x.id === sid);
          return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(d || {}) });
        }
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(cfg) });
      };
    },
  });

  // loadConfig 是异步链，等它把 cfg 灌进 window.cfg
  await new Promise((r) => setTimeout(r, 400));
  const doc = dom.window.document;
  const chat = doc.getElementById('chat');

  for (const c of CASES) {
    const d = details[c.sid];
    chat.innerHTML = '';
    for (const m of (d.messages || [])) {
      if (m.speaker === 'human' || m.speaker === 'system') continue;
      dom.window.renderHistoryEntry(m);
    }
    const bubbles = Array.from(chat.querySelectorAll('.bubble'));
    console.log(`\n-- ${c.note} (${c.sid}) 气泡 ${bubbles.length} 个`);
    for (const b of bubbles) {
      const who = (b.querySelector('.who') || {}).textContent || '';
      const name = Object.keys(c.expect).find((n) => who.indexOf(n) === 0) || '(未知发言者)';
      const want = c.expect[name];
      const got = ['claude-code', 'codex', 'fake', 'unknown'].find((t) => b.classList.contains(t));
      console.log(`   ${name.padEnd(28)} class=${b.className}  背景=${dom.window.getComputedStyle(b).backgroundColor}`);
      if (want) {
        check(got === want, `${c.sid}: 「${name}」气泡应为 ${want}，实际 ${got}`);
      } else {
        check(got === 'unknown', `${c.sid}: 未登记发言者「${name}」应为 unknown，实际 ${got}`);
      }
    }
  }

  if (!ok) { console.error('\n气泡颜色验证失败'); process.exit(1); }
  console.log('\n气泡颜色验证通过 ✓');
  process.exit(0);
})().catch((e) => { console.error(e); process.exit(1); });
