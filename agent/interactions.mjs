async function boundedJSON(response, limit) {
  if (!response.body) throw new Error('missing response');
  const reader = response.body.getReader();
  let size = 0; const chunks = [];
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > limit) throw new Error('response too large');
      chunks.push(Buffer.from(value));
    }
    return JSON.parse(Buffer.concat(chunks).toString('utf8'));
  } finally { await reader.cancel(); }
}

// Private run transport. Neither proofs nor native content belong in job output.
export function interactionClient(origin, env, fetcher = fetch) {
  const base = new URL(origin);
  if (base.protocol !== 'https:' || base.username || base.password || base.search || base.hash || base.pathname !== '/') throw new Error('invalid interaction origin');
  const endpoint = new URL('/v1/interaction-sessions', base);
  const oidc = new URL(env.ACTIONS_ID_TOKEN_REQUEST_URL);
  if (oidc.protocol !== 'https:' || oidc.username || oidc.password || !env.ACTIONS_ID_TOKEN_REQUEST_TOKEN) throw new Error('interaction proof unavailable');
  oidc.searchParams.set('audience', endpoint.href);
  return async body => {
    try {
      const proof = await fetcher(oidc, { headers: { Authorization: `bearer ${env.ACTIONS_ID_TOKEN_REQUEST_TOKEN}` }, redirect: 'error', signal: AbortSignal.timeout(4000) });
      if (!proof.ok) throw new Error();
      const { value } = await boundedJSON(proof, 65536);
      if (typeof value !== 'string' || !value || value.length > 32768) throw new Error();
      const response = await fetcher(endpoint, { method: 'POST', headers: { Authorization: `Bearer ${value}`, 'content-type': 'application/json' }, body: JSON.stringify(body), redirect: 'error', signal: AbortSignal.timeout(12000) });
      if (!response.ok) throw new Error();
      return await boundedJSON(response, 262144);
    } catch {
      // An uncertain POST is never retried here, especially a delivery claim.
      throw new Error('interaction request failed; delivery may be uncertain');
    }
  };
}

// One instance belongs to one native connection lifetime. A bridge restart must
// not reconstruct liveness from persisted conversation history.
export class InteractionBridge {
  constructor({ role, provider, session, epoch, call, persist, write, pending }) {
    if (![role, session, epoch].every(x => typeof x === 'string' && x.length > 0 && x.length <= 256) || !['codex', 'claude-code'].includes(provider)) throw new Error('invalid native binding');
    if (![call, persist, write, pending].every(x => typeof x === 'function')) throw new Error('missing bridge boundary');
    this.binding = { role, provider, session_id: session, epoch };
    this.call = call; this.persist = persist; this.write = write; this.pending = pending;
    this.requests = new Map();
  }
  async observe(native) {
    const id = this.binding.provider === 'codex' ? native.id : native.request_id;
    if (!(typeof id === 'string' && id.length > 0 || Number.isSafeInteger(id))) throw new Error('invalid native request ID');
    const key = JSON.stringify([typeof id, id]);
    const raw = JSON.stringify(native);
    const old = this.requests.get(key);
    if (old) {
      if (old.raw !== raw) throw new Error('native request identity changed');
      return old;
    }
    if (this.requests.size >= 32 || Buffer.byteLength(raw) > 65536) throw new Error('native question capacity exceeded');
    const entry = { id, raw, native: JSON.parse(raw), state: 'observing' };
    this.requests.set(key, entry);
    const result = await this.call({ ...this.binding, operation: 'observe', native_request: native });
    if (!result?.receipt?.interaction_id || result.receipt.state !== 'pending') throw new Error('native observation requires recovery');
    entry.interaction = result.receipt.interaction_id;
    entry.state = 'pending';
    return entry;
  }
  async check(entry) {
    if (entry.state !== 'pending' || !this.pending(entry.id)) return;
    const status = await this.call({ ...this.binding, operation: 'observe', native_request: entry.native });
    if (status?.receipt?.interaction_id !== entry.interaction) throw new Error('interaction identity changed');
    if (status.receipt.state === 'pending') return;
    if (status.receipt.state !== 'accepted') throw new Error('interaction requires recovery');
    if (entry.state !== 'pending') return;
    // Fence locally before any awaited call: concurrent checks cannot claim twice.
    entry.state = 'claiming';
    await this.persist({ ...this.binding, interaction_id: entry.interaction, state: 'delivery_attempted' });
    if (!this.pending(entry.id)) throw new Error('native request no longer pending');
    const delivered = await this.call({ ...this.binding, operation: 'deliver', interaction_id: entry.interaction, native_request: entry.native });
    if (delivered?.receipt?.interaction_id !== entry.interaction || delivered.receipt.state !== 'sending' || !delivered.native_response || !this.pending(entry.id)) throw new Error('native delivery requires recovery');
    const responseID = this.binding.provider === 'codex' ? delivered.native_response.id : delivered.native_response.response?.request_id;
    if (responseID !== entry.id) throw new Error('native response identity changed');
    await this.write(delivered.native_response);
    entry.state = 'sent';
  }
  async resolved(entry) {
    entry.state = 'resolved';
    return this.call({ ...this.binding, operation: 'resolve', interaction_id: entry.interaction, native_id: entry.id });
  }
}
