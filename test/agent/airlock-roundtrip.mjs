// Run from an explicitly supplied private Airlock checkout at the qualified commit.
// Go overlay adds only this test; neither checkout is mutated.
import {mkdtemp,writeFile,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join,resolve} from 'node:path';
import {fileURLToPath} from 'node:url';
import {spawnSync} from 'node:child_process';
const cwd=resolve(process.env.AIRLOCK_CHECKOUT||'');
const revision='568d21726675b9ecc0cd02b9ff86b279df9db62a';
if(!process.env.AIRLOCK_CHECKOUT||spawnSync('git',['rev-parse','HEAD'],{cwd,encoding:'utf8'}).stdout?.trim()!==revision)throw new Error('Supply Airlock #282 qualified checkout');
const dir=await mkdtemp(join(tmpdir(),'airlock-bridge-overlay-'));
try {
 const overlay=join(dir,'overlay.json');
 await writeFile(overlay,JSON.stringify({Replace:{[join(cwd,'internal/interactionapi/actions_bridge_test.go')]:fileURLToPath(new URL('./airlock-bridge_test.go',import.meta.url))}}));
 const result=spawnSync('go',['test','-count=1','-timeout=90s','-overlay',overlay,'./internal/interactionapi','-run','^TestActionsBridgeRoundTrip$','-v'],{cwd,stdio:'inherit',env:{...process.env,AIRLOCK_BRIDGE_DRIVER:fileURLToPath(new URL('./airlock-bridge-driver.mjs',import.meta.url))}});
 if(result.error)throw result.error;
 process.exitCode=result.status??1;
}finally{await rm(dir,{recursive:true,force:true});}
