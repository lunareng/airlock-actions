// What the action's two steps share: its inputs, the job's GitHub OIDC token, Airlock's routes, and the rules for
// anything that holds a token. Ported from lunareng/autonomy's scripts/airlock.mjs, which lunareng's own role
// runs used first; the decisions there hold here. No dependencies: Node's own fetch and fs.
import { appendFileSync, readFileSync } from 'node:fs';

// Sent to Airlock as the Airlock-Action header, so it can say which version a run used and, one day, refuse one
// too old to trust (lunareng/airlock#41).
export const VERSION = JSON.parse(readFileSync(new URL('../package.json', import.meta.url), 'utf8')).version;

export const DEFAULT_AIRLOCK = 'https://airlock.lunareng.com';

// An input as the runner hands it to a JavaScript action: INPUT_<NAME>, upper-cased, spaces as underscores.
export const input = (name) => (process.env[`INPUT_${name.replace(/ /g, '_').toUpperCase()}`] ?? '').trim();

// Airlock's origin, from airlock-url. It is also the OIDC token's audience, and Mint accepts exactly its own
// origin, so a path or a trailing slash is dropped rather than sent as a different audience. Only https, or a
// loopback address for a stand-in on the runner itself; null for anything else.
export function airlockOrigin(raw) {
  let u;
  try { u = new URL(raw); } catch { return null; }
  const loopback = u.protocol === 'http:' && ['127.0.0.1', 'localhost', '[::1]'].includes(u.hostname);
  return u.protocol === 'https:' || loopback ? u.origin : null;
}

// Only a token that can't break a step output or the mask: one line of token characters, no whitespace or control
// characters, masked first, then handed to later steps; never echoed. No prefix or length beyond that: GitHub
// changes its token format (a 'ghs_' + 36 check refused lunareng's clerk its first real token, 2026-10-26).
export const safe = (value) => typeof value === 'string' && /^[A-Za-z0-9_.~+/=-]{20,4096}$/.test(value);

// Every line the action prints has every token it knows cut out, whatever Airlock's answer says.
const secrets = [];
export const hide = (...values) => { for (const v of values) if (typeof v === 'string' && v) secrets.push(v); };
const cut = (line) => secrets.reduce((l, t) => l.split(t).join('***'), String(line));
export const say = (line) => console.log(cut(line));
// A workflow command's message, escaped the way the runner reads it (%, CR and LF), after the cut; a title, when
// given, escaped as a property (those three, and : and ,).
const escape = (s) => s.replace(/%/g, '%25').replace(/\r/g, '%0D').replace(/\n/g, '%0A');
export const annotate = (kind, line, title) => console.log(`::${kind}${title ? ` title=${escape(title).replace(/:/g, '%3A').replace(/,/g, '%2C')}` : ''}::${escape(cut(line))}`);
export const mask = (value) => process.stdout.write(`::add-mask::${value}\n`);

// A value for later steps (GITHUB_OUTPUT) or for the post step (GITHUB_STATE): one name=value line, which every
// value here is by the time it gets this far.
export function put(file, name, value) {
  if (/[\r\n]/.test(value)) throw new Error(`${name} spans lines`);
  appendFileSync(process.env[file], `${name}=${value}\n`);
}

// The job's GitHub OIDC token for Airlock: the run's proof of where it runs, with no access to anything. Airlock
// takes each one once, so every call asks GitHub for its own. Null when the job has no id-token: write.
export async function oidc(audience, signal) {
  const { ACTIONS_ID_TOKEN_REQUEST_URL: url, ACTIONS_ID_TOKEN_REQUEST_TOKEN: bearer } = process.env;
  if (!url || !bearer) return null;
  const at = new URL(url);
  at.searchParams.set('audience', audience);
  const body = await fetch(at, { headers: { authorization: `bearer ${bearer}` }, signal }).then((r) => r.json());
  if (typeof body?.value !== 'string' || !body.value) throw new Error('GitHub gave no OIDC token');
  return body.value;
}

// One of Airlock's routes, proven by an OIDC token. Its answer is always an object: one that isn't JSON is {}.
export async function call(origin, path, proof, body, signal) {
  const res = await fetch(`${origin}${path}`, {
    method: 'POST',
    headers: { authorization: `Bearer ${proof}`, 'content-type': 'application/json', 'airlock-action': VERSION },
    body: JSON.stringify(body),
    signal,
  });
  const out = await res.json().catch(() => ({}));
  return { status: res.status, out: out && typeof out === 'object' && !Array.isArray(out) ? out : {} };
}

