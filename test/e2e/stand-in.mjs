// A stand-in Airlock on 127.0.0.1 for the hand-back job (.github/workflows/test.yml): it grants one made-up
// token and records each call, the token only as whether it came back. `node stand-in.mjs start <log>` starts it
// in the background and returns once it answers, so the job's next steps can call it.
import { spawn } from 'node:child_process';
import { createHash, randomBytes } from 'node:crypto';
import { appendFileSync, writeFileSync } from 'node:fs';
import { createServer } from 'node:http';

const [, , mode, log] = process.argv;
const PORT = 8787;

if (mode === 'start') {
  spawn(process.execPath, [new URL(import.meta.url).pathname, 'serve', log], { detached: true, stdio: 'ignore' }).unref();
  for (let i = 0; i < 100; i++) {
    try {
      await fetch(`http://127.0.0.1:${PORT}/health`);
      process.exit(0);
    } catch {
      await new Promise((r) => setTimeout(r, 100));
    }
  }
  console.log('::error::the stand-in Airlock did not start');
  process.exit(1);
}

const token = `ghs_E2E${randomBytes(18).toString('hex')}`;
const sha = (s) => createHash('sha256').update(String(s)).digest('hex');
writeFileSync(log, '');
createServer((req, res) => {
  let body = '';
  req.on('data', (c) => { body += c; });
  req.on('end', () => {
    if (req.url === '/health') return res.end('ok');
    let json = null;
    try { json = JSON.parse(body); } catch { /* recorded as no role */ }
    appendFileSync(log, `${JSON.stringify({
      path: req.url,
      action: req.headers['airlock-action'] ?? null,
      proof: sha(req.headers.authorization ?? ''),
      role: json?.role ?? null,
      handedBack: req.url === '/v1/revoke' ? json?.token === token : null,
    })}\n`);
    res.setHeader('content-type', 'application/json');
    if (req.url === '/v1/mint') {
      return res.end(JSON.stringify({ mode: 'live', decision: 'granted', run: '1.1', grant: { repositories: ['e2e'], permissions: { contents: 'read' } }, token, expiresAt: new Date(Date.now() + 3600e3).toISOString(), sessions: {} }));
    }
    if (req.url === '/v1/revoke') return res.end(JSON.stringify({ revoked: true, run: '1.1' }));
    res.statusCode = 404;
    return res.end('{}');
  });
}).listen(PORT, '127.0.0.1');
