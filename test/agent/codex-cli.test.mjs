// Actual pinned Codex CLI, local mock Responses server, no provider credentials or paid inference.
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {spawn,spawnSync} from 'node:child_process';
import {mkdtemp,mkdir,writeFile,copyFile,readFile,access,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {codexConfig} from '../../agent/run.mjs';

for(const scenario of ['deny','git','guard']) test(`actual Codex CLI ${scenario==='git'?'allows declared git commit':scenario==='guard'?'protects the external guard policy':'blocks undeclared shell command'}`,{timeout:30000},async()=>{
 const permitted=scenario==='git';
 const base=await mkdtemp(join(tmpdir(),'codex-cli-smoke-'));const home=join(base,'home'),cwd=join(base,'work');await mkdir(home);await mkdir(cwd);await mkdir(join(cwd,'repos'),{recursive:true});await mkdir(join(cwd,'repos/app'));
 assert.equal(spawnSync('git',['init','-q'],{cwd:join(cwd,'repos/app')}).status,0);
 const p={auth:'api-key',mcp:{},writeGitDirs:permitted?['repos/app/.git']:[]};
 await writeFile(join(home,'config.toml'),codexConfig(p,home,cwd));
 await copyFile(fileURLToPath(new URL('../../agent/guard.py',import.meta.url)),join(home,'guard.py'));
 await writeFile(join(home,'guard-policy.json'),JSON.stringify({bash:scenario==='guard'?['python3']:['git'],mcp:[],edit:permitted,maxTools:10}));
 let requests=0,toolOutput='';
 const server=createServer(async(req,res)=>{
  let body='';for await(const b of req)body+=b;let request;try{request=JSON.parse(body)}catch{}
  if(!request){res.writeHead(404);res.end();return;}
  requests++;toolOutput=JSON.stringify(request.input.filter(x=>x.type==='function_call_output'));
  const name=request.tools?.find(t=>['shell','shell_command','exec_command'].includes(t.name))?.name;
  const command=scenario==='guard'?`python3 -c 'open("${home}/guard-policy.json", "w").write("changed")'`:permitted?'git -C repos/app -c user.name=Test -c user.email=test@example.com commit --allow-empty -m offline-smoke':'touch forbidden-file';
  const args=name==='shell'?{command:['/bin/sh','-c',command],workdir:cwd}:name==='exec_command'?{cmd:command,workdir:cwd}:{command,workdir:cwd};
  const item=requests===1&&name?{type:'function_call',id:'fc_1',call_id:'call_1',name,arguments:JSON.stringify(args)}:{type:'message',id:'msg_1',role:'assistant',content:[{type:'output_text',text:'offline done'}],status:'completed'};
  const response={id:`resp_${requests}`,object:'response',created_at:Math.floor(Date.now()/1000),status:'completed',output:[item],usage:{input_tokens:1,output_tokens:1,total_tokens:2}};
  res.writeHead(200,{'content-type':'text/event-stream'});
  for(const [type,data] of [['response.created',{response:{...response,status:'in_progress',output:[]}}],['response.output_item.added',{output_index:0,item}],['response.output_item.done',{output_index:0,item}],['response.completed',{response}]])res.write(`event: ${type}\ndata: ${JSON.stringify({type,...data})}\n\n`);
  res.end();
 });
 await new Promise(ok=>server.listen(0,'127.0.0.1',ok));
 let child;
 try{
  const args=['exec','--strict-config','--dangerously-bypass-hook-trust','--skip-git-repo-check','--ephemeral','--json','--model','offline-test','--disable','unbounded_connection_retries','-c','model_provider="offline"','-c','model_providers.offline.name="Offline"','-c',`model_providers.offline.base_url="http://127.0.0.1:${server.address().port}"`,'-c','model_providers.offline.wire_api="responses"','-c','model_providers.offline.requires_openai_auth=false','-c','model_providers.offline.supports_websockets=false','-'];
  child=spawn(process.env.CODEX_TEST_BINARY||'codex',args,{cwd,env:{PATH:process.env.PATH,CODEX_HOME:home},stdio:['pipe','pipe','pipe']});
  child.stdin.end('Execute the tool request, then finish.');let stderr='';child.stderr.on('data',b=>stderr+=b);child.stdout.resume();
  const timer=setTimeout(()=>child.kill('SIGKILL'),20000);
  let status;try{status=await new Promise((ok,bad)=>{child.on('error',bad);child.on('close',ok);});}finally{clearTimeout(timer);}
  assert.equal(status,0,stderr);assert.equal(requests,2);
  if(permitted){const log=spawnSync('git',['log','--oneline'],{cwd:join(cwd,'repos/app'),encoding:'utf8'});assert.match(log.stdout,/offline-smoke/);}
  else if(scenario==='guard'){assert.match(toolOutput,/PermissionError|Operation not permitted|Permission denied/);assert.equal(JSON.parse(await readFile(join(home,'guard-policy.json'),'utf8')).maxTools,10);}
  else{assert.match(toolOutput,/blocked by PreToolUse hook/);await assert.rejects(access(join(cwd,'forbidden-file')));}
  assert.ok(Number(await readFile(join(home,'guard-count'),'utf8'))>=1,'the real CLI ran the hook');
 }finally{child?.kill('SIGKILL');server.closeAllConnections();await new Promise(ok=>server.close(ok));await rm(base,{recursive:true,force:true});}
});
