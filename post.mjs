// The post step, which the runner runs when the job ends, on success, failure or cancel (post-if: always()): it
// hands the run's token back (POST /v1/revoke), so no token outlives its run and Airlock's stamps stop citing it, and
// ends the read-only token. It never fails the job, and every line it prints has the tokens cut out.
import { airlockUrl, headers, input, oidcToken } from './lib.mjs';

const role = input('role');
const token = process.env.STATE_token ?? '';
const readToken = process.env.STATE_read_token ?? '';
const AIRLOCK = airlockUrl();
const expires = 'the token expires within the hour';
const say = (line) => console.log([token, readToken].filter(Boolean).reduce((l, t) => l.split(t).join('***'), line));

// The read-only token ends itself: GitHub's DELETE /installation/token, authenticated by the token alone.
if (readToken) {
  try {
    const api = (process.env.GITHUB_API_URL || 'https://api.github.com').replace(/\/+$/, '');
    const r = await fetch(`${api}/installation/token`, { method: 'DELETE', headers: { authorization: `Bearer ${readToken}`, accept: 'application/vnd.github+json' }, signal: AbortSignal.timeout(10000) });
    say(r.status === 204 ? 'Airlock: read-only token ended' : `Airlock: read-only token not ended (${r.status}); it expires within the hour`);
  } catch (err) {
    say(`Airlock: read-only token not ended (${err?.name ?? 'Error'}); it expires within the hour`);
  }
}
if (!token) {
  say('Airlock: no token to hand back');
  process.exit(0);
}
const timeout = AbortSignal.timeout(20000);
let res;
let out = {};
try {
  // Each OIDC token is used once, so the hand-back asks for a fresh one.
  const id = await oidcToken(AIRLOCK, timeout);
  if (!id) {
    say(`Airlock: no OIDC token available to this job (id-token: write missing); ${expires}`);
    process.exit(0);
  }
  res = await fetch(`${AIRLOCK}/v1/revoke`, { method: 'POST', headers: headers(id), body: JSON.stringify({ role, token }), signal: timeout });
  out = await res.json().catch(() => ({}));
} catch (err) {
  say(`Airlock: unreachable (${err?.name ?? 'Error'}); ${expires}`);
  process.exit(0);
}
if (res.status === 200 && out.revoked === true) say(`Airlock: token handed back, run ${out.run ?? '-'}`);
else if (res.status >= 400 && res.status < 500 && out.decision === 'refused') say(`Airlock: ${res.status} refused (${out.reason}: ${out.message})`);
else say(`Airlock: ${res.status}${out.reason ? ` (${out.reason}: ${out.message})` : ''}, not handed back; ${expires}`);
process.exit(0);
