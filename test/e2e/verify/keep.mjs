// The main step only keeps where the stand-in's log is, for the post step (check.mjs), which does the work.
import { appendFileSync } from 'node:fs';

appendFileSync(process.env.GITHUB_STATE, `log=${process.env.INPUT_LOG}\n`);
