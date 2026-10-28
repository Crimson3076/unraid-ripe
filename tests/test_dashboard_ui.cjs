// Optional interaction check: RIPE_PYTHON=.venv/bin/python RIPE_JSDOM=/path/to/jsdom node tests/test_dashboard_ui.cjs
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
const tick=()=>new Promise(resolve=>setTimeout(resolve,20));
const view=(id,name,extra)=>({status:'up_to_date',status_label:'Up to date',reason:'ok',tags:[],pending:[],app:{id,name,source:'docker',profile:{tier:'general'},detected:{},...extra}});
(async()=>{
 const dom=new JSDOM(html,{url:'http://localhost/',runScripts:'outside-only'}),w=dom.window,d=w.document,errors=[];
 w.matchMedia=()=>({matches:false});w.scrollTo=()=>{};w.addEventListener('error',e=>errors.push(e.error));
 const hint='No upstream repository mapping; choose one in the app profile';
 const apps=[view('docker:mapped','Mapped',{upstream_repo:'a/b'}),view('docker:bare','Unmapped <img src=x onerror=alert(1)>',{detected:{mapping_hint:hint}}),view('docker:stale','Stale',{upstream_repo:'a/c',detected:{mapping_hint:hint}})];
 w.fetch=async url=>{if(url!=='/api/dashboard')throw new Error('Unexpected API: '+url);return {ok:true,json:async()=>({apps,summary:{},timezone:'UTC'})};};
 w.eval(fs.readFileSync(path.join(root,'ripe/static/app.js'),'utf8'));await tick();await tick();
 const cards=[...d.querySelectorAll('.app-card')],hintOf=id=>cards.find(c=>c.getAttribute('href')==='/apps/'+encodeURIComponent(id)).querySelector('.mapping-hint');
 assert.equal(cards.length,3);
 assert.equal(hintOf('docker:mapped'),null,'Mapped app has no hint');
 assert.equal(hintOf('docker:stale'),null,'Hint is hidden once a repo is mapped');
 const el=hintOf('docker:bare');assert(el);
 assert.equal(el.textContent.trim(),'No release notes source. Choose a GitHub repo in the app profile.');
 assert.equal(el.dataset.href,'/apps/docker%3Abare#profile');
 assert.equal(d.querySelector('.app-card img'),null,'Upstream/model text is escaped');
 const opened=[];w.open=(...a)=>opened.push(a[0]);
 el.dispatchEvent(new w.MouseEvent('click',{bubbles:true,cancelable:true,ctrlKey:true}));
 el.dispatchEvent(new w.MouseEvent('auxclick',{bubbles:true,cancelable:true,button:1}));
 assert.deepEqual(opened,['/apps/docker%3Abare#profile','/apps/docker%3Abare#profile'],'Modified and middle clicks open a new tab');
 d.querySelector('[data-filter="needs_mapping"]').click();
 assert.deepEqual([...d.querySelectorAll('.app-card h3')].map(x=>x.textContent),['Unmapped <img src=x onerror=alert(1)>']);
 assert.equal(errors.length,0);dom.window.close();
 console.log('Ripe dashboard UI passed: mapping hint for mapped vs unmapped apps, escaping and needs-mapping filter.');
})().catch(error=>{console.error(error);process.exitCode=1;});
