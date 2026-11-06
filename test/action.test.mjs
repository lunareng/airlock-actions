// The action's two steps against a stand-in for GitHub's OIDC endpoint, Airlock and GitHub's API (a preloaded
// fetch), no network: each step runs as the runner runs it, a node process with INPUT_*, GITHUB_OUTPUT,
// GITHUB_STATE and, for the post step, STATE_*. A grant's token reaches later steps only through the masked
// output and the post step only through state; anything but a grant or shadow stops the run; the post step never
// fails and never prints a token. Run: npm test
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { existsSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const MAIN = new URL('../src/main.mjs', import.meta.url).pathname;
const POST = new URL('../src/post.mjs', import.meta.url).pathname;
const VERSION = JSON.parse(readFileSync(new URL('../package.json', import.meta.url), 'utf8')).version;
const AIRLOCK = 'https://airlock.example';
const TOKEN = 'ghs_TESTONLYabcdef0123456789';

// One step. `answers` says what each route answers: { mint, revoke, end } as { status, body }, or 'down'; a list is
// one answer per call, its last answering every call after. A wait of a second or more (a retry's) is written to
// `waits` and not waited for.
function step(script, { answers = {}, env = {}, state = {} } = {}) {
  const dir = mkdtempSync(join(tmpdir(), 'airlock-action-'));
  const sent = join(dir, 'sent');
  const waits = join(dir, 'waits');
  const stub = join(dir, 'stub.mjs');
  writeFileSync(stub, `
    import { appendFileSync } from 'node:fs';
    const answers = ${JSON.stringify(answers)};
    let n = 0;
    const calls = {};
    const answer = (route, fallback) => {
      const all = answers[route];
      const i = calls[route] = (calls[route] ?? -1) + 1;
      const a = Array.isArray(all) ? all[Math.min(i, all.length - 1)] : all;
      if (a === 'down') throw new TypeError('fetch failed');
      const { status, body } = a ?? fallback;
      return new Response(status === 204 ? null : typeof body === 'string' ? body : JSON.stringify(body), { status });
    };
    const later = globalThis.setTimeout;
    globalThis.setTimeout = (fn, ms, ...args) => {
      if (ms >= 1000) { appendFileSync(${JSON.stringify(waits)}, ms + '\\n'); ms = 0; }
      return later(fn, ms, ...args);
    };
    globalThis.fetch = async (url, init = {}) => {
      const u = new URL(String(url));
      const h = Object.fromEntries(Object.entries(init.headers ?? {}).map(([k, v]) => [k.toLowerCase(), v]));
      appendFileSync(${JSON.stringify(sent)}, JSON.stringify({ url: u.href, method: init.method ?? 'GET', headers: h, body: init.body ? JSON.parse(init.body) : null }) + '\\n');
      if (u.origin === 'https://oidc.example') return new Response(JSON.stringify({ value: 'eyJoidc-' + process.pid + '-' + ++n }));
      if (u.pathname === '/v1/mint') return answer('mint', { status: 418, body: {} });
      if (u.pathname === '/v1/revoke') return answer('revoke', { status: 200, body: { revoked: true, run: '7.1' } });
      if (u.href === 'https://api.github.test/installation/token') return answer('end', { status: 204, body: '' });
      return new Response('{}', { status: 418 });
    };`);
  const output = join(dir, 'output');
  const stateFile = join(dir, 'state');
  const envFile = join(dir, 'env');
  writeFileSync(output, '');
  writeFileSync(stateFile, '');
  writeFileSync(envFile, '');
  const r = spawnSync(process.execPath, ['--import', stub, script], {
    env: {
      PATH: process.env.PATH,
      ACTIONS_ID_TOKEN_REQUEST_URL: 'https://oidc.example/token?api-version=2.0',
      ACTIONS_ID_TOKEN_REQUEST_TOKEN: 'req',
      GITHUB_OUTPUT: output,
      GITHUB_STATE: stateFile,
      GITHUB_ENV: envFile,
      GITHUB_API_URL: 'https://api.github.test',
      INPUT_ROLE: 'triage',
      'INPUT_AIRLOCK-URL': AIRLOCK,
      ...Object.fromEntries(Object.entries(state).map(([k, v]) => [`STATE_${k}`, v])),
      ...env,
    },
    encoding: 'utf8',
  });
  const kv = (f) => Object.fromEntries(readFileSync(f, 'utf8').split('\n').filter(Boolean).map((l) => [l.slice(0, l.indexOf('=')), l.slice(l.indexOf('=') + 1)]));
  return {
    code: r.status,
    stdout: r.stdout,
    stderr: r.stderr,
    output: kv(output),
    state: kv(stateFile),
    env: kv(envFile),
    sent: existsSync(sent) ? readFileSync(sent, 'utf8').trim().split('\n').map((l) => JSON.parse(l)) : [],
    waits: existsSync(waits) ? readFileSync(waits, 'utf8').trim().split('\n').map(Number) : [],
  };
}
const main = (answers, env) => step(MAIN, { answers, env });
const to = (r, path) => r.sent.filter((s) => new URL(s.url).pathname === path);

const granted = { status: 200, body: { mode: 'live', decision: 'granted', run: '9.1', grant: { repositories: ['webapp'], permissions: { contents: 'write', pull_requests: 'write' } }, token: TOKEN, expiresAt: '2026-10-30T04:00:00.000Z', sessions: {} } };
const live = (extra) => ({ mint: { ...granted, body: { ...granted.body, ...extra } } });

// ---------- The main step ----------

test('a grant: the token is masked before anything else, then goes to the post step and to later steps', () => {
  const r = main(live());
  assert.equal(r.code, 0, r.stdout);
  const lines = r.stdout.split('\n');
  assert.deepEqual(lines.filter((l) => l.includes(TOKEN)), [`::add-mask::${TOKEN}`]);
  assert.equal(lines[lines.findIndex((l) => l.includes(TOKEN))], `::add-mask::${TOKEN}`);
  assert.deepEqual(r.output, { token: TOKEN, 'expires-at': '2026-10-30T04:00:00.000Z', sessions: '{}', 'read-token': '', telemetry: 'false' });
  assert.deepEqual(r.state, { token: TOKEN, origin: AIRLOCK, role: 'triage' });
  assert.match(r.stdout, /Airlock granted: 1 repositories \(webapp\), contents:write pull_requests:write, until 2026-10-30T04:00:00\.000Z/);
});

test('it asks Mint for the role, proven by an OIDC token for Airlock\'s origin, and sends its version', () => {
  const r = main(live());
  const [mint] = to(r, '/v1/mint');
  assert.equal(mint.url, `${AIRLOCK}/v1/mint`);
  assert.equal(mint.method, 'POST');
  assert.deepEqual(mint.body, { role: 'triage' });
  assert.match(mint.headers.authorization, /^Bearer eyJoidc-/);
  assert.equal(mint.headers['airlock-action'], VERSION);
  const [id] = r.sent.filter((s) => s.url.startsWith('https://oidc.example/'));
  assert.equal(new URL(id.url).searchParams.get('audience'), AIRLOCK);
  assert.equal(id.headers.authorization, 'bearer req');
});

test('airlock-url is an origin: a trailing slash or a path is dropped, so the audience is exactly Airlock\'s', () => {
  for (const url of [`${AIRLOCK}/`, `${AIRLOCK}/v1`, AIRLOCK]) {
    const r = main(live(), { 'INPUT_AIRLOCK-URL': url });
    assert.equal(r.code, 0, url);
    assert.equal(to(r, '/v1/mint')[0].url, `${AIRLOCK}/v1/mint`);
    assert.equal(new URL(r.sent[0].url).searchParams.get('audience'), AIRLOCK);
  }
  const loop = main(live(), { 'INPUT_AIRLOCK-URL': 'http://127.0.0.1:8787' });
  assert.equal(to(loop, '/v1/mint')[0].url, 'http://127.0.0.1:8787/v1/mint');
});

test('an airlock-url that isn\'t https (or loopback) is refused before any token is asked for', () => {
  for (const url of ['http://airlock.example', 'ftp://airlock.example', 'airlock.example', 'not a url']) {
    const r = main(live(), { 'INPUT_AIRLOCK-URL': url });
    assert.equal(r.code, 1, url);
    assert.deepEqual(r.sent, []);
    assert.match(r.stdout, /::error::Airlock: airlock-url must be an https origin/);
  }
});

test('with no airlock-url it is Airlock\'s own', () => {
  const r = main(live(), { 'INPUT_AIRLOCK-URL': '' });
  assert.equal(to(r, '/v1/mint')[0].url, 'https://airlock.lunareng.com/v1/mint');
});

test('a refusal stops the run with Airlock\'s reason and message, and nothing is written', () => {
  const r = main({ mint: { status: 403, body: { decision: 'refused', reason: 'not_on_main', message: 'Airlock: not on main', run: '9.1' } } });
  assert.equal(r.code, 1);
  assert.deepEqual(r.output, {});
  assert.deepEqual(r.state, {});
  assert.match(r.stdout, /::error::Airlock refused: not_on_main: Airlock: not on main/);
});

test('shadow is green: what Airlock would hand out is printed, nothing is handed on, the step passes', () => {
  const r = main({ mint: { status: 200, body: { mode: 'shadow', decision: 'would_grant', run: '9.1', grant: granted.body.grant, sessions: { tracker: { audience: 'https://mcp.acme.example', scopes: ['api'], tools: [] } } } } });
  assert.equal(r.code, 0);
  assert.deepEqual(r.output, { telemetry: 'false' });
  assert.deepEqual(r.state, {});
  assert.deepEqual(r.env, {});
  assert.match(r.stdout, /Airlock would grant: 1 repositories \(webapp\), contents:write pull_requests:write, and sessions for tracker\. Shadow: nothing was handed out\./);
});

test('the grant line names the repositories after the count; a name not shaped like one is `?`; none prints as before', () => {
  const shadow = (repositories) => main({ mint: { status: 200, body: { mode: 'shadow', decision: 'would_grant', run: '9.1', grant: { repositories, permissions: { contents: 'read' } } } } });
  const line = (r) => r.stdout.split('\n').find((l) => l.startsWith('Airlock would grant:'));
  assert.equal(line(shadow(['webapp', 'api'])), 'Airlock would grant: 2 repositories (webapp, api), contents:read. Shadow: nothing was handed out.');
  assert.equal(line(shadow(['bad name!'])), 'Airlock would grant: 1 repositories (?), contents:read. Shadow: nothing was handed out.');
  assert.equal(line(shadow(['ok.repo_1-x', 'x'.repeat(101), '', 7, 'a\nb', null])), 'Airlock would grant: 6 repositories (ok.repo_1-x, ?, ?, ?, ?, ?), contents:read. Shadow: nothing was handed out.');
  assert.equal(line(shadow([])), 'Airlock would grant: 0 repositories, contents:read. Shadow: nothing was handed out.');
  assert.equal(line(main({ mint: { status: 200, body: { mode: 'shadow', decision: 'would_grant', run: '9.1' } } })), 'Airlock would grant: nothing. Shadow: nothing was handed out.');
  const live2 = main(live({ grant: { repositories: ['webapp', 'api'], permissions: { contents: 'write' } } }));
  assert.match(live2.stdout, /^Airlock granted: 2 repositories \(webapp, api\), contents:write, until 2026-10-30T04:00:00\.000Z$/m);
});

test('Airlock unreachable, an answer that isn\'t JSON, or no OIDC for the job: no token, the run stops', () => {
  for (const answers of [{ mint: 'down' }, { mint: { status: 502, body: 'Bad Gateway' } }, { mint: { status: 200, body: '[]' } }]) {
    const r = main(answers);
    assert.equal(r.code, 1, JSON.stringify(answers));
    assert.deepEqual(r.output, {});
    assert.deepEqual(r.state, {});
  }
  const noOidc = main(live(), { ACTIONS_ID_TOKEN_REQUEST_URL: '' });
  assert.equal(noOidc.code, 1);
  assert.deepEqual(noOidc.sent, []);
  assert.match(noOidc.stdout, /add permissions: id-token: write/);
});

// ---------- Trying again when API Gateway, not Airlock, answered (lunareng/airlock-actions#52) ----------

const GATEWAY_500 = { status: 500, body: { message: 'Internal Server Error' } };
const retries = (r, path = '/v1/mint') => r.stdout.split('\n').filter((l) => l.startsWith(`::warning::Airlock: ${path} attempt `));

test('retry: two API Gateway 500s, then a grant: the run gets its token, and the log has two retry lines', () => {
  const r = main({ mint: [GATEWAY_500, GATEWAY_500, granted] });
  assert.equal(r.code, 0, r.stdout);
  assert.equal(r.output.token, TOKEN);
  assert.equal(r.state.token, TOKEN);
  assert.deepEqual(retries(r).map((l) => l.replace(/in \d+ s$/, 'in N s')), [
    '::warning::Airlock: /v1/mint attempt 1 of 6 got HTTP 500 from in front of Airlock; trying again in N s',
    '::warning::Airlock: /v1/mint attempt 2 of 6 got HTTP 500 from in front of Airlock; trying again in N s',
  ]);
  // Three calls, each proven by its own OIDC token: Airlock takes each one once.
  const mints = to(r, '/v1/mint');
  assert.equal(mints.length, 3);
  assert.equal(new Set(mints.map((m) => m.headers.authorization)).size, 3);
  assert.deepEqual(mints.map((m) => m.body), [{ role: 'triage' }, { role: 'triage' }, { role: 'triage' }]);
  // 2 s, then 5 s, each with up to a fifth more.
  assert.equal(r.waits.length, 2);
  assert.ok(r.waits[0] >= 2000 && r.waits[0] <= 2400, String(r.waits));
  assert.ok(r.waits[1] >= 5000 && r.waits[1] <= 6000, String(r.waits));
});

test('retry: a 403 refusal is one call, no retry', () => {
  const r = main({ mint: [{ status: 403, body: { decision: 'refused', reason: 'not_on_main', message: 'Airlock: not on main', run: '9.1' } }, granted] });
  assert.equal(r.code, 1);
  assert.equal(to(r, '/v1/mint').length, 1);
  assert.deepEqual(retries(r), []);
  assert.deepEqual(r.waits, []);
  assert.match(r.stdout, /::error::Airlock refused: not_on_main: Airlock: not on main/);
});

test('retry: six API Gateway 500s: six calls, five retry lines, and the run stops with today\'s message', () => {
  const r = main({ mint: GATEWAY_500 });
  assert.equal(r.code, 1);
  assert.equal(to(r, '/v1/mint').length, 6);
  assert.equal(retries(r).length, 5);
  assert.equal(r.waits.length, 5);
  assert.deepEqual(r.output, {});
  assert.deepEqual(r.state, {});
  assert.match(r.stdout, /^::error::Airlock: no token \(HTTP 500\); the run stops here$/m);
});

test('retry: anything Airlock said is final, whatever its status: its refusals and its own timeout are one call', () => {
  for (const mint of [
    { status: 503, body: { error: 'timeout', message: 'The request took too long. Nothing was changed.' } },
    { status: 503, body: { decision: 'refused', reason: 'github_unreachable', message: 'Airlock: Airlock could not reach GitHub' } },
    { status: 500, body: { decision: 'refused', reason: 'internal', message: 'm' } },
    { status: 502, body: { decision: 'refused', reason: 'github_error', message: 'm' } },
    { status: 504, body: { error: 'timeout', message: 'm' } },
    { status: 501, body: { message: 'Not Implemented' } },
    { status: 429, body: { message: 'Too Many Requests' } },
    { status: 404, body: { message: 'Not Found' } },
    { status: 401, body: { message: 'Unauthorized' } },
    { status: 200, body: { mode: 'live', decision: 'granted' } },
  ]) {
    const r = main({ mint: [mint, granted] });
    assert.equal(r.code, 1, JSON.stringify(mint));
    assert.equal(to(r, '/v1/mint').length, 1, JSON.stringify(mint));
    assert.deepEqual(retries(r), [], JSON.stringify(mint));
    assert.deepEqual(r.state, {}, JSON.stringify(mint));
  }
});

test('retry: no answer, or a 502, 503 or 504 that isn\'t Airlock\'s, is tried again', () => {
  for (const [first, got] of [
    ['down', 'no answer (TypeError)'],
    [{ status: 502, body: 'Bad Gateway' }, 'HTTP 502 from in front of Airlock'],
    [{ status: 503, body: { message: 'Service Unavailable' } }, 'HTTP 503 from in front of Airlock'],
    [{ status: 504, body: { message: 'Endpoint request timed out' } }, 'HTTP 504 from in front of Airlock'],
  ]) {
    const r = main({ mint: [first, granted] });
    assert.equal(r.code, 0, r.stdout);
    assert.equal(r.output.token, TOKEN);
    assert.equal(to(r, '/v1/mint').length, 2);
    assert.deepEqual(retries(r).map((l) => l.replace(/in \d+ s$/, '')), [`::warning::Airlock: /v1/mint attempt 1 of 6 got ${got}; trying again `]);
  }
  const down = main({ mint: 'down' });
  assert.equal(down.code, 1);
  assert.equal(to(down, '/v1/mint').length, 6);
  assert.match(down.stdout, /^::error::Airlock: unreachable \(TypeError\); no token$/m);
});

test('retry: the schedule is 2, 5, 10, 20 and 40 s with up to a fifth more, and stops where the 110 s would run out', async () => {
  const { WAITS, BUDGET_MS, retryWait, notAirlocks } = await import('../src/airlock.mjs');
  assert.deepEqual(WAITS, [2000, 5000, 10000, 20000, 40000]);
  // Inside the role workflow's 2-minute step limit, with room to report.
  assert.ok(BUDGET_MS <= 110000);
  for (const random of [() => 0, () => 0.5, () => 0.999999]) {
    let elapsed = 0;
    const waits = [];
    for (let n = 0; n < 5; n++) {
      const w = retryWait(n, elapsed, random);
      assert.ok(w >= WAITS[n] && w <= WAITS[n] * 1.2, `${n}: ${w}`);
      waits.push(w);
      elapsed += w + 500;
    }
    assert.equal(retryWait(5, elapsed, random), null);
  }
  // Slow attempts use the time up: no wait starts an attempt with under 5 s left.
  assert.equal(retryWait(4, 65001, () => 0), null);
  assert.equal(retryWait(4, 65000, () => 0), 40000);
  assert.equal(retryWait(0, 104000, () => 0), null);
  // Only an answer that can't be Airlock's.
  assert.equal(notAirlocks({ status: 500, out: { message: 'Internal Server Error' } }), true);
  assert.equal(notAirlocks({ status: 502, out: {} }), true);
  assert.equal(notAirlocks({ status: 503, out: { error: 'timeout' } }), false);
  assert.equal(notAirlocks({ status: 500, out: { decision: 'refused' } }), false);
  assert.equal(notAirlocks({ status: 400, out: {} }), false);
});

test('outside a job step (no GITHUB_OUTPUT or GITHUB_STATE), or with no role, nothing is asked for', () => {
  for (const env of [{ GITHUB_OUTPUT: '' }, { GITHUB_STATE: '' }, { INPUT_ROLE: '' }, { INPUT_ROLE: '  ' }]) {
    const r = main(live(), env);
    assert.equal(r.code, 1, JSON.stringify(env));
    assert.deepEqual(r.sent, []);
  }
});

test('a token in GitHub\'s newer, longer formats is handed on as it is', () => {
  for (const token of [`ghs_${'A1b2'.repeat(80)}`, `ghs_${'x'.repeat(36)}.${'y'.repeat(120)}-z_9`, `v1.${'a'.repeat(40)}`]) {
    const r = main(live({ token }));
    assert.equal(r.code, 0, r.stdout);
    assert.equal(r.output.token, token);
    assert.equal(r.stdout.split('\n').filter((l) => l.includes(token)).join(), `::add-mask::${token}`);
  }
});

test('a token that could break the output or the mask is never printed or written, and goes straight back', () => {
  for (const token of [`${TOKEN}\nother=1`, `${TOKEN} other`, `${TOKEN}\u0007`, 'ghs_short']) {
    const r = main(live({ token }));
    assert.equal(r.code, 1);
    assert.deepEqual(r.output, {});
    assert.deepEqual(r.state, {});
    assert.ok(!r.stdout.includes(token) && !r.stderr.includes(token));
    assert.deepEqual(to(r, '/v1/revoke').map((s) => s.body), [{ role: 'triage', token }]);
  }
});

// MCP sessions: one per run.mcp server (Mint hands a run all of them or none), handed on like the token.
const SESSION = 'eyJhbGciOiJFUzI1NiJ9.eyJyb2xlIjoidHJpYWdlIn0.TESTONLYsig_-';
const tracker = (token = SESSION) => ({ tracker: { audience: 'https://mcp.acme.example', token, expiresAt: '2026-10-30T04:00:00.000Z', scopes: ['api'], tools: ['issues_*'] } });

test('a session is masked with the token, before any output, and handed on in the sessions JSON', () => {
  const r = main(live({ sessions: tracker() }));
  assert.equal(r.code, 0, r.stdout);
  assert.deepEqual(JSON.parse(r.output.sessions), tracker());
  const lines = r.stdout.split('\n');
  assert.deepEqual(lines.filter((l) => l.includes(SESSION)), [`::add-mask::${SESSION}`]);
  assert.ok(lines.indexOf(`::add-mask::${SESSION}`) < lines.findIndex((l) => l.startsWith('Airlock granted')));
  assert.match(r.stdout, /Airlock session: tracker for https:\/\/mcp\.acme\.example, until 2026-10-30T04:00:00\.000Z/);
});

test('a session that can\'t be handed on safely stops the run; the token is already in state, so it goes back', () => {
  for (const sessions of [tracker(`${SESSION}\nother=1`), tracker('short'), { 'Bad Name': tracker().tracker }]) {
    const r = main(live({ sessions }));
    assert.equal(r.code, 1);
    assert.deepEqual(r.output, {});
    assert.equal(r.state.token, TOKEN);
    assert.ok(!r.stdout.includes(SESSION));
  }
});

// The read-only token for what a role clones but doesn't write (lunareng/airlock#45).
const READ = 'ghs_TESTONLYread0123456789abc';
const withRead = (token = READ) => live({ read: { grant: { repositories: ['design-system'], permissions: { contents: 'read' } }, token, expiresAt: '2026-10-30T04:00:00.000Z' } });

test('a read-only token is masked before any output, handed on as read-token, and left for the post step to end', () => {
  const r = main(withRead());
  assert.equal(r.code, 0);
  assert.equal(r.output['read-token'], READ);
  assert.equal(r.state.read_token, READ);
  const lines = r.stdout.split('\n');
  assert.equal(lines.filter((l) => l.includes(READ)).join(), `::add-mask::${READ}`);
  assert.ok(lines.indexOf(`::add-mask::${READ}`) < lines.findIndex((l) => l.startsWith('Airlock granted')));
  assert.match(r.stdout, /Airlock read-only: 1 repositories/);
});

test('a read-only token that could break the output stops the run; the token still goes back', () => {
  const r = main(withRead('ghs_bad token\nx'));
  assert.equal(r.code, 1);
  assert.deepEqual(r.output, {});
  assert.equal(r.state.token, TOKEN);
  assert.equal(r.state.read_token, undefined);
});

test('a message from Airlock that quotes the token has it cut out of the log', () => {
  const r = main(live({ message: `a token for 1 repository: ${TOKEN}`, reason: 'x' }));
  assert.equal(r.stdout.split('\n').filter((l) => l.includes(TOKEN)).join(), `::add-mask::${TOKEN}`);
});

// ---------- The run's signal and telemetry ----------

test('signal: what the run was dispatched for goes to Mint with the role; an empty one sends none', () => {
  const url = 'https://github.com/acme/webapp/issues/12';
  assert.deepEqual(to(main(live(), { INPUT_SIGNAL: url }), '/v1/mint')[0].body, { role: 'triage', signal: url });
  assert.deepEqual(to(main(live(), { INPUT_SIGNAL: '' }), '/v1/mint')[0].body, { role: 'triage' });
});

test('reason and clock: why the run was started goes to Mint with the role; empty, too long or not "true" sends none', () => {
  const body = (env) => to(main(live(), env), '/v1/mint')[0].body;
  assert.deepEqual(body({ INPUT_REASON: 'schedule 7 11 * * 1-5', INPUT_CLOCK: 'true' }), { role: 'triage', reason: 'schedule 7 11 * * 1-5', clock: true });
  assert.deepEqual(body({ INPUT_REASON: '  review on acme/api#12 ', INPUT_CLOCK: 'false' }), { role: 'triage', reason: 'review on acme/api#12' });
  assert.deepEqual(body({ INPUT_REASON: 'x'.repeat(120) }), { role: 'triage', reason: 'x'.repeat(120) });
  assert.deepEqual(body({ INPUT_REASON: 'x'.repeat(121), INPUT_CLOCK: 'yes' }), { role: 'triage' });
  assert.deepEqual(body({ INPUT_REASON: '', INPUT_CLOCK: '' }), { role: 'triage' });
});

// Mint's answer when the tenant sends role runs' telemetry to Airlock (lunareng/airlock functions/mint, telemetryEnv).
const TELEMETRY_TOKEN = 'eyJtelemetry.TESTONLY.session0123456789';
const TELEMETRY_ENV = {
  OTEL_LOG_TOOL_DETAILS: '1',
  CLAUDE_CODE_ENABLE_TELEMETRY: '1',
  OTEL_LOGS_EXPORTER: 'otlp',
  OTEL_METRICS_EXPORTER: 'otlp',
  OTEL_EXPORTER_OTLP_PROTOCOL: 'http/json',
  OTEL_EXPORTER_OTLP_ENDPOINT: `${AIRLOCK}/v1/otlp`,
  OTEL_EXPORTER_OTLP_HEADERS: `Authorization=Bearer ${TELEMETRY_TOKEN}`,
  OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE: 'cumulative',
};
const telemetry = (env = TELEMETRY_ENV, token = TELEMETRY_TOKEN) => ({ telemetry: { token, expiresAt: '2026-10-30T04:00:00.000Z', env } });

test('telemetry by default: Mint\'s telemetry env goes to every later step, its session masked before anything else', () => {
  const r = main(live(telemetry()));
  assert.equal(r.code, 0, r.stdout);
  assert.deepEqual(r.env, TELEMETRY_ENV);
  assert.equal(r.output.telemetry, 'true');
  assert.equal(r.output.token, TOKEN);
  const lines = r.stdout.split('\n');
  assert.deepEqual(lines.filter((l) => l.includes(TELEMETRY_TOKEN)), [`::add-mask::${TELEMETRY_TOKEN}`]);
  assert.match(r.stdout, /Airlock telemetry: exported to the later steps, until 2026-10-30T04:00:00\.000Z/);
});

test('telemetry in shadow: the run gets nothing but its telemetry, and the step passes', () => {
  const r = main({ mint: { status: 200, body: { mode: 'shadow', decision: 'would_grant', run: '9.1', grant: granted.body.grant, sessions: {}, ...telemetry() } } });
  assert.equal(r.code, 0, r.stdout);
  assert.deepEqual(r.env, TELEMETRY_ENV);
  assert.deepEqual(r.output, { telemetry: 'true' });
  assert.deepEqual(r.state, {});
  assert.deepEqual(r.stdout.split('\n').filter((l) => l.includes(TELEMETRY_TOKEN)), [`::add-mask::${TELEMETRY_TOKEN}`]);
  assert.match(r.stdout, /Shadow: nothing was handed out but its telemetry\./);
});

test('telemetry is only Claude Code\'s own variables, sent only to Airlock: anything else exports nothing, and the run goes on', () => {
  const unsafe = {
    'another variable': { ...TELEMETRY_ENV, NODE_OPTIONS: '--require /tmp/x.js' },
    'a path': { ...TELEMETRY_ENV, PATH: '/tmp' },
    'an endpoint elsewhere': { ...TELEMETRY_ENV, OTEL_EXPORTER_OTLP_ENDPOINT: 'https://collector.example/v1/otlp' },
    'a logs endpoint elsewhere': { ...TELEMETRY_ENV, OTEL_EXPORTER_OTLP_LOGS_ENDPOINT: 'https://collector.example/v1/logs' },
    'no endpoint': Object.fromEntries(Object.entries(TELEMETRY_ENV).filter(([k]) => k !== 'OTEL_EXPORTER_OTLP_ENDPOINT')),
    'a value over two lines': { ...TELEMETRY_ENV, OTEL_LOGS_EXPORTER: 'otlp\nNODE_OPTIONS=--require /tmp/x.js' },
    'a value that isn\'t text': { ...TELEMETRY_ENV, OTEL_LOGS_EXPORTER: 1 },
    'no variables': {},
  };
  for (const [name, env] of Object.entries(unsafe)) {
    const r = main(live(telemetry(env)));
    assert.equal(r.code, 0, name);
    assert.deepEqual(r.env, {}, name);
    assert.equal(r.output.telemetry, 'false', name);
    assert.equal(r.output.token, TOKEN, name);
    assert.match(r.stdout, /::warning::Airlock: the telemetry env holds something this step won't export/, name);
    assert.ok(!r.stdout.split('\n').some((l) => l.includes(TELEMETRY_TOKEN) && !l.startsWith('::add-mask::')), name);
  }
  const bad = main(live(telemetry(TELEMETRY_ENV, 'eyJ bad\ntoken-0123456789')));
  assert.deepEqual(bad.env, {});
  assert.equal(bad.output.telemetry, 'false');
});

test('telemetry with no GITHUB_ENV to write to is not exported; the run still gets its token', () => {
  const r = main(live(telemetry()), { GITHUB_ENV: '' });
  assert.equal(r.code, 0, r.stdout);
  assert.deepEqual(r.env, {});
  assert.equal(r.output.telemetry, 'false');
  assert.equal(r.output.token, TOKEN);
});

// ---------- The post step ----------

const post = (answers = {}, state = { token: TOKEN, origin: AIRLOCK, role: 'triage' }, env = {}) => step(POST, { answers, state, env });
const clean = (r) => {
  assert.equal(r.code, 0, r.stdout);
  assert.ok(!r.stdout.includes(TOKEN) && !r.stderr.includes(TOKEN), r.stdout + r.stderr);
  assert.ok(!r.stdout.includes(READ) && !r.stderr.includes(READ), r.stdout + r.stderr);
};

test('post: the token goes back once, with its role, a fresh OIDC token and the action\'s version', () => {
  const r = post();
  clean(r);
  assert.equal(r.stdout, 'Airlock: token handed back, run 7.1\n');
  const [revoke] = to(r, '/v1/revoke');
  assert.equal(to(r, '/v1/revoke').length, 1);
  assert.equal(revoke.url, `${AIRLOCK}/v1/revoke`);
  assert.deepEqual(revoke.body, { role: 'triage', token: TOKEN });
  assert.equal(revoke.headers['airlock-action'], VERSION);
  assert.match(revoke.headers.authorization, /^Bearer eyJoidc-/);
});

test('main then post: the post step hands back exactly what the main step was granted, on a new OIDC token', () => {
  const m = main(withRead());
  const p = post({}, m.state);
  clean(p);
  assert.deepEqual(to(p, '/v1/revoke').map((s) => s.body), [{ role: 'triage', token: TOKEN }]);
  assert.notEqual(to(p, '/v1/revoke')[0].headers.authorization, to(m, '/v1/mint')[0].headers.authorization);
  assert.deepEqual(p.sent.filter((s) => s.method === 'DELETE').map((s) => [s.url, s.headers.authorization]), [['https://api.github.test/installation/token', `Bearer ${READ}`]]);
});

test('post: GitHub had already ended it (alreadyInvalid) reads as handed back', () => {
  const r = post({ revoke: { status: 200, body: { revoked: true, alreadyInvalid: true, run: '7.2' } } });
  clean(r);
  assert.equal(r.stdout, 'Airlock: token handed back, run 7.2\n');
});

test('post: a refusal is a warning with its status and reason, and the job still passes', () => {
  for (const [status, reason] of [[403, 'not_on_main'], [409, 'nothing_to_revoke'], [401, 'token_invalid']]) {
    const r = post({ revoke: { status, body: { decision: 'refused', reason, message: 'Airlock: m' } } });
    clean(r);
    assert.equal(r.stdout, `::warning::Airlock: ${status} refused (${reason}: Airlock: m); the token expires within the hour\n`);
  }
});

test('post: a refusal that quotes the token has it cut out of the log', () => {
  const r = post({ revoke: { status: 400, body: { decision: 'refused', reason: 'bad_request', message: `not this run's token: ${TOKEN}` } } });
  clean(r);
  assert.match(r.stdout, /400 refused \(bad_request: not this run's token: \*\*\*\)/);
});

test('post: a 404, a 5xx or an unreachable Airlock never fails the job; the token expires within the hour', () => {
  // Airlock's own answers: one call, one line.
  for (const revoke of [
    { status: 404, body: 'Not Found' },
    { status: 503, body: { decision: 'refused', reason: 'github_unreachable', message: 'Airlock: Airlock could not reach GitHub' } },
    { status: 500, body: { decision: 'refused', reason: 'internal', message: 'm' } },
    { status: 503, body: { error: 'timeout', message: 'm' } },
  ]) {
    const r = post({ revoke });
    clean(r);
    assert.equal(to(r, '/v1/revoke').length, 1);
    assert.match(r.stdout, /^::warning::.*the token expires within the hour\n$/);
  }
  // None that can be Airlock's (lunareng/airlock-actions#52): six calls, five retry lines, then the same warning.
  for (const revoke of [{ status: 502, body: 'Bad Gateway' }, 'down']) {
    const r = post({ revoke });
    clean(r);
    assert.equal(to(r, '/v1/revoke').length, 6);
    assert.match(r.stdout, /^(::warning::Airlock: \/v1\/revoke attempt [1-5] of 6 got .+; trying again in \d+ s\n){5}::warning::.*the token expires within the hour\n$/);
  }
});

test('post: an API Gateway 500, then Airlock: the token is handed back, after one retry line', () => {
  const r = post({ revoke: [GATEWAY_500, { status: 200, body: { revoked: true, run: '7.3' } }] });
  clean(r);
  assert.equal(to(r, '/v1/revoke').length, 2);
  assert.equal(retries(r, '/v1/revoke').length, 1);
  assert.match(r.stdout, /Airlock: token handed back, run 7\.3\n$/);
});

test('post: no OIDC for the job, or no token to hand back (shadow, a refusal), still passes and posts nothing', () => {
  const noOidc = post({}, undefined, { ACTIONS_ID_TOKEN_REQUEST_URL: '' });
  clean(noOidc);
  assert.deepEqual(noOidc.sent, []);
  assert.match(noOidc.stdout, /id-token: write missing/);
  const none = post({}, {});
  clean(none);
  assert.deepEqual(none.sent, []);
  assert.equal(none.stdout, 'Airlock: no token to hand back\n');
});

test('post: the read-only token is ended at GitHub and never printed, whatever GitHub answers', () => {
  for (const [end, said] of [[undefined, /read-only token ended/], [{ status: 401, body: '' }, /::warning::Airlock: read-only token not ended \(401\)/], ['down', /read-only token not ended \(TypeError\)/]]) {
    const r = post({ end }, { token: TOKEN, origin: AIRLOCK, role: 'triage', read_token: READ });
    clean(r);
    assert.match(r.stdout, said);
    assert.match(r.stdout, /token handed back/);
  }
});

// ---------- action.yml ----------

const yml = readFileSync(new URL('../action.yml', import.meta.url), 'utf8');
const section = (name) => {
  const body = yml.split(new RegExp(`^${name}:\\n`, 'm'))[1] ?? '';
  return [...body.split(/^\S/m)[0].matchAll(/^ {2}([a-z][a-z-]*):/gm)].map((m) => m[1]);
};

test('action.yml: the post step runs on success, failure and cancel, on the Node the runner ships', () => {
  assert.match(yml, /^ {2}using: node24$/m);
  assert.match(yml, /^ {2}main: src\/main\.mjs$/m);
  assert.match(yml, /^ {2}post: src\/post\.mjs$/m);
  assert.match(yml, /^ {2}post-if: always\(\)$/m);
});

test('action.yml declares exactly the inputs the steps read and the outputs the main step writes', () => {
  assert.deepEqual(section('inputs'), ['role', 'signal', 'reason', 'clock', 'airlock-url']);
  assert.deepEqual(section('outputs'), Object.keys(main(withRead()).output));
  assert.match(yml, /^ {4}default: https:\/\/airlock\.lunareng\.com$/m);
});

test('the version sent is the release\'s: package.json holds an exact semver', () => {
  assert.match(VERSION, /^\d+\.\d+\.\d+$/);
});
