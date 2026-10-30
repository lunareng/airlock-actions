// The action against a stand-in Airlock and a stand-in GitHub OIDC endpoint: the main and post steps run as the runner
// runs them (node, INPUT_* env, GITHUB_OUTPUT, GITHUB_ENV and GITHUB_STATE files), and the tests read what they wrote
// and printed. No network, no GitHub.
import { after, before, beforeEach, describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { createServer } from 'node:http';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const TOKEN = 'ghs_TESTtoken0000000000000000000000000000';
const READ = 'ghs_READtoken000000000000000000000000000';
const SESSION = 'eyJhbGciOiJFUzI1NiJ9.session.signature0';
const TELEMETRY = 'eyJhbGciOiJFUzI1NiJ9.telemetry.signature0';
const grant = { repositories: ['api', 'autonomy'], permissions: { metadata: 'read', contents: 'write' } };

let server;
let base;
let seen;
let answer;
before(async () => {
  server = createServer((req, res) => {
    let body = '';
    req.on('data', (c) => (body += c));
    req.on('end', () => {
      seen.push({ method: req.method, url: req.url, auth: req.headers.authorization, agent: req.headers['user-agent'], body });
      if (req.url.startsWith('/oidc?')) return res.end(JSON.stringify({ value: `oidc-${seen.length}` }));
      if (req.url === '/installation/token') { res.statusCode = 204; return res.end(); }
      const [status, out] = answer(req.url, body);
      res.statusCode = status;
      res.end(JSON.stringify(out));
    });
  });
  await new Promise((ok) => server.listen(0, '127.0.0.1', ok));
  base = `http://127.0.0.1:${server.address().port}`;
});
after(() => server.close());
beforeEach(() => {
  seen = [];
  answer = () => [500, {}];
});

// Runs main.mjs or post.mjs as the runner would, and returns its exit code, its log and the files it wrote.
function run(script, inputs = {}, env = {}) {
  const dir = mkdtempSync(join(tmpdir(), 'airlock-action-'));
  const files = { GITHUB_OUTPUT: join(dir, 'output'), GITHUB_ENV: join(dir, 'env'), GITHUB_STATE: join(dir, 'state') };
  for (const f of Object.values(files)) writeFileSync(f, '');
  const vars = {
    PATH: process.env.PATH, ...files,
    ACTIONS_ID_TOKEN_REQUEST_URL: `${base}/oidc?api-version=2.0`, ACTIONS_ID_TOKEN_REQUEST_TOKEN: 'request-bearer',
    GITHUB_API_URL: base, 'INPUT_AIRLOCK-URL': base,
    ...Object.fromEntries(Object.entries(inputs).map(([k, v]) => [`INPUT_${k.toUpperCase()}`, v])),
    ...env,
  };
  return new Promise((ok) => execFile(process.execPath, [join(root, script)], { env: vars }, (err, stdout, stderr) => {
    const read = (f) => readFileSync(files[f], 'utf8');
    const kv = (text) => Object.fromEntries(text.split('\n').filter(Boolean).map((l) => [l.slice(0, l.indexOf('=')), l.slice(l.indexOf('=') + 1)]));
    ok({ code: err ? err.code ?? 1 : 0, log: stdout + stderr, outputs: kv(read('GITHUB_OUTPUT')), env: kv(read('GITHUB_ENV')), state: kv(read('GITHUB_STATE')) });
  }));
}
const mintBody = () => JSON.parse(seen.find((s) => s.url === '/v1/mint').body);
// A secret may appear in the log only on its own ::add-mask:: line.
const onlyMasked = (log, secret) => assert.deepEqual(log.split('\n').filter((l) => l.includes(secret)), [`::add-mask::${secret}`]);

const granted = (over = {}) => () => [200, { mode: 'live', decision: 'granted', run: '7001.1', grant, token: TOKEN, expiresAt: '2026-10-30T16:00:00.000Z', sessions: {}, ...over }];
const telemetry = { token: TELEMETRY, expiresAt: '2026-10-30T16:00:00.000Z', env: { CLAUDE_CODE_ENABLE_TELEMETRY: '1', OTEL_LOG_TOOL_DETAILS: '1', OTEL_EXPORTER_OTLP_HEADERS: `Authorization=Bearer ${TELEMETRY}` } };

describe('main: a live role', () => {
  it('hands the token to the step\'s outputs and the post step, masked first and never printed', async () => {
    answer = granted();
    const r = await run('main.mjs', { role: 'clerk' });
    assert.equal(r.code, 0);
    assert.equal(r.outputs.token, TOKEN);
    assert.equal(r.outputs['expires-at'], '2026-10-30T16:00:00.000Z');
    assert.equal(r.outputs.decision, 'granted');
    assert.equal(r.outputs.run, '7001.1');
    assert.equal(r.state.token, TOKEN);
    onlyMasked(r.log, TOKEN);
    assert.match(r.log, /Airlock granted: 2 repositories, metadata:read contents:write/);
    assert.equal(seen[0].url, `/oidc?api-version=2.0&audience=${encodeURIComponent(base)}`);
    assert.equal(seen[1].auth, 'Bearer oidc-1');
    assert.equal(seen[1].agent, 'airlock-actions/1.0.0');
    assert.deepEqual(mintBody(), { role: 'clerk' });
  });

  it('sends the signal it was dispatched for, and none when there is none', async () => {
    answer = granted();
    await run('main.mjs', { role: 'clerk', signal: 'https://github.com/acme/api/issues/12' });
    assert.deepEqual(mintBody(), { role: 'clerk', signal: 'https://github.com/acme/api/issues/12' });
    seen = [];
    await run('main.mjs', { role: 'clerk', signal: '' });
    assert.deepEqual(mintBody(), { role: 'clerk' });
  });

  it('exports the telemetry env to the later steps by default, the session masked first', async () => {
    answer = granted({ telemetry });
    const r = await run('main.mjs', { role: 'clerk' });
    assert.equal(r.code, 0);
    assert.deepEqual(r.env, telemetry.env);
    assert.equal(r.outputs.telemetry, 'true');
    onlyMasked(r.log, TELEMETRY);
  });

  it('exports nothing when the tenant has telemetry off, and says so', async () => {
    answer = granted();
    const r = await run('main.mjs', { role: 'clerk' });
    assert.deepEqual(r.env, {});
    assert.equal(r.outputs.telemetry, 'false');
  });

  it('never exports a telemetry env that could break the env file, and still runs', async () => {
    for (const bad of [{ ...telemetry, env: { 'BAD NAME': '1' } }, { ...telemetry, env: { A: 'x\nB=y' } }, { ...telemetry, token: 'short' }, { ...telemetry, env: {} }]) {
      answer = granted({ telemetry: bad });
      const r = await run('main.mjs', { role: 'clerk' });
      assert.equal(r.code, 0);
      assert.deepEqual(r.env, {}, JSON.stringify(bad));
      assert.equal(r.outputs.telemetry, 'false');
    }
  });

  it('hands each MCP session and the read-only token out, masked; the sessions as JSON too', async () => {
    const read = { grant: { repositories: ['docs'], permissions: { contents: 'read' } }, token: READ, expiresAt: '2026-10-30T16:00:00.000Z' };
    answer = granted({ read, sessions: { clerk: { audience: 'https://mcp.example.com', token: SESSION, expiresAt: '2026-10-30T16:00:00.000Z', scopes: ['api'], tools: [] } } });
    const r = await run('main.mjs', { role: 'clerk', sessions: 'clerk' });
    assert.equal(r.code, 0);
    assert.equal(r.outputs.session_clerk, SESSION);
    assert.equal(JSON.parse(r.outputs.sessions).clerk.token, SESSION);
    assert.equal(r.outputs['read-token'], READ);
    assert.equal(r.state.read_token, READ);
    for (const s of [TOKEN, SESSION, READ]) onlyMasked(r.log, s);
  });

  it('stops when a session it needs is missing, but still hands the token to the post step', async () => {
    answer = granted();
    const r = await run('main.mjs', { role: 'clerk', sessions: 'clerk' });
    assert.equal(r.code, 1);
    assert.match(r.log, /no usable session for clerk/);
    assert.equal(r.state.token, TOKEN);
    assert.equal(r.outputs.session_clerk, undefined);
  });

  for (const [what, a] of [
    ['refused', () => [403, { decision: 'refused', reason: 'role_file_changed', message: 'Airlock: the role\'s file changed since the run started', run: '7001.1' }]],
    ['in shadow though the workflow expects a token', () => [200, { mode: 'shadow', decision: 'would_grant', run: '7001.1', grant, sessions: {} }]],
    ['a token that can\'t be carried safely', granted({ token: 'ghs_bad token\nINJECT=1' })],
    ['an answer that isn\'t JSON', () => [502, 'gateway']],
  ]) {
    it(`stops the run with nothing handed out: ${what}`, async () => {
      answer = a;
      const r = await run('main.mjs', { role: 'clerk' });
      assert.equal(r.code, 1);
      assert.equal(r.outputs.token, undefined);
      assert.equal(r.state.token, undefined);
    });
  }

  it('stops when the job can\'t ask GitHub for an OIDC token', async () => {
    const r = await run('main.mjs', { role: 'clerk' }, { ACTIONS_ID_TOKEN_REQUEST_URL: '' });
    assert.equal(r.code, 1);
    assert.match(r.log, /id-token: write/);
    assert.equal(seen.length, 0);
  });

  it('stops when Airlock is unreachable', async () => {
    const r = await run('main.mjs', { role: 'clerk' }, { 'INPUT_AIRLOCK-URL': 'http://127.0.0.1:9' });
    assert.equal(r.code, 1);
    assert.match(r.log, /unreachable/);
  });

  it('refuses a role that isn\'t a role name before asking anything', async () => {
    const r = await run('main.mjs', { role: 'clerk; rm -rf /' });
    assert.equal(r.code, 1);
    assert.equal(seen.length, 0);
  });
});

describe('main: a role in shadow', () => {
  it('reports what Airlock would hand out, exports its telemetry, and never fails', async () => {
    answer = () => [200, { mode: 'shadow', decision: 'would_grant', run: '7001.1', grant, sessions: {}, telemetry }];
    const r = await run('main.mjs', { role: 'clerk', shadow: 'true' });
    assert.equal(r.code, 0);
    assert.match(r.log, /Airlock would grant: 2 repositories/);
    assert.equal(r.outputs.token, undefined);
    assert.deepEqual(r.env, telemetry.env);
  });

  it('never fails, even refused or unreachable', async () => {
    answer = () => [403, { decision: 'refused', reason: 'rate_limited', message: 'Airlock: too many mints' }];
    assert.equal((await run('main.mjs', { role: 'clerk', shadow: 'true' })).code, 0);
    assert.equal((await run('main.mjs', { role: 'clerk', shadow: 'true' }, { 'INPUT_AIRLOCK-URL': 'http://127.0.0.1:9' })).code, 0);
  });

  it('a token Airlock does grant is still recorded for the post step', async () => {
    answer = granted();
    const r = await run('main.mjs', { role: 'clerk', shadow: 'true' });
    assert.equal(r.state.token, TOKEN);
  });
});

describe('post: hands the token back when the job ends', () => {
  it('with a fresh OIDC token, the token in the body, and never in the log', async () => {
    answer = (url) => (url === '/v1/revoke' ? [200, { revoked: true, run: '7001.1', at: '2026-10-30T15:00:00.000Z' }] : [500, {}]);
    const r = await run('post.mjs', { role: 'clerk' }, { STATE_token: TOKEN });
    assert.equal(r.code, 0);
    const revoke = seen.find((s) => s.url === '/v1/revoke');
    assert.deepEqual(JSON.parse(revoke.body), { role: 'clerk', token: TOKEN });
    assert.equal(revoke.auth, 'Bearer oidc-1');
    assert.match(r.log, /token handed back, run 7001.1/);
    assert.doesNotMatch(r.log, new RegExp(TOKEN));
  });

  it('ends the read-only token too', async () => {
    answer = () => [200, { revoked: true, run: '7001.1' }];
    const r = await run('post.mjs', { role: 'clerk' }, { STATE_token: TOKEN, STATE_read_token: READ });
    const del = seen.find((s) => s.url === '/installation/token');
    assert.equal(del.method, 'DELETE');
    assert.equal(del.auth, `Bearer ${READ}`);
    assert.match(r.log, /read-only token ended/);
    assert.doesNotMatch(r.log, new RegExp(READ));
  });

  it('asks nothing when there is no token (a shadow run, or a run refused at the start)', async () => {
    const r = await run('post.mjs', { role: 'clerk' });
    assert.equal(r.code, 0);
    assert.equal(seen.length, 0);
  });

  it('never fails the job: refused, unreachable or no OIDC token, it says the token expires within the hour', async () => {
    answer = () => [403, { decision: 'refused', reason: 'token_mismatch', message: `Airlock: that is not the token ${TOKEN}` }];
    let r = await run('post.mjs', { role: 'clerk' }, { STATE_token: TOKEN });
    assert.equal(r.code, 0);
    assert.match(r.log, /403 refused \(token_mismatch/);
    assert.doesNotMatch(r.log, new RegExp(TOKEN));
    r = await run('post.mjs', { role: 'clerk' }, { STATE_token: TOKEN, 'INPUT_AIRLOCK-URL': 'http://127.0.0.1:9' });
    assert.equal(r.code, 0);
    assert.match(r.log, /unreachable.*expires within the hour/);
    r = await run('post.mjs', { role: 'clerk' }, { STATE_token: TOKEN, ACTIONS_ID_TOKEN_REQUEST_TOKEN: '' });
    assert.equal(r.code, 0);
    assert.match(r.log, /id-token: write missing/);
  });
});

describe('action.yml', () => {
  it('runs main, and post always, on the runner\'s Node with nothing to build', () => {
    const yml = readFileSync(join(root, 'action.yml'), 'utf8');
    assert.match(yml, /using: node24/);
    assert.match(yml, /main: main\.mjs/);
    assert.match(yml, /post: post\.mjs/);
    assert.match(yml, /post-if: always\(\)/);
    const pkg = JSON.parse(readFileSync(join(root, 'package.json'), 'utf8'));
    assert.deepEqual(pkg.dependencies ?? {}, {});
  });
});
