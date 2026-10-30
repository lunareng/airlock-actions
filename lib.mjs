// What the main and post steps share: the action's inputs, the files the runner hands a step, the GitHub OIDC token,
// and the one check every secret passes before it is masked or written anywhere. No dependencies: the runner's Node
// and these files are the whole action, so there is nothing to build and nothing to drift from what was reviewed.
import { appendFileSync } from 'node:fs';

export const VERSION = '1.0.0';

// An input as the runner passes it: INPUT_<NAME>, the name upper-cased with spaces as underscores (hyphens kept).
export const input = (name) => (process.env[`INPUT_${name.replace(/ /g, '_').toUpperCase()}`] ?? '').trim();

export const airlockUrl = () => (input('airlock-url') || 'https://airlock.lunareng.com').replace(/\/+$/, '');

// A secret that can't break a step output, an env line or the mask: one line of token characters, no whitespace or
// control characters. No prefix or length check beyond that: GitHub changes its token format.
export const safe = (value) => typeof value === 'string' && /^[A-Za-z0-9_.~+/=-]{20,4096}$/.test(value);

export const mask = (value) => process.stdout.write(`::add-mask::${value}\n`);

function append(file, what, line) {
  if (!file) throw new Error(`the runner gave this step no ${what} file`);
  appendFileSync(file, `${line}\n`);
}
export const setOutput = (name, value) => append(process.env.GITHUB_OUTPUT, 'GITHUB_OUTPUT', `${name}=${value}`);
export const exportEnv = (name, value) => append(process.env.GITHUB_ENV, 'GITHUB_ENV', `${name}=${value}`);
// State the post step reads back as STATE_<name>.
export const saveState = (name, value) => append(process.env.GITHUB_STATE, 'GITHUB_STATE', `${name}=${value}`);

// The job's GitHub OIDC token, audience Airlock's origin: the run's only proof of where it runs. Each is used once.
export async function oidcToken(audience, signal) {
  const { ACTIONS_ID_TOKEN_REQUEST_URL: url, ACTIONS_ID_TOKEN_REQUEST_TOKEN: bearer } = process.env;
  if (!url || !bearer) return null;
  const res = await fetch(`${url}&audience=${encodeURIComponent(audience)}`, { headers: { authorization: `bearer ${bearer}` }, signal });
  const body = await res.json().catch(() => ({}));
  return typeof body.value === 'string' ? body.value : null;
}

export const headers = (bearer) => ({ authorization: `Bearer ${bearer}`, 'content-type': 'application/json', 'user-agent': `airlock-actions/${VERSION}` });
