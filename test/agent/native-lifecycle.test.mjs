import { test } from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { nativeCodex } from '../../agent/native-codex.mjs';

test('closure between initialization and next RPC rejects immediately', {timeout:1000}, async()=>{
 const child=new EventEmitter();child.stdout=new EventEmitter();child.stdin=new EventEmitter();child.kill=()=>true;
 const methods=[];
 child.stdin.write=(line,callback)=>{
  const message=JSON.parse(line);methods.push(message.method);callback();
  if(message.method==='initialize')child.stdout.emit('data',Buffer.from(JSON.stringify({id:message.id,result:{}})+'\n'));
  if(message.method==='initialized')child.emit('close',1);
 };
 await assert.rejects(nativeCodex(child,{plan:{auth:'api-key'},cwd:process.cwd(),home:'/unused',apiKey:'synthetic',call:async()=>{},persist:async()=>{}}),/closed/);
 assert.deepEqual(methods,['initialize','initialized']);
});

import {mkdtemp,mkdir,realpath,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';

test('split UTF-8 native questions retain their exact content and IDs', {timeout:2000}, async()=>{
 const base=await realpath(await mkdtemp(join(tmpdir(),'native-decode-'))),cwd=join(base,'work'),home=join(base,'home');
 await mkdir(cwd);await mkdir(home);
 const child=new EventEmitter();child.stdout=new EventEmitter();child.stdin=new EventEmitter();child.kill=()=>true;
 const emit=value=>child.stdout.emit('data',Buffer.from(JSON.stringify(value)+'\n'));
 let observed;
 const hash='sha256:'+'a'.repeat(64);
 const quote=s=>`'${s.replaceAll("'","'\\''")}'`;
 const hook={key:'fixture-hook',sourcePath:join(home,'config.toml'),source:'user',eventName:'preToolUse',handlerType:'command',command:`python3 ${quote(join(home,'guard.py'))} ${quote(home)}`,matcher:'.*',timeoutSec:10,async:false,enabled:true,isManaged:false,currentHash:hash};
 child.stdin.write=(line,callback)=>{
  const m=JSON.parse(line);callback();
  if(!m.id)return;
  let result={};
  if(m.method==='account/read')result={account:{type:'apiKey'}};
  if(m.method==='hooks/list')result={data:[{hooks:[{...hook,trustStatus:m.params.threadId?'trusted':'untrusted'}],errors:[],warnings:[]}]};
  if(m.method==='thread/start')result={approvalPolicy:'never',cwd,sandbox:{type:'workspaceWrite',networkAccess:true,excludeTmpdirEnvVar:true,excludeSlashTmp:true,writableRoots:[]},thread:{id:'thread',environments:[{environmentId:'local',cwd,runtimeWorkspaceRoots:[cwd]}]}};
  emit({id:m.id,result});
  if(m.method==='turn/start')setImmediate(()=>{
   const request={id:'café',method:'item/tool/requestUserInput',params:{threadId:'thread',questions:[{id:'café',question:'Café?'}]}};
   const bytes=Buffer.from(JSON.stringify(request)+'\n'),cut=bytes.indexOf(Buffer.from('é'))+1;
   child.stdout.emit('data',bytes.subarray(0,cut));child.stdout.emit('data',bytes.subarray(cut));
  });
 };
 try {
  await nativeCodex(child,{plan:{auth:'api-key',role:'engineer'},cwd,home,apiKey:'synthetic',persist:async()=>{},call:async body=>{
   if(body.operation==='observe'){
    observed=body.native_request;
    setImmediate(()=>{emit({method:'serverRequest/resolved',params:{threadId:'thread',requestId:'café'}});emit({method:'turn/completed',params:{threadId:'thread',turn:{status:'completed'}}});});
    return {receipt:{interaction_id:'question',state:'pending'}};
   }
   return {receipt:{interaction_id:'question',state:'resolved'}};
  }});
  assert.equal(observed.id,'café');assert.equal(observed.params.questions[0].question,'Café?');
 }finally{await rm(base,{recursive:true,force:true});}
});
