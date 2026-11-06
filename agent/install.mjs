import { spawnSync } from 'node:child_process';
const provider = process.env.PROVIDER;
const version = process.env.CLI_VERSION;
if (!['claude', 'codex'].includes(provider) || !/^\d+\.\d+\.\d+$/.test(version || '')) throw new Error('select a pinned CLI version (X.Y.Z)');
const env = Object.fromEntries(Object.entries(process.env).filter(([k]) => !/(TOKEN|SECRET|API_KEY|AUTH_JSON)/i.test(k)));
const r = spawnSync('npm', ['install', '-g', `${provider === 'codex' ? '@openai/codex' : '@anthropic-ai/claude-code'}@${version}`], { env, stdio: 'inherit' });
process.exit(r.status ?? 1);
