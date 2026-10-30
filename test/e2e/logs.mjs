// Reads the hand-back job's own log from GitHub once it has finished: the masked token shows as *** where the agent
// printed it, the stand-in's token never shows in the clear, and the hand-back was checked. Run by the logs job
// (.github/workflows/test.yml) with the job's GITHUB_TOKEN (actions: read).
const { GITHUB_API_URL: api, GITHUB_REPOSITORY: repo, GITHUB_RUN_ID: run, GITHUB_RUN_ATTEMPT: attempt, GH_TOKEN: token } = process.env;
const headers = { authorization: `Bearer ${token}`, accept: 'application/vnd.github+json' };
const get = async (path) => {
  const r = await fetch(`${api}/repos/${repo}${path}`, { headers });
  if (!r.ok) throw new Error(`${path}: ${r.status}`);
  return r;
};

const { jobs } = await (await get(`/actions/runs/${run}/attempts/${attempt}/jobs`)).json();
const job = jobs.find((j) => j.name === 'hand-back');
if (!job) { console.log('::error::no hand-back job in this run'); process.exit(1); }
// A finished job's log can take a few seconds to be served.
let text = '';
for (let i = 0; i < 10 && !text.includes('Airlock e2e:'); i++) {
  if (i) await new Promise((r) => setTimeout(r, 3000));
  text = await get(`/actions/jobs/${job.id}/logs`).then((r) => r.text(), () => '');
}
const problems = [];
if (!/the agent's token: \*\*\*/.test(text)) problems.push('the agent step\'s token did not show as ***');
if (/ghs_E2E[0-9a-f]{36}/.test(text)) problems.push('the token shows in the clear in the hand-back job\'s log');
if (!text.includes('Airlock e2e: the token was handed back')) problems.push('the hand-back job\'s check did not pass');
for (const p of problems) console.log(`::error::${p}`);
if (problems.length) process.exit(1);
console.log('Airlock e2e: in the hand-back job\'s log the token shows only as ***');
