// Provider/auth selection is independent of billing; never infer API billing from a missing login.
export function selectAgent(run, env = {}) {
  const provider = env.INPUT_PROVIDER || env.AIRLOCK_AGENT_PROVIDER || run.provider || 'claude';
  const auth = env.INPUT_AUTH || env.AIRLOCK_AGENT_AUTH || 'subscription';
  if (!['claude', 'codex'].includes(provider)) throw new Error('provider must be claude or codex');
  if (!['subscription', 'api-key'].includes(auth)) throw new Error('auth must be subscription or api-key');
  const model = env[provider === 'codex' ? 'AIRLOCK_CODEX_MODEL' : 'AIRLOCK_CLAUDE_MODEL'] || run.model;
  if (typeof model !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(model)) throw new Error('select a model for this provider');
  if (provider === 'codex' && /^(claude|opus|sonnet|haiku)/i.test(model)) throw new Error('Codex needs AIRLOCK_CODEX_MODEL or a Codex model in run.model');
  if (provider === 'claude' && /^(gpt-|o[134](?:-|$)|codex)/i.test(model)) throw new Error('Claude needs AIRLOCK_CLAUDE_MODEL or a Claude model in run.model');
  return { provider, auth, model };
}

if (process.argv[1]?.endsWith('/provider.mjs')) {
  const { readFileSync } = await import('node:fs');
  try { console.log(JSON.stringify(selectAgent(JSON.parse(readFileSync(0, 'utf8')), process.env))); }
  catch (e) { console.error(`Airlock: ${e.message}`); process.exitCode = 1; }
}
