// Optional interaction check: RIPE_PYTHON=.venv/bin/python RIPE_JSDOM=/path/to/jsdom node tests/test_progress_ui.cjs
const fs=require('fs'),path=require('path'),assert=require('assert'),{execFileSync}=require('child_process');
const {JSDOM}=require(process.env.RIPE_JSDOM||'jsdom');
const root=path.resolve(__dirname,'..');
const html=JSON.parse(execFileSync(process.env.RIPE_PYTHON||'python3',['-c',`
import json,tempfile
from pathlib import Path
from fastapi.testclient import TestClient
from ripe.app import create_app
from ripe.config import Runtime
with tempfile.TemporaryDirectory() as temp:
 with TestClient(create_app(Runtime(config_dir=Path(temp),ha_url='',ha_token=None,disable_scheduler=True)),base_url='http://localhost') as client:
  print(json.dumps(client.get('/').text))
`],{cwd:root,encoding:'utf8'}));
const tick=(ms=40)=>new Promise(resolve=>setTimeout(resolve,ms));
const idle={running:false,phase:'done',started_at:null,finished_at:null,sources:{done:0,total:0},releases:{done:0,total:0,skipped:0},current:'',eta_seconds:null,last_error:null,errors:[]};
const running=extra=>({...idle,running:true,phase:'analysing',started_at:'2026-01-01T00:00:00+00:00',sources:{done:30,total:30},releases:{done:43,total:100,skipped:5},current:'jellyfin',eta_seconds:360,...extra});
async function boot(initial) {
 const dom=new JSDOM(html,{url:'http://localhost/',runScripts:'outside-only',pretendToBeVisual:true}),w=dom.window,d=w.document,errors=[];
 const ctx={dom,w,d,errors,status:initial,posts:0,dashboards:0};
 w.matchMedia=()=>({matches:false});w.scrollTo=()=>{};w.addEventListener('error',e=>errors.push(e.error));
 // Compress the 3 s fade and the 2 s / 30 s refresh timers so the test stays fast.
 const real=w.setTimeout.bind(w);w.setTimeout=(f,ms,...a)=>real(f,ms>=2000?ms/100:ms,...a);
 w.fetch=async (url,options={})=>{
  if(url==='/api/poll/status')return {ok:true,json:async()=>ctx.status};
  if(url==='/api/poll'&&options.method==='POST'){ctx.posts++;ctx.status=running({phase:'collecting',sources:{done:0,total:0},releases:{done:0,total:0,skipped:0},current:'',eta_seconds:null});return {ok:true,status:202,json:async()=>({status:'started'})};}
  if(url==='/api/dashboard'){ctx.dashboards++;return {ok:true,json:async()=>({apps:[],summary:{},timezone:'UTC',last_poll:'2026-01-01T00:00:00+00:00'})};}
  throw new Error('Unexpected API: '+url);
 };
 w.eval(fs.readFileSync(path.join(root,'ripe/static/app.js'),'utf8'));await tick();
 ctx.set=async status=>{ctx.status=status;d.dispatchEvent(new w.Event('visibilitychange'));await tick();};
 return ctx;
}
(async()=>{
 // Idle: nothing shown, button usable.
 let c=await boot(idle);let {d}=c;
 const strip=d.querySelector('#poll-strip'),button=d.querySelector('#poll-button'),line=d.querySelector('#poll-line'),fill=d.querySelector('#poll-bar-fill'),bar=d.querySelector('#poll-bar');
 assert.equal(strip.hidden,true,'idle: strip hidden');
 assert.equal(button.disabled,false);assert.equal(button.textContent.trim(),'Check for updates');assert.equal(button.hasAttribute('aria-busy'),false);

 // Running with unknown totals: indeterminate shimmer, no percentage.
 await c.set(running({phase:'collecting',sources:{done:0,total:0},releases:{done:0,total:0,skipped:0},current:'',eta_seconds:null}));
 assert.equal(strip.hidden,false);assert.equal(strip.dataset.state,'running');
 assert(bar.classList.contains('indeterminate'),'indeterminate until totals are known');
 assert.equal(button.textContent.trim(),'Checking…');

 // Collecting counts sources.
 await c.set(running({phase:'collecting',sources:{done:3,total:12},releases:{done:0,total:0,skipped:0},current:'Docker',eta_seconds:null}));
 assert.equal(bar.classList.contains('indeterminate'),false);assert.equal(fill.style.width,'25%');
 assert.equal(line.textContent,'Collecting sources · 3 of 12 · Docker');

 // Analysing counts releases and shows the ETA.
 await c.set(running());
 assert.equal(button.disabled,true);assert.equal(button.getAttribute('aria-disabled'),'true');assert.equal(button.getAttribute('aria-busy'),'true');
 assert.equal(button.textContent.trim(),'Checking… 43%');assert(button.querySelector('.spinner'));
 assert.equal(fill.style.width,'43%');assert.equal(bar.getAttribute('aria-valuenow'),'43');
 assert.equal(line.textContent,'Analysing releases · 43 of 100 · jellyfin · about 6 min left');
 await c.set(running({eta_seconds:null,current:'<b>x</b>'}));
 assert.equal(line.textContent,'Analysing releases · 43 of 100 · <b>x</b>','No ETA before three model calls');
 assert.equal(line.querySelector?.('b'),null);

 // Panel: per-phase counts, current app, skipped, errors (escaped).
 await c.set(running({errors:[{source:'docker',message:'<img src=x onerror=alert(1)>'}]}));
 d.querySelector('#poll-strip-toggle').click();
 const panel=d.querySelector('#poll-panel');assert.equal(panel.hidden,false);
 assert.equal(d.querySelector('#poll-strip-toggle').getAttribute('aria-expanded'),'true');
 assert(/30 of 30/.test(panel.textContent)&&/43 of 100/.test(panel.textContent)&&/5 \(/.test(panel.textContent)&&/jellyfin/.test(panel.textContent));
 assert(panel.textContent.includes('<img src=x onerror=alert(1)>'));assert.equal(panel.querySelector('img'),null);
 d.querySelector('#poll-strip-toggle').click();assert.equal(panel.hidden,true);

 // Finished: bar fills, button returns, dashboard refreshes, strip fades away.
 const before=c.dashboards;
 await c.set(idle);
 assert.equal(strip.dataset.state,'done');assert.equal(fill.style.width,'100%');
 assert.equal(button.disabled,false);assert.equal(button.textContent.trim(),'Check for updates');assert.equal(button.hasAttribute('aria-busy'),false);
 assert(c.dashboards>before,'Last checked reloads when the check ends');
 await tick(800);assert.equal(strip.hidden,true,'strip fades out after the check');

 // Clicking Check starts one check and the button cannot start a second.
 c.dashboards=0;await tick();
 button.click();await tick(100);
 assert.equal(c.posts,1);assert.equal(button.disabled,true);assert.equal(button.getAttribute('aria-busy'),'true');
 button.click();await tick();assert.equal(c.posts,1,'No second check while one is running');
 c.dom.window.close();

 // Failed: warning state with reason and a link to the logs.
 c=await boot(running());({d}=c);
 await c.set({...idle,finished_at:'2026-01-01T00:05:00+00:00',last_error:'Collection failed (RuntimeError); last known records preserved.'});
 const s2=d.querySelector('#poll-strip');
 assert.equal(s2.hidden,false);assert.equal(s2.dataset.state,'failed');
 assert.equal(d.querySelector('#poll-line').textContent,'Check stopped: Collection failed (RuntimeError); last known records preserved.');
 assert.equal(d.querySelector('#poll-logs-link').hidden,false);assert.equal(d.querySelector('#poll-logs-link').getAttribute('href'),'/logs');
 assert.equal(d.querySelector('#poll-button').disabled,false);
 await tick(800);assert.equal(s2.hidden,false,'A failure stays visible until dismissed');
 d.querySelector('#poll-dismiss').click();assert.equal(s2.hidden,true);
 assert.equal(c.errors.length,0);c.dom.window.close();

 // Reload in the middle of a check shows the progress at once.
 c=await boot(running());({d}=c);
 assert.equal(d.querySelector('#poll-strip').hidden,false);
 assert.equal(d.querySelector('#poll-line').textContent,'Analysing releases · 43 of 100 · jellyfin · about 6 min left');
 assert.equal(d.querySelector('#poll-button').getAttribute('aria-busy'),'true');
 assert.equal(c.errors.length,0);c.dom.window.close();
 console.log('Ripe progress UI passed: idle, indeterminate, collecting, analysing, panel, done, failed and reload states.');
})().catch(error=>{console.error(error);process.exitCode=1;}).finally(()=>process.exit());
