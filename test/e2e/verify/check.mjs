// Runs after the Airlock action's post step: the job passes only if the stand-in was asked for one token and got
// that token back, both calls carried the action's version, and the hand-back came with its own OIDC token.
import { readFileSync } from 'node:fs';

const version = JSON.parse(readFileSync(new URL('../../../package.json', import.meta.url), 'utf8')).version;
const calls = readFileSync(process.env.STATE_log, 'utf8').split('\n').filter(Boolean).map((l) => JSON.parse(l));
const mint = calls.filter((c) => c.path === '/v1/mint');
const revoke = calls.filter((c) => c.path === '/v1/revoke');
const problems = [];
if (mint.length !== 1) problems.push(`the stand-in was asked for ${mint.length} tokens, not 1`);
if (revoke.length !== 1 || revoke[0].handedBack !== true) problems.push('the token was not handed back');
for (const c of [...mint, ...revoke]) if (c.action !== version) problems.push(`${c.path} sent Airlock-Action ${c.action}, not ${version}`);
for (const c of [...mint, ...revoke]) if (c.role !== 'e2e') problems.push(`${c.path} named role ${c.role}`);
if (mint[0] && revoke[0] && mint[0].proof === revoke[0].proof) problems.push('the hand-back reused the mint\'s OIDC token');
for (const p of problems) console.log(`::error::${p}`);
if (problems.length) process.exit(1);
console.log(`Airlock e2e: the token was handed back after the agent step failed (Airlock-Action ${version})`);
