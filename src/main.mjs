// The action's main step: the run's token from Airlock. The run proves where it is with the job's OIDC token and
// names its role; Airlock answers from the role's file at this commit. A grant's token, sessions and read-only
// token are masked before anything else and reach later steps only as outputs; the post step hands them back.
// Claude Code's telemetry env, when Airlock sends it (the tenant sends role runs' telemetry to Airlock), goes to
// every later step through GITHUB_ENV, live or shadow: telemetry by default, with nothing to configure.
import { airlockOrigin, annotate, call, DEFAULT_AIRLOCK, handBack, hide, input, mask, oidc, put, safe, say } from './airlock.mjs';

const stop = (line) => { annotate('error', line); process.exit(1); };

const role = input('role');
const dispatched = input('signal');
const origin = airlockOrigin(input('airlock-url') || DEFAULT_AIRLOCK);
if (!role) stop('Airlock: the role input is empty');
if (!origin) stop('Airlock: airlock-url must be an https origin, like https://airlock.lunareng.com');
// Nothing is minted that couldn't be handed on and handed back.
if (!process.env.GITHUB_OUTPUT || !process.env.GITHUB_STATE) stop('Airlock: no GITHUB_OUTPUT or GITHUB_STATE: this runs as a step of a GitHub Actions job');

const signal = AbortSignal.timeout(20000);
let answer;
try {
  const proof = await oidc(origin, signal);
  if (!proof) stop('Airlock: no OIDC token for this job (add permissions: id-token: write)');
  answer = await call(origin, '/v1/mint', proof, { role, ...(dispatched ? { signal: dispatched } : {}) }, signal);
} catch (err) {
  stop(`Airlock: unreachable (${err?.name ?? 'Error'}); no token`);
}
const { status, out } = answer;
const sessions = out.sessions && typeof out.sessions === 'object' ? Object.entries(out.sessions) : [];
hide(out.token, out.read?.token, out.telemetry?.token, ...sessions.map(([, s]) => s?.token));
say(`Airlock: ${status} ${out.decision ?? 'no answer'}${out.reason ? ` (${out.reason}: ${out.message})` : ''}, run ${out.run ?? '-'}`);
const grant = Array.isArray(out.grant?.repositories) ? `${out.grant.repositories.length} repositories, ${Object.entries(out.grant.permissions ?? {}).map(([k, v]) => `${k}:${v}`).join(' ')}` : '';

// The run's telemetry, exported to every later step: only Claude Code's own variables, only to Airlock's own
// receiver, and its session masked first. Anything else in it, or no GITHUB_ENV, and nothing is exported: telemetry
// is never a reason to stop a run. A session can only add telemetry to its own run, so shadow exports it too.
function telemetry() {
  const t = out.telemetry;
  const env = t && typeof t.env === 'object' && t.env !== null ? Object.entries(t.env) : [];
  const ok = t && safe(t.token) && process.env.GITHUB_ENV && env.length > 0
    && env.every(([k, v]) => /^(CLAUDE_CODE_ENABLE_TELEMETRY|OTEL_[A-Z0-9_]{1,60})$/.test(k) && typeof v === 'string' && !/[\r\n]/.test(v))
    && env.some(([k, v]) => k === 'OTEL_EXPORTER_OTLP_ENDPOINT' && v === `${origin}/v1/otlp`)
    && env.every(([k, v]) => !k.startsWith('OTEL_EXPORTER_OTLP_') || !k.endsWith('ENDPOINT') || v === `${origin}/v1/otlp`);
  if (!ok) {
    if (t) annotate('warning', 'Airlock: the telemetry env holds something this step won\'t export; no telemetry for this run');
    return 'false';
  }
  mask(t.token);
  for (const [k, v] of env) put('GITHUB_ENV', k, v);
  say(`Airlock telemetry: exported to the later steps, until ${t.expiresAt}`);
  return 'true';
}

// Shadow is green: Airlock checked the run, recorded what it would hand out, and handed out nothing, so the step
// passes with an empty token and a step that needs one checks for it. Turning the role live (identity.key_store:
// airlock) then changes nothing in its workflow. (lunareng's own script fails a would-grant instead, because its
// generator knows which roles are live; this action can't.)
if (status === 200 && out.decision === 'would_grant') {
  const sent = telemetry();
  put('GITHUB_OUTPUT', 'telemetry', sent);
  say(`Airlock would grant: ${grant || 'nothing'}${sessions.length ? `, and sessions for ${sessions.map(([c]) => c).join(', ')}` : ''}. Shadow: nothing was handed out${sent === 'true' ? ' but its telemetry' : ''}.`);
  process.exit(0);
}
if (status !== 200 || out.decision !== 'granted' || typeof out.token !== 'string' || !out.token) {
  stop(out.decision === 'refused' ? `Airlock refused: ${out.reason}: ${out.message}` : `Airlock: no token (HTTP ${status}); the run stops here`);
}
if (!safe(out.token)) {
  // It can't be masked or handed on safely: it goes straight back, unprinted, and the run stops.
  await handBack({ origin, role, token: out.token });
  stop('Airlock: the token holds characters a step output can\'t carry safely; the run stops here');
}

mask(out.token);
// The token goes to the post step first, so from here on whatever stops the run, it is handed back.
put('GITHUB_STATE', 'token', out.token);
put('GITHUB_STATE', 'origin', origin);
put('GITHUB_STATE', 'role', role);
// The read-only token for what the role clones but doesn't write (lunareng/airlock#45): to the post step as
// early, which ends it.
const read = out.read ?? null;
if (read && safe(read.token)) { mask(read.token); put('GITHUB_STATE', 'read_token', read.token); }
if (read && !safe(read.token)) stop('Airlock: the read-only token can\'t be handed on safely; the run stops here');
// Mint hands a run every session its file names or none of them, so each one must be one the step can carry.
if (sessions.some(([c, s]) => !/^[a-z][a-z0-9-]{0,40}$/.test(c) || !safe(s?.token))) stop('Airlock: a session can\'t be handed on safely; the run stops here');
for (const [, s] of sessions) mask(s.token);

put('GITHUB_OUTPUT', 'token', out.token);
put('GITHUB_OUTPUT', 'expires-at', /^[0-9TZ:.+-]{10,40}$/.test(String(out.expiresAt)) ? out.expiresAt : '');
put('GITHUB_OUTPUT', 'sessions', JSON.stringify(Object.fromEntries(sessions.map(([c, s]) => [c, { audience: s.audience, token: s.token, expiresAt: s.expiresAt, scopes: s.scopes, tools: s.tools }]))));
put('GITHUB_OUTPUT', 'read-token', read ? read.token : '');
put('GITHUB_OUTPUT', 'telemetry', telemetry());
say(`Airlock granted: ${grant}, until ${out.expiresAt}`);
if (read) say(`Airlock read-only: ${(read.grant?.repositories ?? []).length} repositories, until ${read.expiresAt}`);
for (const [c, s] of sessions) say(`Airlock session: ${c} for ${s.audience}, until ${s.expiresAt}`);
