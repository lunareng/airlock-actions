// Actual pinned Codex CLI, local mock Responses API and mock MCP server: a role's MCP tools reach the server
// with its session header, and a tool outside the role file never does. No provider credentials or paid inference.
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {spawn} from 'node:child_process';
import {mkdtemp,mkdir,writeFile,copyFile,readFile,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {codexConfig} from '../../agent/run.mjs';

const SESSION='test-mcp-session';
const scenarios=[
 {name:'calls a tool the role file lists',tools:['board_read'],header:'X-API-Key',call:'board_read',reaches:true},
 {name:'never offers or calls a tool the role file leaves out',tools:['board_read'],header:'X-API-Key',call:'board_post',reaches:false},
 {name:'calls a tool a role-file pattern matches',tools:['board_*'],call:'board_read',reaches:true},
 {name:'blocks a tool no role-file pattern matches',tools:['board_*'],call:'admin_delete',reaches:false,hook:true},
];

for(const s of scenarios) test(`actual Codex CLI ${s.name}`,{timeout:30000},async()=>{
 const base=await mkdtemp(join(tmpdir(),'codex-mcp-'));const home=join(base,'home'),cwd=join(base,'work');await mkdir(home);await mkdir(cwd);
 const calls=[],keys=[];
 const mcp=createServer(async(req,res)=>{
  let body='';for await(const b of req)body+=b;
  keys.push(s.header?req.headers['x-api-key']:req.headers.authorization);
  if(req.method!=='POST'){res.writeHead(405);res.end();return;}
  const m=JSON.parse(body);
  if(m.id===undefined){res.writeHead(202);res.end();return;}
  if(m.method==='tools/call')calls.push(m.params.name);
  const result=m.method==='initialize'?{protocolVersion:m.params.protocolVersion,capabilities:{tools:{}},serverInfo:{name:'mock',version:'1'}}
   :m.method==='tools/list'?{tools:['board_read','board_post','admin_delete'].map(name=>({name,description:name,inputSchema:{type:'object',properties:{}}}))}
   :m.method==='tools/call'?{content:[{type:'text',text:`mock-ok:${m.params.name}`}],isError:false}:{};
  res.writeHead(200,{'content-type':'application/json'});res.end(JSON.stringify({jsonrpc:'2.0',id:m.id,result}));
 });
 await new Promise(ok=>mcp.listen(0,'127.0.0.1',ok));
 const spec={url:'https://mcp.invalid/mcp',...(s.header?{header:s.header}:{})};
 // codexConfig accepts only https endpoints; the test swaps in its local server and changes nothing else.
 const config=codexConfig({auth:'api-key',mcp:{board:spec},tools:{board:s.tools}},home,cwd);
 await writeFile(join(home,'config.toml'),config.replace('url = "https://mcp.invalid/mcp"',`url = "http://127.0.0.1:${mcp.address().port}/mcp"`));
 await copyFile(fileURLToPath(new URL('../../agent/guard.py',import.meta.url)),join(home,'guard.py'));
 // The guard policy run.mjs writes for this role.
 await writeFile(join(home,'guard-policy.json'),JSON.stringify({bash:[],mcp:s.tools.map(t=>`mcp__board__${t}`),edit:false,maxTools:10}));
 let requests=0,offered=[],toolOutput='';
 const server=createServer(async(req,res)=>{
  let body='';for await(const b of req)body+=b;let request;try{request=JSON.parse(body)}catch{}
  if(!request){res.writeHead(404);res.end();return;}
  requests++;
  if(requests===1)offered=(request.tools.find(t=>t.name==='mcp__board')?.tools||[]).map(t=>t.name);
  toolOutput=JSON.stringify(request.input.filter(x=>x.type==='function_call_output'));
  const item=requests===1?{type:'function_call',id:'fc_1',call_id:'call_1',namespace:'mcp__board',name:s.call,arguments:'{}'}:{type:'message',id:'msg_1',role:'assistant',content:[{type:'output_text',text:'offline done'}],status:'completed'};
  const response={id:`resp_${requests}`,object:'response',created_at:Math.floor(Date.now()/1000),status:'completed',output:[item],usage:{input_tokens:1,output_tokens:1,total_tokens:2}};
  res.writeHead(200,{'content-type':'text/event-stream'});
  for(const [type,data] of [['response.created',{response:{...response,status:'in_progress',output:[]}}],['response.output_item.added',{output_index:0,item}],['response.output_item.done',{output_index:0,item}],['response.completed',{response}]])res.write(`event: ${type}\ndata: ${JSON.stringify({type,...data})}\n\n`);
  res.end();
 });
 await new Promise(ok=>server.listen(0,'127.0.0.1',ok));
 let child;
 try{
  const args=['exec','--strict-config','--dangerously-bypass-hook-trust','--skip-git-repo-check','--ephemeral','--json','--model','offline-test','--disable','unbounded_connection_retries','-c','model_provider="offline"','-c','model_providers.offline.name="Offline"','-c',`model_providers.offline.base_url="http://127.0.0.1:${server.address().port}"`,'-c','model_providers.offline.wire_api="responses"','-c','model_providers.offline.requires_openai_auth=false','-c','model_providers.offline.supports_websockets=false','-'];
  child=spawn(process.env.CODEX_TEST_BINARY||'codex',args,{cwd,env:{PATH:process.env.PATH,CODEX_HOME:home,ROLE_MCP_BOARD_TOKEN:SESSION},stdio:['pipe','pipe','pipe']});
  child.stdin.end('Execute the tool request, then finish.');let stderr='';child.stderr.on('data',b=>stderr+=b);child.stdout.resume();
  const timer=setTimeout(()=>child.kill('SIGKILL'),20000);
  let status;try{status=await new Promise((ok,bad)=>{child.on('error',bad);child.on('close',ok);});}finally{clearTimeout(timer);}
  assert.equal(status,0,stderr);assert.equal(requests,2);
  // The session reaches the server only as the header the role file names.
  assert.ok(keys.length>0);assert.ok(keys.every(k=>k===(s.header?SESSION:`Bearer ${SESSION}`)),JSON.stringify(keys));
  if(s.tools.every(t=>!t.includes('*')))assert.deepEqual(offered,s.tools);
  if(s.reaches){assert.deepEqual(calls,[s.call]);assert.match(toolOutput,new RegExp(`mock-ok:${s.call}`));}
  else{assert.deepEqual(calls,[]);assert.doesNotMatch(toolOutput,/mock-ok/);}
  if(s.hook)assert.match(toolOutput,/blocked by PreToolUse hook/);
  if(s.reaches||s.hook)assert.ok(Number(await readFile(join(home,'guard-count'),'utf8'))>=1,'the real CLI ran the hook');
 }finally{child?.kill('SIGKILL');server.closeAllConnections();mcp.closeAllConnections();await new Promise(ok=>server.close(ok));await new Promise(ok=>mcp.close(ok));await rm(base,{recursive:true,force:true});}
});
