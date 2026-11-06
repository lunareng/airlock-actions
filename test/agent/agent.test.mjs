import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, readFile, readdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { selectAgent } from '../../agent/provider.mjs';
import { codexConfig, agentEnvironment, execute } from '../../agent/run.mjs';
import { brokerClient, subscription } from '../../agent/auth.mjs';

const plan = { provider: 'codex', auth: 'api-key', model: 'gpt-example', maxTurns: 10, prompt: 'complete this task', bash: ['git status', 'gh issue'], edit: false, mcp: {}, tools: {}, allowed: 'Read', mcpConfig: '/tmp/mcp.json' };
const auth = { auth_mode: 'chatgpt', tokens: { access_token: 'access-secret-123', refresh_token: 'refresh-secret-123', id_token: 'identity-secret-123' } };

for (const provider of ['claude','codex']) for (const mode of ['subscription','api-key']) test(`${provider}/${mode}: explicit selection`, () => {
  assert.deepEqual(selectAgent({ model: provider === 'claude' ? 'claude-example' : 'gpt-example' }, { INPUT_PROVIDER: provider, INPUT_AUTH: mode }), { provider, auth: mode, model: provider === 'claude' ? 'claude-example' : 'gpt-example' });
});
test('legacy defaults, fleet override, per-run override, clear-to-default and model matching', () => {
  const run = { model: 'claude-example' };
  assert.equal(selectAgent(run).provider, 'claude'); assert.equal(selectAgent(run).auth, 'subscription');
  assert.equal(selectAgent(run, { AIRLOCK_AGENT_PROVIDER: 'codex', AIRLOCK_CODEX_MODEL: 'gpt-example' }).provider, 'codex');
  assert.equal(selectAgent(run, { INPUT_PROVIDER: 'claude', AIRLOCK_AGENT_PROVIDER: 'codex' }).provider, 'claude');
  assert.equal(selectAgent(run, { AIRLOCK_AGENT_PROVIDER: '' }).provider, 'claude');
  for (const env of [{ INPUT_PROVIDER:'other' }, { INPUT_AUTH:'auto' }, { INPUT_PROVIDER:'codex' }, { AIRLOCK_CLAUDE_MODEL:'gpt-example' }, { AIRLOCK_CLAUDE_MODEL:'claude\nINJECT=1' }]) assert.throws(() => selectAgent(run, env));
});
test('inactive model credentials and GitHub OIDC never reach the provider process', () => {
  const env = { PATH:'/bin', GH_TOKEN:'scoped-github', ACTIONS_ID_TOKEN_REQUEST_TOKEN:'job-proof', CLAUDE_CODE_OAUTH_TOKEN:'claude-login', ANTHROPIC_API_KEY:'anthropic-key', OPENAI_API_KEY:'openai-key', CODEX_HOME:'/old' };
  for (const provider of ['claude','codex']) for (const mode of ['subscription','api-key']) {
    const e = agentEnvironment({ ...plan, provider, auth:mode }, env, '/private-home');
    assert.equal(e.GH_TOKEN, 'scoped-github'); assert.equal(e.ACTIONS_ID_TOKEN_REQUEST_TOKEN, undefined);
    const keys = Object.keys(e).filter(k => /TOKEN|API_KEY/.test(k));
    assert.deepEqual(keys.sort(), ['GH_TOKEN', ...(provider === 'claude' ? [mode === 'subscription' ? 'CLAUDE_CODE_OAUTH_TOKEN' : 'ANTHROPIC_API_KEY'] : mode === 'api-key' ? ['CODEX_API_KEY'] : [])].sort());
  }
  assert.throws(() => agentEnvironment(plan, {}, '/home'), /requires OPENAI_API_KEY/);
});
test('Codex config: sandbox, no interactive approvals, isolated hooks and MCP env references', () => {
  const p = { ...plan, tools: { board:['read','post'] }, mcp: { board:{url:'https://mcp.example/mcp'} } };
  const text = codexConfig(p, '/tmp/private-home', '/work');
  for (const s of ['approval_policy = "never"', 'sandbox_mode = "workspace-write"', 'trust_level = "untrusted"', 'exclude_slash_tmp = true', 'matcher = ".*"', 'bearer_token_env_var = "ROLE_MCP_BOARD_TOKEN"', 'enabled_tools = ["read","post"]']) assert.ok(text.includes(s), s);
  assert.ok(!text.includes('danger-full-access'));
  assert.ok(codexConfig({ ...p, mcp: { board:{url:'https://mcp.example', header:'X-API-Key'} } }, '/tmp/home','/work').includes('env_http_headers = { "X-API-Key" = "ROLE_MCP_BOARD_TOKEN" }'));
  for (const tool of ['WebFetch', 'WebSearch', 'Agent']) assert.throws(() => codexConfig({ ...p, nativeTools:[tool] }, '/tmp/home','/work'), /not yet supported/);
});

