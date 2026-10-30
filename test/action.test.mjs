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

// One step. `answers` says what each route answers: { mint, revoke, end } as { status, body }, or 'down'.
function step(script, { answers = {}, env = {}, state = {} } = {}) {
  const dir = mkdtempSync(join(tmpdir(), 'airlock-action-'));
  const sent = join(dir, 'sent');
  const stub = join(dir, 'stub.mjs');
  writeFileSync(stub, `
    import { appendFileSync } from 'node:fs';
    const answers = ${JSON.stringify(answers)};
    let n = 0;
    const answer = (a, fallback) => {
      if (a === 'down') throw new TypeError('fetch failed');
      const { status, body } = a ?? fallback;
      return new Response(status === 204 ? null : typeof body === 'string' ? body : JSON.stringify(body), { status });
    };
    globalThis.fetch = async (url, init = {}) => {
      const u = new URL(String(url));
      const h = Object.fromEntries(Object.entries(init.headers ?? {}).map(([k, v]) => [k.toLowerCase(), v]));
      appendFileSync(${JSON.stringify(sent)}, JSON.stringify({ url: u.href, method: init.method ?? 'GET', headers: h, body: init.body ? JSON.parse(init.body) : null }) + '\\n');
      if (u.origin === 'https://oidc.example') return new Response(JSON.stringify({ value: 'eyJoidc-' + process.pid + '-' + ++n }));
      if (u.pathname === '/v1/mint') return answer(answers.mint, { status: 418, body: {} });
      if (u.pathname === '/v1/revoke') return answer(answers.revoke, { status: 200, body: { revoked: true, run: '7.1' } });
      if (u.href === 'https://api.github.test/installation/token') return answer(answers.end, { status: 204, body: '' });
      return new Response('{}', { status: 418 });
    };`);
  const output = join(dir, 'output');
  const stateFile = join(dir, 'state');
  writeFileSync(output, '');
  writeFileSync(stateFile, '');
  const r = spawnSync(process.execPath, ['--import', stub, script], {
    env: {
      PATH: process.env.PATH,
      ACTIONS_ID_TOKEN_REQUEST_URL: 'https://oidc.example/token?api-version=2.0',
      ACTIONS_ID_TOKEN_REQUEST_TOKEN: 'req',
      GITHUB_OUTPUT: output,
      GITHUB_STATE: stateFile,
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
    sent: existsSync(sent) ? readFileSync(sent, 'utf8').trim().split('\n').map((l) => JSON.parse(l)) : [],
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
  assert.deepEqual(r.output, { token: TOKEN, 'expires-at': '2026-10-30T04:00:00.000Z', sessions: '{}', 'read-token': '' });
  assert.deepEqual(r.state, { token: TOKEN, origin: AIRLOCK, role: 'triage' });
  assert.match(r.stdout, /Airlock granted: 1 repositories, contents:write pull_requests:write, until 2026-10-30T04:00:00\.000Z/);
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
  assert.deepEqual(r.output, {});
  assert.deepEqual(r.state, {});
  assert.match(r.stdout, /Airlock would grant: 1 repositories, contents:write pull_requests:write, and sessions for tracker\. Shadow: nothing was handed out\./);
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
  for (const revoke of [
    { status: 404, body: 'Not Found' },
    { status: 503, body: { decision: 'refused', reason: 'github_unreachable', message: 'Airlock: Airlock could not reach GitHub' } },
    { status: 500, body: { decision: 'refused', reason: 'internal', message: 'm' } },
    { status: 502, body: 'Bad Gateway' },
    'down',
  ]) {
    const r = post({ revoke });
    clean(r);
    assert.match(r.stdout, /^::warning::.*the token expires within the hour\n$/);
  }
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
  assert.deepEqual(section('inputs'), ['role', 'airlock-url']);
  assert.deepEqual(section('outputs'), Object.keys(main(withRead()).output));
  assert.match(yml, /^ {4}default: https:\/\/airlock\.lunareng\.com$/m);
});

test('the version sent is the release\'s: package.json holds an exact semver', () => {
  assert.match(VERSION, /^\d+\.\d+\.\d+$/);
});
