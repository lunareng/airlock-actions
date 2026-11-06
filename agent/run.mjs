import { spawn } from 'node:child_process';
import { mkdtemp, writeFile, readFile, rm, copyFile, chmod } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { join, resolve, dirname } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { brokerClient, leaseAuth } from './auth.mjs';

const here = dirname(fileURLToPath(import.meta.url));
const toml = JSON.stringify;
const quote = s => `'${s.replaceAll("'", "'\\''")}'`;

export function codexConfig(plan, home, workspace) {
  if ((plan.nativeTools || []).length > 0) throw new Error('Codex mapping for native WebFetch/WebSearch/Agent tools is not yet supported; select Claude or revise the role in policy');
  if ((plan.writeGitDirs || []).some(p => typeof p !== 'string' || !/^repos\/[A-Za-z0-9._-]+\/\.git$/.test(p))) throw new Error('invalid Git writable directory');
  const lines = [
    'approval_policy = "never"', 'sandbox_mode = "workspace-write"', 'allow_login_shell = false',
    `forced_login_method = ${toml(plan.auth === 'subscription' ? 'chatgpt' : 'api')}`,
    'cli_auth_credentials_store = "file"', 'web_search = "disabled"',
    '[features]', 'hooks = true', 'unified_exec = false', 'multi_agent = false',
    'apps = false', 'plugins = false', 'computer_use = false', 'browser_use = false',
    'browser_use_external = false', 'in_app_browser = false', 'browser_use_full_cdp_access = false',
    'skill_mcp_dependency_install = false', 'skill_search = false', 'workspace_dependencies = false',
    'image_generation = false', 'code_mode = false', 'code_mode_host = false',
    '[sandbox_workspace_write]', `writable_roots = ${toml((plan.writeGitDirs || []).map(p => resolve(workspace, p)))}`, 'network_access = true', 'exclude_tmpdir_env_var = true', 'exclude_slash_tmp = true',
    `[projects.${toml(workspace)}]`, 'trust_level = "untrusted"',
    '[shell_environment_policy]', 'inherit = "all"',
    'exclude = ["CODEX_*", "OPENAI_*", "ANTHROPIC_*", "CLAUDE_*", "ACTIONS_*", "ROLE_MCP_*", "RIPGREP_CONFIG_PATH"]',
    '[[hooks.PreToolUse]]', 'matcher = ".*"', '[[hooks.PreToolUse.hooks]]', 'type = "command"',
    `command = ${toml(`python3 ${quote(join(home, 'guard.py'))} ${quote(home)}`)}`, 'timeout = 10',
  ];
  // Exact MCP tool names are passed to Codex. Globs stay in the guard, because enabled_tools
  // does not expand them. The upstream receives the same per-run scoped session as Claude.
  for (const [name, spec] of Object.entries(plan.mcp || {})) {
    if (!/^[a-z0-9][a-z0-9-]{0,39}$/.test(name)) throw new Error('invalid MCP connector');
    const url = new URL(spec.url);
    if (url.protocol !== 'https:' || url.username || url.password) throw new Error('invalid MCP endpoint');
    const variable = `ROLE_MCP_${name.toUpperCase().replaceAll('-', '_')}_TOKEN`;
    lines.push(`[mcp_servers.${toml(name)}]`, `url = ${toml(spec.url)}`, 'required = true');
    const tools = plan.tools?.[name] || [];
    if (!tools.length) throw new Error(`MCP ${name} has no allowed tools`);
    if (tools.every(t => !t.includes('*'))) lines.push(`enabled_tools = ${toml(tools)}`);
    const header = spec.header || 'Authorization';
    if (header === 'Authorization') lines.push(`bearer_token_env_var = ${toml(variable)}`);
    else {
      if (!/^[A-Za-z0-9-]+$/.test(header)) throw new Error('invalid MCP header');
      lines.push(`env_http_headers = { ${toml(header)} = ${toml(variable)} }`);
    }
  }
  return `${lines.join('\n')}\n`;
}

