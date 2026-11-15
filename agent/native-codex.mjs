import { StringDecoder } from 'node:string_decoder';
import { randomUUID } from 'node:crypto';
import { resolve, join, relative, isAbsolute } from 'node:path';
import { realpath } from 'node:fs/promises';
import { InteractionBridge } from './interactions.mjs';

// The pinned app-server protocol over private stdio, never a listening socket.
export async function nativeCodex(child, { plan, cwd, home, apiKey, call, persist, pollMs = 2000 }) {
  let sequence = 0, buffer = '', stopped = false, thread, bridge, result = '';
  const replies = new Map(), pending = new Map(), operations = new Set(), writes = new Set();
  let fatal;
  const decoder = new StringDecoder('utf8');
  let finish, fail;
  const completed = new Promise((resolve, reject) => { finish = resolve; fail = reject; });
  // Observe early process failures even while initialization is awaiting an RPC.
  completed.catch(() => {});
  const stop = () => {
    if (stopped) return;
    stopped = true;
    const error = new Error('native interaction stopped; inspect retained Airlock state');
    fatal = error;
    for (const request of replies.values()) request.reject(error);
    replies.clear();
    for (const reject of writes) reject(error);
    fail(error); child.kill('SIGTERM');
  };
  const write = message => new Promise((resolve, reject) => {
    if (stopped) return reject(new Error('native connection closed'));
    const settle = error => {
      clearTimeout(timer); writes.delete(settle);
      if (error) reject(new Error('native write uncertain')); else resolve();
    };
    const timer = setTimeout(() => { settle(new Error()); stop(); }, 15000);
    writes.add(settle);
    try { child.stdin.write(JSON.stringify(message) + '\n', settle); }
    catch { settle(new Error()); stop(); }
  });
  const rpc = (method, params) => {
    if (stopped) return Promise.reject(new Error('native connection closed'));
    const id = `airlock-${++sequence}`;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(stop, 15000);
      replies.set(id, { resolve: value => { clearTimeout(timer); resolve(value); }, reject: error => { clearTimeout(timer); reject(error); } });
      write({ id, method, params }).catch(stop);
    });
  };
  const receive = async message => {
    if (!message.method && replies.has(message.id)) {
      const request = replies.get(message.id); replies.delete(message.id);
      if (message.error) request.reject(new Error('native RPC refused'));
      else request.resolve(message.result);
      return;
    }
    if (message.method === 'item/tool/requestUserInput') {
      if (!bridge || message.params?.threadId !== thread) throw new Error('unbound native question');
      const key = JSON.stringify(message.id);
      if (pending.size >= 32) throw new Error('native question capacity exceeded');
      if (pending.has(key)) throw new Error('duplicate native request');
      const entry = { live: true, interaction: null };
      pending.set(key, entry);
      entry.interaction = await bridge.observe(message);
      if (!entry.live) await bridge.resolved(entry.interaction);
      return;
    }
    if (message.method === 'serverRequest/resolved') {
      if (message.params?.threadId !== thread) throw new Error('unbound native resolution');
      const entry = pending.get(JSON.stringify(message.params.requestId));
      if (entry) { entry.live = false; if (entry.interaction) await bridge.resolved(entry.interaction); }
      return;
    }
    // No generic approval handler: native tool/sandbox permission requests cannot
    // be answered through a question response or silently approved here.
    if (message.method && Object.hasOwn(message, 'id')) throw new Error('unsupported native request');
    if (message.method === 'item/completed' && message.params?.threadId === thread && message.params.item?.type === 'agentMessage') {
      result = message.params.item.text || '';
      if (result.length > 65536) throw new Error('native result too large');
    }
    if (message.method === 'turn/completed' && message.params?.threadId === thread) {
      if (message.params.turn?.status !== 'completed') throw new Error('native turn failed');
      finish(result);
    }
  };
  child.stdout.on('data', chunk => {
    buffer += typeof chunk === 'string' ? chunk : decoder.write(chunk);
    if (Buffer.byteLength(buffer) > 262144) return stop();
    let end;
    while ((end = buffer.indexOf('\n')) >= 0) {
      const line = buffer.slice(0, end); buffer = buffer.slice(end + 1);
      if (!line.trim()) continue;
      try {
        const operation = receive(JSON.parse(line));
        operations.add(operation);
        operation.catch(stop).finally(() => operations.delete(operation));
      } catch { stop(); }
    }
  });
  child.on('error', stop); child.on('close', stop); child.stdin.on('error', stop);
  let polling = false;
  const timer = setInterval(async () => {
    if (polling || stopped || !bridge) return;
    polling = true;
    try { for (const entry of pending.values()) if (entry.live && entry.interaction) await bridge.check(entry.interaction); }
    catch { stop(); }
    finally { polling = false; }
  }, pollMs);
  try {
    await rpc('initialize', { clientInfo: { name: 'airlock', version: '1' }, capabilities: { experimentalApi: true } });
    await write({ method: 'initialized' });
    if (plan.auth === 'api-key') {
      if (typeof apiKey !== 'string' || !apiKey) throw new Error('native API credential unavailable');
      await rpc('account/login/start', { type: 'apiKey', apiKey });
    }
    const account = await rpc('account/read', { refreshToken: false });
    if (account.account?.type !== (plan.auth === 'subscription' ? 'chatgpt' : 'apiKey')) throw new Error('native authentication mode not verified');
    // Trust only the action's one bundled hook, outside the writable workspace.
    // Use native discovery and config APIs; never trust hooks from the checkout.
    const canonicalHome = await realpath(home);
    const homeFromWorkspace = relative(await realpath(cwd), canonicalHome);
    if (!homeFromWorkspace.startsWith('..' + '/') && !isAbsolute(homeFromWorkspace)) throw new Error('native policy home must be outside workspace');
    const quote = s => `'${s.replaceAll("'", "'\\''")}'`;
    const expectedCommand = `python3 ${quote(join(home, 'guard.py'))} ${quote(home)}`;
    const qualifyHook = response => {
      if (response.data?.length !== 1) throw new Error('native hook scope not verified');
      const entry = response.data[0];
      if (entry.errors?.length || entry.warnings?.length || entry.hooks?.length !== 1) throw new Error('native hook set not verified');
      const hook = entry.hooks[0];
      if (hook.sourcePath !== join(canonicalHome, 'config.toml') || hook.source !== 'user' ||
          hook.eventName !== 'preToolUse' || hook.handlerType !== 'command' || hook.command !== expectedCommand ||
          hook.matcher !== '.*' || hook.timeoutSec !== 10 || hook.async || !hook.enabled || hook.isManaged ||
          hook.pluginId != null || hook.environmentId != null || !/^sha256:[a-f0-9]{64}$/.test(hook.currentHash)) {
        throw new Error('native policy hook not verified');
      }
      return hook;
    };
    const declaredHook = qualifyHook(await rpc('hooks/list', { cwds: [cwd] }));
    await rpc('config/value/write', { keyPath: 'hooks.state', value: { [declaredHook.key]: { enabled: true, trusted_hash: declaredHook.currentHash } }, mergeStrategy: 'replace' });
    const started = await rpc('thread/start', { model: plan.model, cwd, ephemeral: true, approvalPolicy: 'never', sandbox: 'workspace-write', environments: [{ environmentId: 'local', cwd, runtimeWorkspaceRoots: [cwd] }] });
    const roots = new Set([resolve(cwd), ...(plan.writeGitDirs || []).map(path => resolve(cwd, path))]);
    if (started.approvalPolicy !== 'never' || started.cwd !== resolve(cwd) || started.sandbox?.type !== 'workspaceWrite' ||
        started.sandbox.networkAccess !== true || started.sandbox.excludeTmpdirEnvVar !== true || started.sandbox.excludeSlashTmp !== true ||
        !Array.isArray(started.sandbox.writableRoots) || started.sandbox.writableRoots.some(path => !roots.has(path))) {
      throw new Error('native runtime did not preserve run sandbox');
    }
    const environments = started.thread?.environments;
    if (!Array.isArray(environments) || environments.length !== 1 || environments[0].environmentId !== 'local' ||
        environments[0].cwd !== resolve(cwd) || JSON.stringify(environments[0].runtimeWorkspaceRoots) !== JSON.stringify([resolve(cwd)])) throw new Error('native environment binding not verified');
    thread = started?.thread?.id;
    const activeHook = qualifyHook(await rpc('hooks/list', { threadId: thread }));
    if (activeHook.currentHash !== declaredHook.currentHash || activeHook.trustStatus !== 'trusted') throw new Error('native policy hook is not trusted');
    bridge = new InteractionBridge({ role: plan.role, provider: 'codex', session: thread, epoch: randomUUID(), call, persist, write, pending: id => !stopped && pending.get(JSON.stringify(id))?.live === true });
    await rpc('turn/start', { threadId: thread, input: [{ type: 'text', text: plan.prompt }] });
    const output = await completed;
    // Native closure and durable acknowledgement can race the turn notification.
    while (operations.size) await Promise.allSettled([...operations]);
    if (fatal) throw fatal;
    return output;
  } finally {
    clearInterval(timer);
    stopped = true;
    child.kill('SIGTERM');
    const forcedExit = setTimeout(() => child.kill('SIGKILL'), 2000);
    forcedExit.unref();
    child.once('close', () => clearTimeout(forcedExit));
    for (const reject of writes) reject(new Error('native connection ended'));
    for (const request of replies.values()) request.reject(new Error('native connection ended'));
  }
}