// Trying a route again (lunareng/airlock-actions#52). While an Airlock task is being replaced, API Gateway answers
// for it with its own 500 or 503 ({"message": …}) for about 90 s, so a call is tried again only when the answer
// can't have come from Airlock: no answer at all (a network error or a timeout), or a 500, 502, 503 or 504 with
// neither Airlock's `decision` nor its `error` (its own 503 {"error":"timeout"}). Anything Airlock said is final.
// Up to 6 attempts, 2, 5, 10, 20 and 40 s apart plus up to a fifth more, each up to 20 s and with its own OIDC
// token (Airlock takes each one once, and an attempt cut off by its timeout may have spent it), all inside 110 s:
// the step's own limit in a role workflow is 2 minutes.
export const WAITS = [2000, 5000, 10000, 20000, 40000];
export const ATTEMPT_MS = 20000;
export const BUDGET_MS = 110000;
// An attempt is only started with at least this long left for it.
const LEAST_MS = 5000;

export const notAirlocks = ({ status, out }) => [500, 502, 503, 504].includes(status) && !('decision' in out) && !('error' in out);

// How long to wait after attempt n (0-based) failed, elapsed ms after the first began; null: no attempt is left.
export function retryWait(n, elapsed, random = Math.random) {
  if (n >= WAITS.length) return null;
  const wait = Math.round(WAITS[n] * (1 + 0.2 * random()));
  return elapsed + wait + LEAST_MS > BUDGET_MS ? null : wait;
}

// The route, proven by a fresh OIDC token, tried again on what notAirlocks says isn't Airlock's answer, one warning
// line per retry. { proof: null } when the job has no OIDC; { answer } when something answered; { error } when
// nothing did on the last attempt.
export async function persist(origin, path, body) {
  const start = Date.now();
  for (let n = 0; ; n++) {
    const signal = AbortSignal.timeout(Math.max(1, Math.min(ATTEMPT_MS, start + BUDGET_MS - Date.now())));
    let result;
    let got;
    try {
      const proof = await oidc(origin, signal);
      if (!proof) return { proof: null };
      result = { answer: await call(origin, path, proof, body, signal) };
      if (!notAirlocks(result.answer)) return result;
      got = `HTTP ${result.answer.status} from in front of Airlock`;
    } catch (error) {
      result = { error };
      got = `no answer (${error?.name ?? 'Error'})`;
    }
    const wait = retryWait(n, Date.now() - start);
    if (wait === null) return result;
    annotate('warning', `Airlock: ${path} attempt ${n + 1} of ${WAITS.length + 1} got ${got}; trying again in ${Math.round(wait / 1000)} s`);
    await new Promise((done) => setTimeout(done, wait));
  }
}

// The hand-back: the read-only token ended at GitHub, and the run's token handed back to Airlock (POST
// /v1/revoke, with a fresh OIDC token and the same role), so both end with the run. It never throws and never
// fails the job: a token it can't hand back still ends with its hour, and the log says so.
export async function handBack({ origin, role, token, readToken }) {
  hide(token, readToken);
  const expires = 'the token expires within the hour';
  const missed = (line) => annotate('warning', line);
  if (readToken) {
    // GitHub's DELETE /installation/token, authenticated by the token alone.
    const api = process.env.GITHUB_API_URL || 'https://api.github.com';
    try {
      const r = await fetch(`${api}/installation/token`, { method: 'DELETE', headers: { authorization: `Bearer ${readToken}`, accept: 'application/vnd.github+json' }, signal: AbortSignal.timeout(10000) });
      if (r.status === 204) say('Airlock: read-only token ended');
      else missed(`Airlock: read-only token not ended (${r.status}); it expires within the hour`);
    } catch (err) {
      missed(`Airlock: read-only token not ended (${err?.name ?? 'Error'}); it expires within the hour`);
    }
  }
  if (!token) { say('Airlock: no token to hand back'); return; }
  if (!origin) { missed(`Airlock: no Airlock to hand the token back to; ${expires}`); return; }
  // Tried again by the same rule as the mint: only what can't be Airlock's answer.
  const tried = await persist(origin, '/v1/revoke', { role, token });
  if (tried.proof === null) { missed(`Airlock: no OIDC token for this job (id-token: write missing); ${expires}`); return; }
  if (tried.error) { missed(`Airlock: unreachable (${tried.error?.name ?? 'Error'}); ${expires}`); return; }
  const { status, out } = tried.answer;
  if (status === 200 && out.revoked === true) say(`Airlock: token handed back, run ${out.run ?? '-'}`);
  else if (status === 404) missed(`Airlock: revoke not available; ${expires}`);
  else if (status >= 400 && status < 500 && out.decision === 'refused') missed(`Airlock: ${status} refused (${out.reason}: ${out.message}); ${expires}`);
  else missed(`Airlock: ${status}${out.reason ? ` (${out.reason}: ${out.message})` : ''}, not handed back; ${expires}`);
}
