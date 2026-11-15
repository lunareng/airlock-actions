// Subprocess side of the real Airlock/DynamoDB qualification. No live credentials.
import { createInterface } from 'node:readline';
import assert from 'node:assert/strict';
import { InteractionBridge } from '../../agent/interactions.mjs';
const input = createInterface({input:process.stdin})[Symbol.asyncIterator]();
const next = async () => JSON.parse((await input.next()).value);
const {native, scenario} = await next();
const writes = [], intents = [];
let deliveries = 0;
const bridge = new InteractionBridge({role:'engineer',provider:'codex',session:'thread-proof',epoch:'process-one',pending:()=>true,
  call:async body=>{
    if(body.operation==='deliver') deliveries++;
    process.stdout.write(JSON.stringify({body})+'\n');
    const response=await next();
    if(response.error) throw new Error('injected transport refusal');
    return response.value;
  },
  persist:async receipt=>{intents.push(receipt);if(scenario==='before-write')throw new Error('crash before claim');},
  write:async response=>{writes.push(response);if(scenario==='after-write')throw new Error('crash after write');},
});
const entry=await bridge.observe(native);
if(scenario==='changed-id') {
  await assert.rejects(bridge.observe({...native,params:{...native.params,itemId:'changed'}}));
} else if(scenario==='success') {
  await bridge.check(entry);
  await bridge.check(entry);
  assert.deepEqual(writes,[{id:0,result:{answers:{proof_color:{answers:['Blue']}}}}]);
  await bridge.resolved(entry);
} else {
  await assert.rejects(bridge.check(entry));
  // The same object cannot resend after any uncertain boundary.
  await bridge.check(entry);
}
assert.equal(writes.length,['success','after-write'].includes(scenario)?1:0);
assert.ok(deliveries<=1);
process.stdout.write(JSON.stringify({done:true,writes:writes.length,deliveries,intents:intents.length})+'\n');
process.stdin.destroy();
