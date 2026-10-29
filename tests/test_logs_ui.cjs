// Optional interaction check: RIPE_PYTHON=.venv/bin/python RIPE_JSDOM=/path/to/jsdom node tests/test_logs_ui.cjs
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
  print(json.dumps(client.get('/logs').text))
`],{cwd:root,encoding:'utf8'}));
const calls=[
 {model:'gemini',outcome:'unavailable',error:'LLMUnavailable',evidence_passed:false},
 {model:'local',outcome:'error',error:'ReadTimeout',evidence_passed:false},
 {model:'legacy',status:'error',evidence_passed:false},
 {model:'invalid',outcome:'invalid_json',error:'ValidationError',evidence_passed:false},
 {model:'rejected',outcome:'ok',evidence_passed:false},
 {model:'checked',outcome:'ok',evidence_passed:true},
 {model:'alias',evidence_valid:false},
 {model:'unknown'}
];
(async()=>{
 const dom=new JSDOM(html,{url:'http://localhost/logs',runScripts:'outside-only'}),w=dom.window;
 w.matchMedia=()=>({matches:false});
 w.fetch=async url=>({ok:true,json:async()=>url==='/api/logs'?{llm:calls,errors:[]}:{}});
 w.eval(fs.readFileSync(path.join(root,'ripe/static/app.js'),'utf8'));
 await new Promise(resolve=>setTimeout(resolve,40));
 try {
 const d=w.document;
 const chips=[...d.querySelectorAll('.log-entry .status-chip')];
 assert.deepEqual(chips.map(x=>x.textContent),[
  'Call failed (LLMUnavailable)','Call failed (ReadTimeout)','Call failed',
  'Call failed (ValidationError)','Evidence rejected','Evidence checked',
  'Evidence rejected','View evidence results'
 ]);
 assert(chips.slice(0,4).every(x=>x.classList.contains('waiting')),'Failed calls use the amber warning style');
 assert(chips[4].classList.contains('read_first'),'Rejected evidence keeps rejection styling');
 assert(chips[5].classList.contains('ripe'),'Checked evidence keeps success styling');
 assert(d.querySelector('.log-summary').textContent.includes('4 failed calls · 2 evidence rejected'));
 } finally {dom.window.close();}
 console.log('Ripe logs UI passed: failed, rejected, checked and separate counts.');
})().catch(error=>{console.error(error);process.exitCode=1;});