async function fixture() {
  const cwd = await mkdtemp(join(tmpdir(),'airlock-agent-test-')); const bin = join(cwd,'bin'); await mkdir(bin);
  const script = `#!/usr/bin/env node
const fs=require('node:fs'),path=require('node:path');
const name=path.basename(process.argv[1]);
const home=process.env.CODEX_HOME||process.env.CLAUDE_CONFIG_DIR;
fs.writeFileSync('observed.json',JSON.stringify({home,env:process.env,args:process.argv.slice(2)}));
if(name==='codex'){
 const authFile=home+'/auth.json'; let secret=process.env.CODEX_API_KEY||'';
 if(fs.existsSync(authFile)){const auth=JSON.parse(fs.readFileSync(authFile));secret=auth.tokens.access_token;auth.tokens.refresh_token='new-refresh-secret';fs.writeFileSync(authFile,JSON.stringify(auth));}
 fs.writeFileSync(home+'/last.txt','finished '+secret);
 console.log(JSON.stringify({type:process.env.FAKE_EVENT||'turn.completed',secret}));
}else{console.log(JSON.stringify({result:'finished '+(process.env.ANTHROPIC_API_KEY||process.env.CLAUDE_CODE_OAUTH_TOKEN),is_error:process.env.FAKE_EVENT==='error'}));}
process.exit(Number(process.env.FAKE_EXIT||0));`;
  for (const cli of ['claude','codex']) await writeFile(join(bin,cli),script,{mode:0o755});
  const env = { PATH: `${bin}:${process.env.PATH}`, RUNNER_TEMP:cwd, OPENAI_API_KEY:'api-secret-key', CLAUDE_CODE_OAUTH_TOKEN:'claude-secret-login', ANTHROPIC_API_KEY:'anthropic-secret-key', GITHUB_STEP_SUMMARY:join(cwd,'summary.md') };
  return { cwd,env };
}
for (const provider of ['claude','codex']) for (const mode of ['subscription','api-key']) test(`launch ${provider}/${mode}, save subscription refresh, redact artifacts, clean home`, async () => {
  const f=await fixture(); const calls=[];
  const broker=()=>async(op,body)=>{calls.push({op,body});return op==='acquire'?{auth:structuredClone(auth),receipt:{leaseId:'lease',version:1}}:{};};
  const p={...plan,provider,auth:mode,model:provider==='codex'?'gpt-example':'claude-example'};
  await execute(p,{...f,env:{...f.env,RUNNER_AUTH_URL:'https://broker.example'},broker});
  const observed=JSON.parse(await readFile(join(f.cwd,'observed.json')));
  await assert.rejects(readdir(observed.home));
  const artifacts=(await readFile(join(f.cwd,'result.txt'),'utf8'))+(await readFile(join(f.cwd,provider==='codex'?'run.jsonl':'run.json'),'utf8'));
  for(const secret of ['api-secret-key','claude-secret-login','anthropic-secret-key','access-secret-123','new-refresh-secret']) assert.ok(!artifacts.includes(secret));
  if(provider==='codex'&&mode==='subscription') {
    assert.deepEqual(calls.map(c=>c.op),['acquire','commit','release']);
    assert.equal(calls[1].body.auth.tokens.refresh_token,'new-refresh-secret');
  } else assert.equal(calls.length,0);
});
test('provider failures still save refreshed login; failed write-back fails run without release', async () => {
  for(const failSave of [false,true]){
    const f=await fixture(); const calls=[];
    const broker=()=>async(op)=>{calls.push(op);if(op==='commit'&&failSave)throw Error('store-secret');return op==='acquire'?{auth:structuredClone(auth),receipt:{}}:{};};
    await assert.rejects(execute({...plan,auth:'subscription'},{...f,env:{...f.env,RUNNER_AUTH_URL:'https://broker.example',FAKE_EXIT:'1'},broker}), failSave?/write-back failed/:/status 1/);
    assert.deepEqual(calls,failSave?['acquire','commit']:['acquire','commit','release']);
  }
});
test('a zero-exit Codex failure and a Claude is_error result are failures', async () => {
  for(const provider of ['codex','claude']){const f=await fixture();await assert.rejects(execute({...plan,provider,auth:'api-key'},{...f,env:{...f.env,FAKE_EVENT:provider==='codex'?'turn.failed':'error'}}));}
});
test('Codex subscription needs a broker; API mode cannot be inferred from available API key',async()=>{
  const f=await fixture();await assert.rejects(execute({...plan,auth:'subscription'},f),/requires runner-auth-url/);
});
test('broker fresh OIDC, no redirects and no response error body leaks',async()=>{
  const calls=[];const call=brokerClient('https://broker.example',{ACTIONS_ID_TOKEN_REQUEST_URL:'https://token.example/job?x=1',ACTIONS_ID_TOKEN_REQUEST_TOKEN:'job-secret'},async(url,options)=>{
    calls.push({url:String(url),options});return new Response(JSON.stringify(calls.length%2===1?{value:'oidc-proof'}:{error:'server-secret'}),{status:calls.length%2===1?200:409});
  });
  await assert.rejects(call('acquire',{ttlSeconds:300}),/refused \(409\)/);
  assert.ok(calls[0].url.includes('audience=lunar-runner-auth'));assert.equal(calls[1].options.redirect,'error');
  assert.throws(()=>subscription({...auth,OPENAI_API_KEY:'key'}));
  assert.throws(()=>brokerClient('http://broker.example',{}));
});
