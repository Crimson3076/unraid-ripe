// Optional interaction check: RIPE_PYTHON=.venv/bin/python RIPE_JSDOM=/path/to/jsdom node tests/test_app_page_ui.cjs
const fs=require('fs'),path=require('path'),assert=require('assert'),{execFileSync}=require('child_process');
const {JSDOM}=require(process.env.RIPE_JSDOM||'jsdom');
const root=path.resolve(__dirname,'..');
const html=JSON.parse(execFileSync(process.env.RIPE_PYTHON||'python3',['-c',`
import json,tempfile
from pathlib import Path
from fastapi.testclient import TestClient
from ripe.app import create_app
from ripe.config import Runtime
from ripe.models import AppRecord
with tempfile.TemporaryDirectory() as temp:
 with TestClient(create_app(Runtime(config_dir=Path(temp),ha_url='',ha_token=None,disable_scheduler=True)),base_url='http://localhost') as client:
  client.app.state.manager.store.save_app(AppRecord(id='docker:drone',name='DroneMobile',source='docker'))
  print(json.dumps(client.get('/apps/docker:drone').text))
`],{cwd:root,encoding:'utf8'}));
const tick=()=>new Promise(resolve=>setTimeout(resolve,20));
const SENSORS='Added new sensors and siren switches.',TOKEN='Token storage moved to the config entry.';
const release=(version,notes,extra)=>({id:'github:x:'+version,app_id:'a',version,published_at:'2026-06-01T00:00:00Z',notes,source_url:'',classification:null,applicability:[],...extra});
const classified=(summary,quote)=>({status:'ok',summary,summary_evidence:quote,tags:[],tag_evidence:{},security:[],breaking:[],migration:[],big_features:[],keyword_hits_addressed:[],keyword_warnings:[],confidence:'high',model:'m',provider:'local',prompt_version:'classification-v1'});
const rollupItem=(text,version,evidence)=>({text,source:'release',release:version,evidence});
async function render(view){
 const dom=new JSDOM(html,{url:'http://localhost/apps/docker%3Adrone',runScripts:'outside-only'}),w=dom.window;
 w.matchMedia=()=>({matches:false});
 w.fetch=async()=>({ok:true,json:async()=>view});
 w.eval(fs.readFileSync(path.join(root,'ripe/static/app.js'),'utf8'));await tick();await tick();
 return dom;
}
const base={app:{id:'docker:drone',name:'DroneMobile',source:'docker',installed_version:'2026.4.10',profile:{tier:'general'},detected:{}},status:'ripe',status_label:'Ripe',reason:'ok',tags:[],hard_signals:[],available_notes:[],latest_version:'2026.7.2',recommended_version:null};
(async()=>{
 // Pending: compact rows, rollup first, no-notes releases collapsed into one line.
 const pending=[release('2026.6.4',SENSORS),release('2026.6.5',TOKEN,{classification:classified('Token storage moved.',TOKEN)}),release('2026.6.6',''),release('2026.7.2','','')];
 const rollup={status:'ok',installed:'2026.4.10',latest:'2026.7.2',headline:'x',provider:'local',model:'m',prompt_version:'rollup-v1',groups:{
  action:[rollupItem('Token storage moved.','2026.6.5',TOKEN)],
  features:[rollupItem('New sensors and siren switches.','2026.6.4',SENSORS),rollupItem('Fabricated.','2026.6.4','Not in the notes')],
  under_the_hood:[{text:'PHP changes from 8.2.1 to 8.3.0 in the container image.',source:'image',release:'',evidence:'PHP_VERSION: 8.2.1 → 8.3.0'}]}};
 let dom=await render({...base,pending,history:[],no_notes:['2026.6.6','2026.7.2'],rollup,next_ripe_at:'2020-01-01T00:00:00Z'});
 let d=dom.window.document;
 assert.equal(d.querySelector('.rollup-card h2').textContent.trim(),'Since your version (2026.4.10)');
 assert.deepEqual([...d.querySelectorAll('.rollup-group h3')].map(x=>x.textContent),['What you need to do','New features','Under the hood'],'Empty groups are omitted');
 const bullets=[...d.querySelectorAll('.rollup-group li')].map(x=>x.querySelector('span').textContent);
 assert.deepEqual(bullets,['Token storage moved.','New sensors and siren switches.','PHP changes from 8.2.1 to 8.3.0 in the container image.'],'Bullets whose quote is not in the cited release are not shown');
 assert(d.querySelector('.rollup-group li .quote').textContent.includes(TOKEN),'Every bullet shows its evidence');
 assert.equal(d.querySelectorAll('.release-list .release-row').length,2,'Only releases with notes get a row');
 assert.equal(d.querySelector('.no-notes-line').textContent,'2026.6.6, 2026.7.2: no notes');
 const lines=[...d.querySelectorAll('.release-row-line')].map(x=>x.textContent);
 assert.deepEqual(lines,['New sensors and siren switches.','Token storage moved.'],'One-line summaries come from the rollup or the evidence-backed summary');
 assert(!d.querySelector('.release-row').open,'Rows are collapsed until expanded');
 assert(d.querySelector('.detail-reason').textContent.includes('Ripe since'),'Past ripening date reads "Ripe since"');
 dom.window.close();

 // Not yet ripe keeps "Next ripening".
 dom=await render({...base,pending:[],history:[],no_notes:[],rollup:null,next_ripe_at:'2999-01-01T00:00:00Z'});
 assert(dom.window.document.querySelector('.detail-reason').textContent.includes('Next ripening'));
 dom.window.close();

 // Wording: classification ran and found nothing.
 const nothing=release('2026.6.9','Docs only.',{classification:classified('','')});
 const unclassified=release('2026.6.10','Some notes.');
 dom=await render({...base,pending:[nothing,unclassified],history:[],no_notes:[],rollup:null});
 d=dom.window.document;
 const rows=[...d.querySelectorAll('.release-row')];
 assert.equal(rows[0].querySelector('.release-row-line').textContent,'Nothing notable found');
 assert(rows[0].querySelector('.release-summary').textContent.includes('Nothing notable found'));
 assert(!rows[0].querySelector('.model-meta').textContent.includes('confidence'),'No confidence line next to "nothing notable"');
 assert.equal(rows[1].querySelector('.release-row-line').textContent,'Not classified yet');
 assert(rows[1].querySelector('.release-summary').textContent.includes('classification available yet'));
 dom.window.close();

 // History: newest three shown, the rest behind "Show all", styled apart from pending.
 const history=['2026.1.1','2026.2.1','2026.3.1','2026.3.5','2026.4.10'].map(v=>release(v,'Old notes.'));
 dom=await render({...base,pending:[],history,no_notes:[],rollup:null});
 d=dom.window.document;
 const visible=[...d.querySelectorAll('#history .release-list > .release-row')].map(x=>x.querySelector('.release-row-version').textContent);
 assert.deepEqual(visible,['2026.4.10','2026.3.5','2026.3.1'],'Last three, newest first');
 const more=d.querySelector('#history .history-more');
 assert.equal(more.querySelector('summary').textContent,'Show all (5)');
 assert.deepEqual([...more.querySelectorAll('.release-row-version')].map(x=>x.textContent),['2026.2.1','2026.1.1']);
 assert(!more.open&&d.querySelectorAll('#history .release-row.history').length===5,'History rows carry the history style');
 dom.window.close();

 console.log('Ripe app page UI passed: rollup, compact rows, no-notes line, wording and collapsed history.');
})().catch(error=>{console.error(error);process.exitCode=1;});