export function agentEnvironment(plan, env, home) {
  const next = { ...env };
  for (const key of Object.keys(next)) if (/^(CLAUDE_|ANTHROPIC_|OPENAI_|CODEX_|ACTIONS_)/.test(key)) delete next[key];
  delete next.RIPGREP_CONFIG_PATH;
  if (plan.provider === 'claude') {
    const key = plan.auth === 'subscription' ? 'CLAUDE_CODE_OAUTH_TOKEN' : 'ANTHROPIC_API_KEY';
    if (!env[key]) throw new Error(`selected auth requires ${key}`);
    next[key] = env[key]; next.CLAUDE_CONFIG_DIR = home;
  } else {
    next.CODEX_HOME = home;
    if (plan.auth === 'api-key') {
      if (!env.OPENAI_API_KEY) throw new Error('selected auth requires OPENAI_API_KEY');
      next.CODEX_API_KEY = env.OPENAI_API_KEY;
    }
  }
  return next;
}

export async function execute(plan, { env = process.env, cwd = process.cwd(), start = spawn, broker = brokerClient } = {}) {
  if (!['claude', 'codex'].includes(plan.provider) || !['subscription', 'api-key'].includes(plan.auth)) throw new Error('invalid selected provider/auth');
  if (!Number.isInteger(plan.maxTurns) || plan.maxTurns < 1 || plan.maxTurns > 10000) throw new Error('invalid tool budget');
  const root = await mkdtemp(join(env.RUNNER_TEMP || '/tmp', 'airlock-agent-'));
  await chmod(root, 0o700);
  const secrets = new Set(Object.entries(env).filter(([k,v]) => v && /TOKEN|SECRET|API_KEY/.test(k)).map(([,v]) => v));
  const keepAuthSecrets = auth => Object.values(auth?.tokens || {}).forEach(v => { if (typeof v === 'string' && v) secrets.add(v); });
  const redact = text => [...secrets].sort((a,b) => b.length-a.length).reduce((s, key) => s.replaceAll(key, '[REDACTED]'), text);
  let lease, stdout = '', stderr = '', failure, result = '', child, signalHandler;
  try {
    const childEnv = agentEnvironment(plan, env, root);
    if (plan.provider === 'codex') {
      await writeFile(join(root, 'config.toml'), codexConfig(plan, root, resolve(cwd)), { mode: 0o600 });
      await copyFile(join(here, 'guard.py'), join(root, 'guard.py'));
      const mcp = Object.entries(plan.tools || {}).flatMap(([c,ts]) => ts.map(t => `mcp__${c}__${t}`));
      await writeFile(join(root, 'guard-policy.json'), JSON.stringify({ bash: plan.bash || [], edit: plan.edit, mcp, maxTools: plan.maxTurns }), { mode: 0o600 });
      if (plan.auth === 'subscription') {
        if (!env.RUNNER_AUTH_URL) throw new Error('Codex subscription requires runner-auth-url with refreshed credential storage (infra#109)');
        lease = await leaseAuth(root, broker(env.RUNNER_AUTH_URL, env));
        keepAuthSecrets(lease.auth);
      }
    }
    const args = plan.provider === 'claude'
      ? ['-p', plan.prompt, '--model', plan.model, '--max-turns', `${plan.maxTurns}`, '--mcp-config', plan.mcpConfig, '--output-format', 'json', '--allowedTools', plan.allowed]
      // Hook sources are bundled with this pinned action, copied outside the writable checkout.
      // Only hook trust is bypassed for these vetted hooks; approvals and sandbox remain enforced.
      : ['exec', '--strict-config', '--dangerously-bypass-hook-trust', '--ephemeral', '--json', '--color', 'never', '--model', plan.model, '--output-last-message', join(root, 'last.txt'), '-'];
    child = start(plan.provider, args, { env: childEnv, cwd, stdio: ['pipe', 'pipe', 'pipe'] });
    const stop = () => { child.kill('SIGTERM'); setTimeout(() => child.kill('SIGKILL'), 2000).unref(); };
    signalHandler = () => { failure = new Error('agent interrupted'); stop(); };
    process.on('SIGINT', signalHandler); process.on('SIGTERM', signalHandler);
    // Leave five minutes for saving/revoking within the 55-minute credential lease.
    const timer = setTimeout(() => { failure = new Error('agent exceeded 50-minute execution budget'); stop(); }, 3000000);
    const budget = setInterval(() => { if (existsSync(join(root, 'budget-exhausted'))) { failure = new Error('Codex tool-call budget exceeded'); stop(); } }, 100);
    child.stdout.on('data', chunk => { stdout += chunk; if (stdout.length > 16_000_000) { failure = new Error('agent output limit exceeded'); stop(); } });
    child.stderr.on('data', chunk => { stderr = (stderr + chunk).slice(-1_000_000); });
    child.stdin.on('error', () => {});
    child.stdin.end(plan.provider === 'codex' ? plan.prompt : undefined);
    let status;
    try { status = await new Promise((ok, bad) => { child.on('error', bad); child.on('close', ok); }); }
    finally { clearTimeout(timer); clearInterval(budget); }
    if (existsSync(join(root, 'budget-exhausted'))) throw new Error('Codex tool-call budget exceeded');
    if (status !== 0) throw new Error(`agent exited with status ${status}`);
    if (failure) throw failure;
    if (plan.provider === 'claude') {
      const answer = JSON.parse(stdout);
      if (answer.is_error) throw new Error('Claude reported an unsuccessful run');
      result = answer.result || '';
    } else {
      const events = stdout.trim().split('\n').filter(Boolean).map(line => JSON.parse(line));
      if (!events.some(e => e.type === 'turn.completed') || events.some(e => ['turn.failed', 'error'].includes(e.type))) throw new Error('Codex did not complete successfully');
      result = await readFile(join(root, 'last.txt'), 'utf8');
    }
  } catch (e) { failure = e; }
  finally {
    if (signalHandler) { process.off('SIGINT', signalHandler); process.off('SIGTERM', signalHandler); }
    if (lease) {
      try {
        // Redact newly refreshed tokens as well as the seed. Cleanup runs on provider failure too.
        try { keepAuthSecrets(JSON.parse(await readFile(join(root, 'auth.json'), 'utf8'))); } catch {}
        await lease.save();
      } catch { failure = new Error('Codex credential write-back failed; lease retained until expiry; reseed may be needed'); }
    }
    await rm(root, { recursive: true, force: true });
  }
  const summary = `Provider: ${plan.provider}\nAuthentication: ${plan.auth}\nModel: ${plan.model}\n${plan.provider === 'codex' ? `Tool-call cap: ${plan.maxTurns}\n` : ''}\n${result}${failure ? `\n\nRun failed: ${failure.message}\n${stderr.slice(-4000)}` : ''}`;
  await writeFile(join(cwd, plan.provider === 'claude' ? 'run.json' : 'run.jsonl'), redact(stdout));
  await writeFile(join(cwd, 'result.txt'), redact(summary));
  if (env.GITHUB_STEP_SUMMARY) await writeFile(env.GITHUB_STEP_SUMMARY, redact(`${summary}\n${failure ? `\nRun failed: ${failure.message}\n` : ''}`), { flag: 'a' });
  if (failure) throw new Error(redact(failure.message));
  return { provider: plan.provider, auth: plan.auth, result: redact(result), stderr: redact(stderr) };
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  try { await execute(JSON.parse(await readFile(process.env.PLAN_PATH, 'utf8'))); }
  catch (e) { console.error(`Airlock: ${e.message}`); process.exitCode = 1; }
}
