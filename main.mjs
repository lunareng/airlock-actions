// The main step: this run's token from Airlock. The run proves where it is with its GitHub OIDC token (the job needs
// permissions: id-token: write), names its role, and Airlock answers from the role's file at this commit. Every
// secret in the answer is masked before anything is written; the token goes to the step's outputs first, and into the
// post step's state, so a run stopped further down still hands it back. Claude Code's telemetry env, when Airlock
// sends it (the tenant turned it on), goes to every later step: telemetry by default, with nothing to configure.
// It never prints a token.
import { airlockUrl, exportEnv, headers, input, mask, oidcToken, safe, saveState, setOutput } from './lib.mjs';

const role = input('role');
const signal = input('signal');
const shadow = input('shadow') === 'true';
const wanted = input('sessions').split(',').map((c) => c.trim()).filter(Boolean);
const AIRLOCK = airlockUrl();

// Stops the run, unless the role is in shadow: there the step only reports.
function stop(line) {
  console.log(line);
  process.exit(shadow ? 0 : 1);
}

if (!/^[a-z][a-z0-9-]{0,39}$/.test(role)) stop(`Airlock: "${role}" is not a role name`);
const timeout = AbortSignal.timeout(20000);
let res;
let out = {};
try {
  const id = await oidcToken(AIRLOCK, timeout);
  if (!id) stop('Airlock: no OIDC token available to this job (add permissions: id-token: write)');
  res = await fetch(`${AIRLOCK}/v1/mint`, { method: 'POST', headers: headers(id), body: JSON.stringify({ role, ...(signal ? { signal } : {}) }), signal: timeout });
  out = await res.json().catch(() => ({}));
} catch (err) {
  stop(`Airlock: unreachable (${err?.name ?? 'Error'}); no token`);
}

console.log(`Airlock: ${res.status} ${out.decision ?? '?'}${out.reason ? ` (${out.reason}: ${out.message})` : ''}, run ${out.run ?? '-'}`);
if (typeof out.run === 'string' && /^\d{1,20}\.\d{1,5}$/.test(out.run)) setOutput('run', out.run);
if (typeof out.decision === 'string' && /^[a-z_]{1,20}$/.test(out.decision)) setOutput('decision', out.decision);
const grant = out.grant ? `${out.grant.repositories.length} repositories, ${Object.entries(out.grant.permissions).map(([k, v]) => `${k}:${v}`).join(' ')}` : '';

// Telemetry, live or shadow: the session can only add telemetry to this run, so a shadow run exports it too.
function telemetry() {
  const t = out.telemetry;
  const env = t && typeof t.env === 'object' && t.env !== null ? Object.entries(t.env) : [];
  const ok = t && safe(t.token) && env.length > 0 && env.every(([k, v]) => /^[A-Z_][A-Z0-9_]{0,63}$/.test(k) && typeof v === 'string' && !/[\r\n]/.test(v));
  if (!ok) {
    setOutput('telemetry', 'false');
    if (t) console.log('Airlock: the telemetry env holds something a step env can\'t carry safely; not exported');
    return;
  }
  mask(t.token);
  for (const [k, v] of env) exportEnv(k, v);
  setOutput('telemetry', 'true');
  console.log(`Airlock telemetry: exported to the later steps, until ${t.expiresAt}`);
}

// `shadow` only decides whether a failure stops the run: a token Airlock does grant is always recorded for the post step.
if (out.decision === 'would_grant') {
  telemetry();
  if (grant) console.log(`Airlock would grant: ${grant}`);
  if (!shadow) stop(`Airlock: ${role} is in shadow there, but this workflow expects its token (does its role file say key_store: airlock at this commit?)`);
  process.exit(0);
}
if (res.status !== 200 || out.decision !== 'granted' || !safe(out.token)) {
  stop(out.decision === 'granted' ? 'Airlock: the token holds characters a step output can\'t carry safely; the run stops here' : 'Airlock: no token; the run stops here');
}

// Each MCP session and the read-only token are masked with the token, before any output is written. One that fails
// the check, or a connector name that can't be an output name, is never printed or written.
const sessions = Object.entries(out.sessions ?? {}).filter(([c, s]) => /^[a-z][a-z0-9-]{0,40}$/.test(c) && safe(s?.token));
const read = out.read && safe(out.read.token) ? out.read : null;
mask(out.token);
for (const [, s] of sessions) mask(s.token);
if (read) mask(read.token);
// The token goes out first, so a run stopped below still hands it back in the post step.
setOutput('token', out.token);
saveState('token', out.token);
if (read) saveState('read_token', read.token);
if (out.read && !read) stop('Airlock: the read-only token holds characters a step output can\'t carry safely; the run stops here');
if (typeof out.expiresAt === 'string') setOutput('expires-at', out.expiresAt);
const missing = wanted.filter((c) => !sessions.some(([name]) => name === c));
if (missing.length) stop(`Airlock: no usable session for ${missing.join(', ')} (run.mcp); the run stops here`);
for (const [c, s] of sessions) setOutput(`session_${c}`, s.token);
setOutput('sessions', JSON.stringify(Object.fromEntries(sessions)));
if (read) setOutput('read-token', read.token);
telemetry();
console.log(`Airlock granted: ${grant}, until ${out.expiresAt}`);
if (read) console.log(`Airlock read-only: ${(read.grant?.repositories ?? []).length} repositories, until ${read.expiresAt}`);
for (const [c, s] of sessions) console.log(`Airlock session: ${c} for ${s.audience}, until ${s.expiresAt}`);
