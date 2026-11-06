import { writeFile, readFile } from 'node:fs/promises';

export function subscription(value) {
  if (value?.auth_mode !== 'chatgpt' || value.OPENAI_API_KEY != null || !value.tokens ||
      !['access_token', 'refresh_token', 'id_token'].every(k => typeof value.tokens[k] === 'string' && /^[A-Za-z0-9._~+/=-]{8,32768}$/.test(value.tokens[k]))) throw new Error('Codex subscription login is missing or invalid');
  return value;
}

export function brokerClient(url, env = process.env, fetcher = fetch) {
  const base = new URL(url);
  if (base.protocol !== 'https:' || base.username || base.password || base.search || base.hash || base.pathname !== '/') throw new Error('runner-auth-url must be an HTTPS origin');
  const oidcUrl = new URL(env.ACTIONS_ID_TOKEN_REQUEST_URL);
  if (oidcUrl.protocol !== 'https:') throw new Error('GitHub OIDC is unavailable');
  oidcUrl.searchParams.set('audience', 'lunar-runner-auth');
  return async (operation, body) => {
    // Fresh proof on every operation, including cleanup after the agent exits.
    const proof = await fetcher(oidcUrl, { headers: { Authorization: `bearer ${env.ACTIONS_ID_TOKEN_REQUEST_TOKEN}` }, signal: AbortSignal.timeout(4000), redirect: 'error' });
    if (!proof.ok) throw new Error('GitHub OIDC request failed');
    const { value } = await proof.json();
    if (typeof value !== 'string' || !value) throw new Error('GitHub OIDC request failed');
    const res = await fetcher(new URL(`/v1/${operation}`, base), { method: 'POST', headers: { Authorization: `Bearer ${value}`, 'content-type': 'application/json' }, body: JSON.stringify({ credential: 'ci', ...body }), signal: AbortSignal.timeout(4000), redirect: 'error' });
    if (!res.ok) throw new Error(`runner auth ${operation} refused (${res.status}); inspect the lease or reseed the dedicated CI login`);
    return res.json();
  };
}

export async function leaseAuth(home, call) {
  const { auth, receipt } = await call('acquire', { ttlSeconds: 3300 });
  subscription(auth);
  await writeFile(`${home}/auth.json`, JSON.stringify(auth), { mode: 0o600, flag: 'wx' });
  return {
    auth,
    async save() {
      const refreshed = subscription(JSON.parse(await readFile(`${home}/auth.json`, 'utf8')));
      await call('commit', { receipt, auth: refreshed });
      // A failed commit retains the lease. Never let another job read a stale seed as a success.
      await call('release', { receipt });
      return refreshed;
    },
  };
}
