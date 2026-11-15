import { test } from 'node:test';
import assert from 'node:assert/strict';
import { InteractionBridge, interactionClient } from '../../agent/interactions.mjs';

const native = { id: 0, method: 'item/tool/requestUserInput', params: { threadId: 'thread' } };
function setup({ fail = '', live = () => true } = {}) {
  const operations = [], writes = [], intents = [];
  let observations = 0;
  const bridge = new InteractionBridge({ role: 'engineer', provider: 'codex', session: 'thread', epoch: 'process', pending: live,
    persist: async value => { intents.push(value); if (fail === 'persist') throw new Error('disk'); },
    write: async value => { writes.push(value); if (fail === 'write') throw new Error('pipe'); },
    call: async body => {
      operations.push(body.operation);
      if (body.operation === 'observe') return { receipt: { interaction_id: 'question', state: observations++ ? 'accepted' : 'pending' } };
      if (body.operation === 'deliver') {
        if (fail === 'http') throw new Error('lost response');
        return { receipt: { interaction_id: 'question', state: 'sending' }, native_response: { id: fail === 'identity' ? 1 : 0, result: { answers: { choice: { answers: ['Blue'] } } } } };
      }
      return { receipt: { interaction_id: 'question', state: 'resolved' } };
    },
  });
  return { bridge, operations, writes, intents };
}

test('concurrent polls deliver once after durable local intent', async () => {
  const f = setup(); const entry = await f.bridge.observe(native);
  await Promise.all(Array.from({ length: 8 }, () => f.bridge.check(entry)));
  assert.equal(f.operations.filter(x => x === 'deliver').length, 1);
  assert.equal(f.intents.length, 1); assert.equal(f.writes.length, 1);
  await f.bridge.resolved(entry);
  assert.equal(entry.state, 'resolved');
});

for (const fail of ['persist', 'http', 'write', 'identity']) test(`uncertain ${fail} cannot retry delivery`, async () => {
  const f = setup({ fail }); const entry = await f.bridge.observe(native);
  await assert.rejects(f.bridge.check(entry));
  await f.bridge.check(entry);
  assert.ok(f.operations.filter(x => x === 'deliver').length <= 1);
  assert.equal(f.writes.length, fail === 'write' ? 1 : 0);
});

test('changed native identity and closed connection fail without delivery', async () => {
  const f = setup({ live: () => false });
  const entry = await f.bridge.observe(native);
  await assert.rejects(f.bridge.observe({ ...native, params: { changed: true } }));
  await f.bridge.check(entry); assert.equal(f.writes.length, 0);
});

test('fresh purpose-scoped proof per call and no automatic POST retry', async () => {
  let proofs = 0, posts = 0;
  const fetcher = async (url, options) => {
    assert.equal(options.redirect, 'error');
    if (url.hostname === 'oidc.example') {
      assert.equal(url.searchParams.get('audience'), 'https://airlock.example/v1/interaction-sessions');
      return Response.json({ value: `proof-${++proofs}` });
    }
    assert.equal(options.headers.Authorization, `Bearer proof-${proofs}`); posts++;
    if (posts === 2) throw new Error('PRIVATE-UPSTREAM');
    return Response.json({receipt:{state:'pending'}});
  };
  const call = interactionClient('https://airlock.example', { ACTIONS_ID_TOKEN_REQUEST_URL: 'https://oidc.example/token', ACTIONS_ID_TOKEN_REQUEST_TOKEN: 'synthetic-test' }, fetcher);
  await call({ operation: 'observe' });
  await assert.rejects(call({ operation: 'deliver' }), error => !error.message.includes('PRIVATE-UPSTREAM'));
  assert.equal(proofs, 2); assert.equal(posts, 2);
});

test('transport refuses unsafe origins before requesting credentials', () => {
  for (const origin of ['http://airlock.example', 'https://user@airlock.example', 'https://airlock.example/path', 'https://airlock.example?token=x']) assert.throws(() => interactionClient(origin, {}));
});

test('native cancellation while claiming never writes the answer', async () => {
  let live=true;
  const f=setup({live:()=>live});
  const entry=await f.bridge.observe(native);
  f.bridge.persist=async()=>{live=false;};
  await assert.rejects(f.bridge.check(entry));
  assert.equal(f.writes.length,0);
  assert.equal(f.operations.filter(x=>x==='deliver').length,0);
});

test('oversized private transport response fails without disclosing content', async () => {
  const call=interactionClient('https://airlock.example',{ACTIONS_ID_TOKEN_REQUEST_URL:'https://oidc.example',ACTIONS_ID_TOKEN_REQUEST_TOKEN:'fixture'},async url=>url.hostname==='oidc.example'?Response.json({value:'fixture-proof'}):new Response('PRIVATE'.repeat(40000)));
  await assert.rejects(call({operation:'observe'}),error=>!error.message.includes('PRIVATE'));
});
