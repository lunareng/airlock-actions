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
import {nativeCodex} from '../../agent/native-codex.mjs';

for(const scenario of ['question','deny','git','guard','unexpected-hook','broader-root']) test(`pinned Codex app-server ${scenario}`,{timeout:30000},async()=>{
 const permitted=scenario==='git';
 const base=await mkdtemp(join(tmpdir(),'codex-cli-smoke-'));const home=join(base,'home'),cwd=join(base,'work');await mkdir(home);await mkdir(cwd);await mkdir(join(cwd,'repos'),{recursive:true});await mkdir(join(cwd,'repos/app'));
 assert.equal(spawnSync('git',['init','-q'],{cwd:join(cwd,'repos/app')}).status,0);
 const p={auth:'api-key',mcp:{},interactions:true,role:'engineer',model:'offline-test',prompt:'Ask a question then finish.',writeGitDirs:permitted?['repos/app/.git']:[]};
 let config=codexConfig(p,home,cwd);
 if(scenario==='unexpected-hook')config=config.replace('matcher = ".*"','matcher = "shell"');
 if(scenario==='broader-root')config=config.replace('writable_roots = []',`writable_roots = ${JSON.stringify([base])}`);
 await writeFile(join(home,'config.toml'),config);
 await copyFile(fileURLToPath(new URL('../../agent/guard.py',import.meta.url)),join(home,'guard.py'));
 await writeFile(join(home,'guard-policy.json'),JSON.stringify({bash:scenario==='guard'?['python3']:['git'],mcp:[],edit:permitted,maxTools:10,interactions:true}));
 let requests=0,toolOutput='';
 const server=createServer(async(req,res)=>{
  let body='';for await(const b of req)body+=b;let request;try{request=JSON.parse(body)}catch{}
  if(!request){res.writeHead(404);res.end();return;}
  if(req.headers.authorization!=='Bearer offline-synthetic-key'){res.writeHead(401);res.end();return;}
  requests++;toolOutput=JSON.stringify(request.input.filter(x=>x.type==='function_call_output'));
  const name=request.tools?.find(t=>scenario==='question'?t.name==='request_user_input':['shell','shell_command','exec_command'].includes(t.name))?.name;
  const command=scenario==='guard'?`python3 -c 'open("${home}/guard-policy.json", "w").write("changed")'`:permitted?'git -C repos/app -c user.name=Test -c user.email=test@example.com commit --allow-empty -m offline-smoke':'touch forbidden-file';
  const args=scenario==='question'?{questions:[{id:'choice',header:'Choice',question:'Which color?',options:[{label:'Blue',description:'Use blue'},{label:'Green',description:'Use green'}]}]}:name==='shell'?{command:['/bin/sh','-c',command],workdir:cwd}:name==='exec_command'?{cmd:command,workdir:cwd}:{command,workdir:cwd};
  const item=requests===1&&name?{type:'function_call',id:'fc_1',call_id:'call_1',name,arguments:JSON.stringify(args)}:{type:'message',id:'msg_1',role:'assistant',content:[{type:'output_text',text:'offline done'}],status:'completed'};
  const response={id:`resp_${requests}`,object:'response',created_at:Math.floor(Date.now()/1000),status:'completed',output:[item],usage:{input_tokens:1,output_tokens:1,total_tokens:2}};
  res.writeHead(200,{'content-type':'text/event-stream'});
  for(const [type,data] of [['response.created',{response:{...response,status:'in_progress',output:[]}}],['response.output_item.added',{output_index:0,item}],['response.output_item.done',{output_index:0,item}],['response.completed',{response}]])res.write(`event: ${type}\ndata: ${JSON.stringify({type,...data})}\n\n`);
  res.end();
 });
 await new Promise(ok=>server.listen(0,'127.0.0.1',ok));
 let child;
 try{
  const args=['app-server','--strict-config','--listen','stdio://','-c','model_provider="offline"','-c','model_providers.offline.name="Offline"','-c',`model_providers.offline.base_url="http://127.0.0.1:${server.address().port}"`,'-c','model_providers.offline.wire_api="responses"','-c','model_providers.offline.requires_openai_auth=true','-c','model_providers.offline.supports_websockets=false'];
  child=spawn(process.env.CODEX_TEST_BINARY||'codex',args,{cwd,env:{PATH:process.env.PATH,CODEX_HOME:home,CODEX_API_KEY:'offline-synthetic-key'},stdio:['pipe','pipe','pipe']});
  let stderr='';child.stderr.on('data',b=>stderr+=b);
  const timer=setTimeout(()=>child.kill('SIGKILL'),20000);
  let observed, sends=0, persisted=0;
  try {
    const running=nativeCodex(child,{plan:p,cwd,home,apiKey:'offline-synthetic-key',pollMs:10,persist:async()=>{persisted++},call:async body=>{
      if(body.operation==='observe') { const state=observed?'accepted':'pending'; observed=body.native_request; return {receipt:{interaction_id:'proof',state}}; }
      if(body.operation==='deliver') { sends++; return {receipt:{interaction_id:'proof',state:'sending'},native_response:{id:observed.id,result:{answers:{choice:{answers:['Blue']}}}}}; }
      if(body.operation==='resolve') return {receipt:{interaction_id:'proof',state:'resolved'}};
      throw new Error('unexpected operation');
    }});
    if(['unexpected-hook','broader-root'].includes(scenario)){await assert.rejects(running,/native .*not verified|native runtime did not preserve/);assert.equal(requests,0);return;}
    const result=await running;
    assert.equal(result,'offline done',stderr);
    assert.equal(requests,2);
    if(scenario==='question'){assert.match(toolOutput,/Blue/);assert.equal(sends,1);assert.equal(persisted,1);}
    else {
      assert.equal(sends,0);
      assert.ok(Number(await readFile(join(home,'guard-count'),'utf8').catch(()=>{throw new Error('MISSING HOOK '+stderr+' OUTPUT '+toolOutput)}))>=1,'native execution passed the policy hook');
      if(scenario==='deny'){assert.match(toolOutput,/blocked by PreToolUse hook/);await assert.rejects(access(join(cwd,'forbidden-file')));}
      if(scenario==='git'){assert.match(spawnSync('git',['log','--oneline'],{cwd:join(cwd,'repos/app'),encoding:'utf8'}).stdout,/offline-smoke/);}
      if(scenario==='guard'){assert.match(toolOutput,/PermissionError|Operation not permitted|Permission denied|Read-only file system/);assert.equal(JSON.parse(await readFile(join(home,'guard-policy.json'),'utf8')).maxTools,10);}
    }
  } finally {clearTimeout(timer);}
 }finally{child?.kill('SIGKILL');server.closeAllConnections();await new Promise(ok=>server.close(ok));await rm(base,{recursive:true,force:true});}
});
