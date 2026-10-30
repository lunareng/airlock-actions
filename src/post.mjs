// The action's post step (post-if: always()): the run hands its token back on success, failure and cancel alike.
// It reads what the main step left in its state, so a run the main step never granted has nothing to hand back.
// It never fails the job.
import { handBack } from './airlock.mjs';

const state = (name) => process.env[`STATE_${name}`] ?? '';
await handBack({ origin: state('origin'), role: state('role'), token: state('token'), readToken: state('read_token') });
