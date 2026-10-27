// Optional interaction check: RIPE_PYTHON=.venv/bin/python RIPE_JSDOM=/path/to/jsdom node tests/test_settings_ui.cjs
const fs=require('fs'),path=require('path'),assert=require('assert'),{execFileSync}=require('child_process');
const {JSDOM}=require(process.env.RIPE_JSDOM||'jsdom');
const root=path.resolve(__dirname,'..');
const fixture=JSON.parse(execFileSync(process.env.RIPE_PYTHON||'python3',['-c',`
import json,tempfile
from pathlib import Path
from fastapi.testclient import TestClient
from ripe.app import create_app
from ripe.config import Runtime
with tempfile.TemporaryDirectory() as temp:
 with TestClient(create_app(Runtime(config_dir=Path(temp),ha_url='',ha_token=None,disable_scheduler=True)),base_url='http://localhost') as client:
  print(json.dumps({'html':client.get('/settings').text,'settings':client.get('/api/settings').json()}))
`],{cwd:root,encoding:'utf8'}));
const tick=()=>new Promise(resolve=>setTimeout(resolve,20));
async function browser(demo=false){
 const dom=new JSDOM(fixture.html,{url:'http://localhost/settings',runScripts:'outside-only'}),w=dom.window,requests=[],errors=[];
 w.matchMedia=()=>({matches:false});w.scrollTo=()=>{};w.addEventListener('error',event=>errors.push(event.error));
 const STATUS={app:'Set (from Settings)',environment:'Set (from environment)',none:'Not set'},creds={github:{source:'environment',env:true},gemini:{source:'none',env:false}};
 let settings={...fixture.settings,demo_mode:demo},connection=demo?{url:'',has_token:false,configured:false,source:'demo',demo:true}:{url:'http://ha.local:8123',has_token:true,configured:true,source:'environment',demo:false};
 w.fetch=async(url,options={})=>{
  const data=options.body?JSON.parse(options.body):null;requests.push({url,method:options.method||'GET',data});let result;
  if(url==='/api/settings'){if(options.method==='PATCH')settings={...settings,...data};result=settings;}
  else if(url==='/api/connections/home-assistant'){
   if(options.method==='PUT')connection={...connection,url:data.url,has_token:true,configured:true,source:'app'};
   if(options.method==='DELETE')connection={url:'',has_token:false,configured:false,source:'app',demo:false};
   result=connection;
  }else if(/^\/api\/connections\/(github|gemini)(\/test)?$/.test(url)){
   const name=url.split('/')[3],c=creds[name];
   if(url.endsWith('/test'))result={ok:true,message:'Test ok for '+name};
   else{
    if(options.method==='PUT'&&(data.token||data.api_key))c.source='app';
    if(options.method==='DELETE')c.source=c.env?'environment':'none';
    result=demo?{source:'demo',configured:false,status:'Not set',demo:true}:{source:c.source,configured:c.source!=='none',status:STATUS[c.source],demo:false};
   }
  }else if(url==='/api/connections/home-assistant/test')result={ok:true,message:'Home Assistant connection verified.'};
  else throw new Error('Unexpected API: '+url);
  return {ok:true,json:async()=>structuredClone(result)};
 };
 w.eval(fs.readFileSync(path.join(root,'ripe/static/app.js'),'utf8'));await tick();await tick();
 assert.equal(errors.length,0,errors.map(error=>error.stack).join('\n'));
 return {dom,w,d:w.document,requests,errors};
}
(async()=>{
 let {dom,w,d,requests,errors}=await browser();
 assert.equal(d.querySelector('#ha-token').type,'password');assert.equal(d.querySelector('#ha-token').value,'');
 assert(d.querySelector('#ha-connection-status').textContent.includes('deployment fallback'));
 assert(!d.querySelector('#settings-form').contains(d.querySelector('#ha-token')),'Credentials have a separate form from general settings');
 d.querySelector('#ha-token').value='new-private-home-assistant-test-token';
 d.querySelector('#ha-connection-form').dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));await tick();
 assert.equal(requests.at(-1).method,'PUT');assert.equal(requests.at(-1).data.token,'new-private-home-assistant-test-token');
 assert.equal(d.querySelector('#ha-token').value,'');assert(!d.body.textContent.includes('new-private-home-assistant-test-token'));
 d.querySelector('#ha-test').click();await tick();assert.equal(requests.at(-1).url,'/api/connections/home-assistant/test');
 assert(d.querySelector('#ha-connection-status').textContent.includes('verified'));
 d.querySelector('#settings-form').dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));await tick();
 assert.equal(requests.at(-1).url,'/api/settings');assert(!JSON.stringify(requests.at(-1).data).includes('token'));
 d.querySelector('#ha-disconnect').click();await tick();assert.equal(requests.at(-1).method,'DELETE');assert(d.querySelector('#ha-test').disabled);
 for(const [name,field] of [['github','token'],['gemini','api_key']]){
  const input=d.querySelector('#'+name+'-secret'),status=d.querySelector('#'+name+'-status'),form=d.querySelector('#'+name+'-form');
  assert.equal(input.type,'password');assert.equal(input.value,'');assert(!d.querySelector('#settings-form').contains(input));
  assert.equal(status.textContent,name==='github'?'Set (from environment)':'Not set');
  assert.equal(d.querySelector('#'+name+'-test').disabled,name==='gemini','Test needs an effective credential');
  const submit=()=>{form.dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));return tick();};
  await submit();assert.equal(requests.at(-1).method,'PUT');assert.equal(requests.at(-1).data[field],'');
  assert.equal(status.textContent,name==='github'?'Set (from environment)':'Not set','Blank save keeps source');
  input.value='private-'+name+'-test-secret';await submit();
  assert.equal(requests.at(-1).data[field],'private-'+name+'-test-secret');assert.equal(input.value,'');
  assert.equal(status.textContent,'Set (from Settings)');assert(!d.body.textContent.includes('private-'+name+'-test-secret'));
  d.querySelector('#'+name+'-test').click();await tick();assert.equal(requests.at(-1).url,'/api/connections/'+name+'/test');
  assert(d.querySelector('#'+name+'-result').textContent.includes('Test ok'));assert.equal(status.textContent,'Set (from Settings)','Test result is separate from state');
  d.querySelector('#'+name+'-clear').click();await tick();assert.equal(requests.at(-1).method,'DELETE');
  assert.equal(status.textContent,name==='github'?'Set (from environment)':'Not set');
   }
 assert(!JSON.stringify(requests.filter(r=>r.url==='/api/settings').map(r=>r.data)).includes('private-'));
 let copied='';w.navigator.clipboard={writeText:async value=>{copied=value;}};d.querySelector('#copy-icon-url').click();await tick();
 assert.equal(copied,'https://raw.githubusercontent.com/Crimson3076/unraid-ripe/main/ripe/static/icon.png');
 assert.equal(d.querySelector('#copy-icon-url').textContent,'Copied!');assert.equal(errors.length,0);dom.window.close();
 ({dom,w,d,requests}=await browser(true));
 for(const n of ['github','gemini'])assert(d.querySelector('#'+n+'-save').disabled&&d.querySelector('#'+n+'-test').disabled&&d.querySelector('#'+n+'-clear').disabled);
 assert(d.querySelector('#ha-save').disabled);assert(d.querySelector('#ha-test').disabled);assert.equal(d.querySelector('#ha-url').value,'');
 const before=requests.length;d.querySelector('#ha-connection-form').dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));await tick();
 assert.equal(requests.length,before,'Demo cannot submit live credentials');dom.window.close();
 console.log('Ripe settings UI passed: private HA/GitHub/Gemini forms, token clearing, saved tests, disconnect, general settings isolation, icon copy and demo protection.');
})().catch(error=>{console.error(error);process.exitCode=1;});
