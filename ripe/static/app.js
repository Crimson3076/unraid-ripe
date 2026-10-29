'use strict';
(() => {
  const paths = {
    grid:'<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
    activity:'<path d="M3 12h4l3-8 4 16 3-8h4"/>',
    settings:'<path d="m9 3-1 3-3 1-2 4 2 3v4l4 2 3-1 3 1 4-2v-4l2-3-2-4-3-1-1-3Z"/><circle cx="12" cy="12" r="3"/>',
    shield:'<path d="M12 3 4 6v5c0 5 4 8 8 10 4-2 8-5 8-10V6l-8-3Z"/><path d="m8.5 12 2.5 2.5 4.5-5"/>',
    lock:'<rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/>',
    moon:'<path d="M20.5 13A8.5 8.5 0 0 1 11 3.5 8.5 8.5 0 1 0 20.5 13Z"/>',
    sun:'<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19M5 19l1.5-1.5m11-11L19 5"/>',
    refresh:'<path d="M20 8a8 8 0 0 0-14-2L3 9m0-6v6h6M4 16a8 8 0 0 0 14 2l3-3m0 6v-6h-6"/>',
    check:'<path d="m5 12 4 4L19 6"/>',
    book:'<path d="M12 5c-3-2-6-2-9-1v15c3-1 6-1 9 1 3-2 6-2 9-1V4c-3-1-6-1-9 1Zm0 0v15"/>',
    clock:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    search:'<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
    arrow:'<path d="M4 12h16m-6-6 6 6-6 6"/>',
    'arrow-left':'<path d="M20 12H4m6-6-6 6 6 6"/>',
    chevron:'<path d="m9 5 7 7-7 7"/>',
    sprout:'<path d="M12 21v-9M12 13C5 13 3 8 3 4c7-1 10 3 9 9Zm0 4c0-7 4-10 9-10 0 6-3 10-9 10Z"/>',
    flask:'<path d="M9 3h6m-5 0v6l-6 10c-.5 1 0 2 1.5 2h13c1.5 0 2-1 1.5-2L14 9V3M7 15h10"/>',
    bell:'<path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4"/>',
    sparkles:'<path d="m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5L12 3ZM20 2v4m-2-2h4"/>',
    server:'<rect x="3" y="3" width="18" height="7" rx="2"/><rect x="3" y="14" width="18" height="7" rx="2"/><path d="M7 6.5h.01M7 17.5h.01M11 6.5h6m-6 11h6"/>',
    globe:'<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a17 17 0 0 1 0 18 17 17 0 0 1 0-18Z"/>',
    box:'<path d="m12 3 9 5v9l-9 5-9-5V8l9-5Zm0 9v10M3 8l9 4 9-4M7 5.5l9 4.5"/>',
    alert:'<path d="m12 3 10 18H2L12 3Zm0 6v5m0 3h.01"/>',
    bug:'<rect x="7" y="7" width="10" height="13" rx="5"/><path d="m9 3 1 4m5-4-1 4M3 9l4 2m10 0 4-2M3 15h4m10 0h4M5 21l3-3m8 0 3 3M7 12h10"/>',
    external:'<path d="M14 3h7v7m0-7L10 14M10 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-5"/>',
    help:'<circle cx="12" cy="12" r="9"/><path d="M9 9a3 3 0 0 1 6 0c0 2-3 2-3 4m0 4h.01"/>',
    scales:'<path d="M12 3v18m-5 0h10M4 7h16M6 7l-4 7h8L6 7Zm12 0-4 7h8l-4-7Z"/>',
    history:'<path d="M3 11a9 9 0 1 1 2 7M3 5v6h6m3-4v5l3 2"/>',
  };
  const icon = name => `<svg viewBox="0 0 24 24" aria-hidden="true">${paths[name] || paths.box}</svg>`;
  const h = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const $ = (selector, scope=document) => scope.querySelector(selector);
  const $$ = (selector, scope=document) => [...scope.querySelectorAll(selector)];
  const text = (selector, value) => {const el=$(selector);if(el)el.textContent=value;};
  const hydrateIcons = (scope=document) => $$('[data-icon]', scope).forEach(el => {el.innerHTML=icon(el.dataset.icon);});
  const page = document.body.dataset.page;
  const labels = {ripe:'Ripe', waiting:'Waiting', recommended_now:'Recommended now', read_first:'Read first', up_to_date:'Up to date', unknown:'Unknown'};
  const statusIcons = {ripe:'check',waiting:'clock',recommended_now:'shield',read_first:'book',up_to_date:'check',unknown:'help'};
  const tags = {bugfix:['bug','Fixes'],security:['shield','Security'],breaking:['alert','Breaking'],migration:['arrow','Migration'],big_feature:['sparkles','Big features'],deprecation:['clock','Deprecation'],performance:['activity','Performance'],docs_only:['book','Documentation']};
  const tiers = {infrastructure:{name:'Infrastructure',description:'The foundations of your homelab',icon:'server'},internet:{name:'Internet-facing apps',description:'Applications exposed to the outside world',icon:'globe'},general:{name:'Everything else',description:'Media, tools, and the rest of your world',icon:'box'}};
  const sources = {docker:'Docker container',home_assistant:'Home Assistant',unraid:'Unraid OS',demo:'Sample application'};
  let zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  let toastTimer;
  let dashboardData;
  let filter='all';
  let appSearch='';
  let detailData;

  function toast(message) {const el=$('#toast');if(!el)return;el.textContent=message;el.hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>el.hidden=true,6000);}
  function errorAt(selector,error) {const el=$(selector);if(!el)return;el.textContent=error?.message || String(error);el.hidden=false;}
  function clearError(selector) {const el=$(selector);if(el){el.hidden=true;el.textContent='';}}
  function demoContext(active){const badge=$('#demo-context');if(badge)badge.hidden=!active;}
  async function api(url, options={}) {
    const method=(options.method || 'GET').toUpperCase();
    const response=await fetch(url,{...options,headers:{Accept:'application/json',...(options.body ? {'Content-Type':'application/json'} : {}),...(!['GET','HEAD'].includes(method) ? {'X-Ripe-Request':'1'} : {}),...(options.headers||{})}});
    let data;try{data=await response.json();}catch{data={};}
    if(!response.ok){let message=data.detail || data.message || `Request failed (${response.status}).`;if(Array.isArray(message))message=message.map(x=>`${x.loc?.slice(1).join('.') || 'Value'}: ${x.msg || 'invalid'}`).join('; ');if(typeof message==='object')message=JSON.stringify(message);throw new Error(message);}
    return data;
  }
  function ageOf(value) {const ms=Date.now()-new Date(value).valueOf();if(!Number.isFinite(ms)||ms<0)return'unknown';const m=Math.floor(ms/60000);if(m<60)return m+'m';const hr=Math.floor(m/60);return hr<48?hr+'h':Math.floor(hr/24)+'d';}
  function date(value, includeTime=false) {if(!value)return'Unknown date';const d=new Date(value);if(!Number.isFinite(d.valueOf()))return'Unknown date';try{return new Intl.DateTimeFormat(undefined,{month:'short',day:'numeric',year:'numeric',...(includeTime?{hour:'numeric',minute:'2-digit'}:{}),timeZone:zone}).format(d);}catch{return d.toLocaleDateString();}}
  function safeURL(value) {try{const u=new URL(value);return ['http:','https:'].includes(u.protocol)&&!u.username&&!u.password ? u.href : null;}catch{return null;}}
  function sourceLink(value, label='Source notes') {const url=safeURL(value);return url?`<a href="${h(url)}" target="_blank" rel="noopener noreferrer">${h(label)} ${icon('external')}</a>`:'';}
  function chip(status,label) {const valid=Object.hasOwn(labels,status)?status:'unknown';return `<span class="status-chip ${valid}">${icon(statusIcons[valid])}${h(label || labels[valid])}</span>`;}
  function tagChips(values,quotes={}) {return (values || []).filter(t=>Object.hasOwn(tags,t)).map(t=>`<span class="tag-chip ${h(t)}"${quotes[t]?` title="Evidence: ${h(quotes[t])}"`:''}>${icon(tags[t][0])}${h(tags[t][1])}</span>`).join('');}
  function avatar(app) {const initials=app.name.split(/[\s_-]+/).slice(0,2).map(w=>w[0]||'').join('').toUpperCase();let hash=0;for(const c of app.name)hash=(hash*31+c.charCodeAt(0))|0;const palettes=[['#eaf0e5','#62844b'],['#e7f0f4','#5c8197'],['#fff0e5','#ce8454'],['#efeaf5','#9580ad'],['#f6ede9','#b78d77']];const palette=Math.abs(hash)%palettes.length;return `<span class="app-avatar avatar-p${palette}" aria-hidden="true">${h(initials)}</span>`;}
  function versions(view) {const app=view.app;return `<div class="version-track"><div><span class="track-label">Installed</span><span class="track-value">${h(app.installed_version || 'Unknown')}</span></div><span class="track-arrow">→</span><div><span class="track-label">Recommended</span><span class="track-value target-value">${h(view.recommended_version || '—')}</span></div><span class="track-arrow">→</span><div><span class="track-label">Latest</span><span class="track-value">${h(view.latest_version || app.latest_version || 'Unknown')}</span></div></div>`;}
  function statusLabel(view) {return view.status==='waiting' && Number.isFinite(view.days_remaining) ? `${view.days_remaining} ${view.days_remaining===1?'day':'days'} to go` : view.status_label || labels[view.status];}
  function hardSignals(signals) {return (signals||[]).map(s=>`<div class="hard-signal">${icon('alert')}${h(s)}</div>`).join('');}
  function needsMapping(app) {return Boolean(app.detected?.mapping_hint)&&!app.upstream_repo;}
  function appCard(view) {const app=view.app;const quotes=view.tag_quotes||{};return `<a class="app-card" href="/apps/${encodeURIComponent(app.id)}"><div class="app-card-top">${avatar(app)}<div class="app-card-title"><h3>${h(app.name)}</h3><div class="app-source">${h(sources[app.source]||app.source)}</div></div><span class="card-chevron">${icon('chevron')}</span></div><div class="card-status">${chip(view.status,statusLabel(view))}<span class="behind">${view.releases_behind?`${h(view.releases_behind)} ${view.releases_behind===1?'release':'releases'} behind`:view.status==='up_to_date'?'All caught up':''}</span></div>${versions(view)}<p class="card-reason">${h(view.rollup?.headline || view.reason || app.error || 'Open the release notes for details.')}</p>${app.latest_only?`<div class="hard-signal">${icon('box')}Latest only · ${app.image_changed_at?'image changed '+h(date(app.image_changed_at)):'no versioned target available'}</div>`:''}${view.hard_signals?.length?hardSignals(view.hard_signals.slice(0,2)):''}${needsMapping(app)?`<div class="mapping-hint" role="link" tabindex="0" data-href="/apps/${encodeURIComponent(app.id)}#profile">${icon('help')}No release notes source. Choose a GitHub repo in the app profile.</div>`:''}<div class="tag-list">${tagChips(view.tags,quotes)}</div>${sidecarList(view)}${ignoreButtons(view)}</a>`;}
  function sidecarList(view) {
    const side=view.sidecars||[];if(!side.length)return '';
    return `<div class="sidecars"><div class="sidecars-title">Supporting containers (${side.length})</div><ul>${side.map(c=>`<li><span class="sidecar-name">${h(c.app.name)}</span><span class="sidecar-version">${h(c.app.installed_version||'Unknown')}${c.latest_version&&c.latest_version!==c.app.installed_version?' → '+h(c.latest_version):''}</span>${chip(c.status,statusLabel(c))}</li>`).join('')}</ul></div>`;
  }
  function ignoreButtons(view,className='ignore-actions') {
    const app=view.app,id=h(app.id);
    const target=view.latest_version||app.latest_digest;
    const version=app.ignored_version?`<button class="button secondary small" type="button" data-undo="version" data-id="${id}">Show this version again</button>`:(view.status!=='up_to_date'&&target?`<button class="button secondary small" type="button" data-ignore="version" data-id="${id}">Ignore this version</button>`:'');
    const whole=app.ignored?`<button class="button secondary small" type="button" data-undo="app" data-id="${id}">Stop ignoring this app</button>`:`<button class="button secondary small" type="button" data-ignore="app" data-id="${id}">Ignore this app</button>`;
    return `<div class="${className}">${whole}${version}</div>`;
  }
  const attention=v=>['recommended_now','read_first'].includes(v.status)||(v.tags||[]).some(t=>['security','breaking'].includes(t));
  const sectionInfo={firmware:{name:'Device firmware',icon:'server'},cards:{name:'Home Assistant cards & integrations',icon:'box'}};
  const sectionOpen={};
  function collapsible(key,title,count,body,open) {
    return `<details class="collapse-group" data-section="${h(key)}"${open?' open':''}><summary class="tier-heading">${h(title)} (${count})</summary>${body}</details>`;
  }
  function ignoredPanel(list) {
    if(!list.length)return '';
    const rows=list.map(item=>`<li><a href="/apps/${encodeURIComponent(item.id)}">${h(item.name)}</a> <span class="muted">${item.scope==='version'?'version '+h(item.version)+' hidden':'ignored'}</span> <button class="button secondary small" type="button" data-undo="${h(item.scope)}" data-id="${h(item.id)}">Undo</button></li>`).join('');
    return collapsible('ignored','Ignored',list.length,`<ul class="ignored-list">${rows}</ul>`,sectionOpen.ignored??false);
  }
  async function changeIgnore(button) {
    const undo=Boolean(button.dataset.undo),scope=button.dataset.undo||button.dataset.ignore,id=encodeURIComponent(button.dataset.id);
    button.disabled=true;
    try{
      if(undo)await api(`/api/apps/${id}/ignore?scope=${scope}`,{method:'DELETE'});
      else await api(`/api/apps/${id}/ignore`,{method:'POST',body:JSON.stringify({scope})});
      toast(undo?'Restored.':scope==='app'?'App ignored. Undo it from the Ignored list.':'This version is hidden until a newer one appears.');
      if(page==='app')await loadDetail();else await loadDashboard(true);
    }catch(e){errorAt(page==='app'?'#detail-error':'#dashboard-error',e);button.disabled=false;}
  }
  const ignoreClick=e=>{const button=e.target.closest?.('[data-ignore],[data-undo]');if(!button)return;e.preventDefault();e.stopPropagation();void changeIgnore(button);};
  function renderGroups() {
    if(!dashboardData)return;
    const all=dashboardData.apps||[];
    const filtered=all.filter(v=>{
      const status=filter==='all'||(filter==='needs_mapping'?needsMapping(v.app):filter==='attention'?['recommended_now','read_first','unknown'].includes(v.status):v.status===filter);
      const search=`${v.app.name} ${v.app.image||''} ${v.app.upstream_repo||''}`.toLowerCase().includes(appSearch);
      return status && search;
    });
    $('#empty-state').hidden=all.length!==0||(dashboardData.ignored||[]).length!==0;
    const groups=Object.keys(tiers).map(tier=>{
      const values=filtered.filter(v=>!v.app.section&&(v.app.profile?.tier||'general')===tier);
      if(!values.length)return'';
      const info=tiers[tier];
      return `<section class="tier-group"><h3 class="tier-heading">${icon(info.icon)}${h(info.name)}<span class="count-pill">${values.length}</span><span class="tier-description">${h(info.description)}</span></h3><div class="app-grid">${values.map(appCard).join('')}</div></section>`;
    }).join('')+Object.entries(sectionInfo).map(([key,info])=>{
      const values=filtered.filter(v=>v.app.section===key);
      return values.length?collapsible(key,info.name,values.length,`<div class="app-grid">${values.map(appCard).join('')}</div>`,sectionOpen[key]??values.some(attention)):'';
    }).join('')+ignoredPanel(dashboardData.ignored||[]);
    $('#app-groups').innerHTML=groups|| (all.length?'<div class="no-results">No applications match this view. Try another filter or search.</div>':'');
    $$('[data-filter]').forEach(b=>{b.classList.toggle('active',b.dataset.filter===filter);b.setAttribute('aria-pressed',String(b.dataset.filter===filter));});
  }
  function renderIntegrations(value) {
    const panel=$('#integration-panel');if(!panel || !value)return;
    let values=Array.isArray(value)?value:Object.entries(value).map(([source,status])=> typeof status==='object'&&status!==null ? {source,...status} : {source,status});
    if(!values.length){panel.hidden=true;return;}
    panel.hidden=false;
    panel.innerHTML='<h3>Data sources</h3>'+values.map(item=>{
      const name=item.source||item.name||'Source';const state=item.status||item.state||'unknown';const ok=['ok','connected','ready','success','model available'].includes(String(state))||state===true;const failed=/unreachable|failed|error|not mounted/i.test(String(state));const unknown=!ok&&!failed;
      return `<div class="integration-item" title="${h(item.message||String(state))}"><span class="live-dot ${ok?'':unknown?'unknown':'error'}"></span>${h(name)} <span class="muted">· ${h(item.message||String(state))}</span></div>`;
    }).join('');
  }
  const profileFields={tier:'Tier',exposure:'Exposure',own_login:'Own login',dependencies:'Dependencies',config:'Config',upstream_repo:'Upstream repo'};
  function renderAutoProfiles(list) {
    const box=$('#auto-profiles');if(!box)return;box.hidden=!list.length;if(!list.length)return;
    box.innerHTML=`<strong>Ripe set up ${h(list.length)} app profile${list.length===1?'':'s'}</strong> from what Docker already knows. <details><summary>Review</summary>${list.map(a=>`<p><a href="/apps/${encodeURIComponent(a.id)}#profile">${h(a.name)}</a><br>${Object.entries(a.reasons).map(([k,v])=>`${h(profileFields[k]||k)}: ${h(v)}`).join('<br>')}</p>`).join('')}<button id="review-auto" class="button secondary small" type="button">Looks good</button></details>`;
    $('#review-auto').onclick=async()=>{try{await api('/api/auto-profiles/review',{method:'POST'});box.hidden=true;}catch(e){errorAt('#dashboard-error',e);}};
  }
  function renderStopped(data) {
    const note=$('#stopped-note');if(!note)return;
    const n=Number(data.stopped_hidden||0);note.hidden=!n;
    if(n){note.innerHTML=`${h(n)} stopped container${n===1?'':'s'} hidden · <button class="link-button" id="show-stopped" type="button">Show</button>`;
      $('#show-stopped').onclick=async()=>{try{await api('/api/settings',{method:'PATCH',body:JSON.stringify({include_stopped:true})});toast('Stopped containers are included from the next check.');await loadDashboard(true);}catch(e){errorAt('#dashboard-error',e);}};}
  }
  async function loadDashboard(quiet=false) {
    try {
      const data=await api('/api/dashboard');dashboardData=data;zone=data.timezone||zone;demoContext(data.demo);
      text('#footer-timezone',`Ripe · ${zone}`);text('#app-count',(data.apps||[]).length);
      const count=data.summary||{};['ripe','recommended_now','read_first','waiting'].forEach(key=>text(`#count-${key}`,count[key]??data.apps.filter(v=>v.status===key).length));
      text('#last-check',(data.last_collection||data.last_poll)?`Last checked ${date(data.last_collection||data.last_poll,true)} · ${data.apps.length} applications${data.classification_queue_length?` · ${data.classification_queue_length} summaries pending`:''}`:'No completed check yet · Inventory will appear after your first scan');
      $('#demo-banner').hidden=!data.demo;
      if(data.demo&&!$('#exit-demo'))$('#demo-banner').insertAdjacentHTML('beforeend','<button id="exit-demo" type="button">Leave demo</button>');
      if($('#exit-demo'))$('#exit-demo').onclick=async()=>{try{await api('/api/demo',{method:'DELETE'});await loadDashboard();toast('Demo closed. Your live inventory is active.');}catch(e){errorAt('#dashboard-error',e);}};
      renderStopped(data);renderGroups();renderIntegrations(data.integration_status);renderAutoProfiles(data.auto_profiles||[]);clearError('#dashboard-error');
    } catch(e) {if(!quiet){$('#app-groups').innerHTML='';errorAt('#dashboard-error',e);}}
  }
  const phaseNames={collecting:'Collecting sources',analysing:'Analysing releases',summarising:'Writing summaries'};
  let refreshPoll=async()=>{};
  function pollPercent(s) {
    if(s.phase==='collecting'){const t=s.sources?.total||0;return t?Math.min(100,Math.round(100*s.sources.done/t)):null;}
    if(s.phase==='analysing'){const t=s.releases?.total||0;return t?Math.min(100,Math.round(100*s.releases.done/t)):100;}
    return 100;
  }
  function etaText(seconds) {
    if(!Number.isFinite(seconds))return '';
    return seconds<60?'less than a minute left':`about ${Math.round(seconds/60)} min left`;
  }
  function pollLine(s) {
    const parts=[phaseNames[s.phase]||'Checking'];
    const c=s.phase==='collecting'?s.sources:s.phase==='analysing'?s.releases:null;
    if(c?.total)parts.push(`${c.done} of ${c.total}`);
    if(s.current)parts.push(s.current);
    const eta=s.phase==='analysing'?etaText(s.eta_seconds):'';
    if(eta)parts.push(eta);
    return parts.join(' · ');
  }
  function pollPanel(s) {
    const errors=s.errors||[];
    return `<dl><dt>Phase</dt><dd>${h(phaseNames[s.phase]||(s.last_error?'Stopped':'Finished'))}</dd><dt>Sources</dt><dd>${h(s.sources?.done??0)} of ${h(s.sources?.total??0)}</dd><dt>Releases</dt><dd>${h(s.releases?.done??0)} of ${h(s.releases?.total??0)}</dd><dt>Skipped</dt><dd>${h(s.releases?.skipped??0)} (older than the analysis cap, or no notes)</dd><dt>Current</dt><dd>${h(s.current||'—')}</dd></dl>${s.last_error?`<h4>Check stopped</h4><p>${h(s.last_error)}</p>`:''}<h4>Errors so far</h4>${errors.length?`<ul>${errors.map(e=>`<li><strong>${h(e.source)}</strong>: ${h(e.message)}</li>`).join('')}</ul>`:'<p class="muted">None.</p>'}`;
  }
  function initProgress() {
    const strip=$('#poll-strip');if(!strip)return;
    const bar=$('#poll-bar'),fill=$('#poll-bar-fill'),line=$('#poll-line'),panel=$('#poll-panel'),toggle=$('#poll-strip-toggle'),logs=$('#poll-logs-link'),dismiss=$('#poll-dismiss');
    let last=null,timer,hideTimer,wasRunning=false;
    const dismissKey='ripe-poll-dismissed';
    const dismissed=()=>{try{return sessionStorage.getItem(dismissKey);}catch{return null;}};
    const setBar=pct=>{
      bar.classList.toggle('indeterminate',pct===null);
      if(pct===null){bar.removeAttribute('aria-valuenow');fill.style.width='';}
      else{bar.setAttribute('aria-valuenow',String(pct));fill.style.width=pct+'%';}
    };
    const setButton=s=>{
      const button=$('#poll-button');if(!button)return;
      if(s.running){
        const pct=pollPercent(s);
        button.disabled=true;button.setAttribute('aria-disabled','true');button.setAttribute('aria-busy','true');
        button.innerHTML=`<span class="spinner" aria-hidden="true"></span>Checking…${pct===null?'':' '+pct+'%'}`;
      }else if(button.getAttribute('aria-busy')==='true'){
        button.disabled=false;button.removeAttribute('aria-disabled');button.removeAttribute('aria-busy');
        button.innerHTML='<span data-icon="refresh"></span>Check for updates';hydrateIcons(button);
      }
    };
    const hide=()=>{clearTimeout(hideTimer);strip.hidden=true;strip.dataset.state='idle';strip.classList.remove('fading');panel.hidden=true;toggle.setAttribute('aria-expanded','false');};
    const show=state=>{clearTimeout(hideTimer);strip.hidden=false;strip.dataset.state=state;strip.classList.remove('fading');logs.hidden=dismiss.hidden=state!=='failed';};
    function render(s) {
      last=s;setButton(s);
      if(s.running){
        show('running');setBar(pollPercent(s));line.textContent=pollLine(s);
      }else if(s.last_error){
        if(dismissed()===(s.finished_at||'x')){hide();}
        else{show('failed');setBar(100);line.textContent=`Check stopped: ${s.last_error}`;}
      }else if(wasRunning){
        show('done');setBar(100);line.textContent='Update check finished';
        hideTimer=setTimeout(()=>{strip.classList.add('fading');hideTimer=setTimeout(hide,600);},3000);
      }else if(strip.dataset.state!=='done'){
        hide();
      }
      if(!panel.hidden)panel.innerHTML=pollPanel(s);
      if(wasRunning&&!s.running)document.dispatchEvent(new CustomEvent('ripe:poll-finished',{detail:s}));
      wasRunning=Boolean(s.running);
    }
    function schedule() {
      clearTimeout(timer);
      timer=setTimeout(()=>{if(document.hidden&&!last?.running)schedule();else void refresh();},last?.running?2000:30000);
    }
    async function refresh() {
      try{render(await api('/api/poll/status'));}catch{}
      schedule();
    }
    toggle.addEventListener('click',()=>{panel.hidden=!panel.hidden;toggle.setAttribute('aria-expanded',String(!panel.hidden));if(!panel.hidden&&last)panel.innerHTML=pollPanel(last);});
    dismiss.addEventListener('click',()=>{try{sessionStorage.setItem(dismissKey,last?.finished_at||'x');}catch{}hide();});
    document.addEventListener('visibilitychange',()=>{if(!document.hidden)void refresh();});
    refreshPoll=refresh;
    void refresh();
  }
  function initDashboard() {
    document.addEventListener('ripe:poll-finished',()=>loadDashboard(true));
    $$('[data-filter]').forEach(button=>button.addEventListener('click',()=>{filter=button.dataset.filter;renderGroups();}));
    const openHint=e=>{const hint=e.target.closest?.('.mapping-hint');if(!hint||(e.type==='keydown'&&e.key!=='Enter')||(e.type==='auxclick'&&e.button!==1))return;e.preventDefault();e.stopPropagation();if(e.ctrlKey||e.metaKey||e.shiftKey||e.type==='auxclick')window.open(hint.dataset.href,'_blank','noopener');else location.href=hint.dataset.href;};
    $('#app-groups').addEventListener('toggle',e=>{const key=e.target.dataset?.section;if(key)sectionOpen[key]=e.target.open;},true);
    $('#app-groups').addEventListener('click',ignoreClick);
    $('#app-groups').addEventListener('click',openHint);$('#app-groups').addEventListener('auxclick',openHint);$('#app-groups').addEventListener('keydown',openHint);
    $('#app-search').addEventListener('input',e=>{appSearch=e.target.value.toLowerCase().trim();renderGroups();});
    $('#poll-button').onclick=async()=>{const button=$('#poll-button');if(button.getAttribute('aria-busy')==='true')return;button.disabled=true;try{const result=await api('/api/poll',{method:'POST'});toast(result.message || (result.status==='demo'?'Demo mode uses sample data and does not scan your homelab.':result.status==='already running'?'An update check is already running.':'Update check started. This may take a little while.'));await refreshPoll();}catch(e){errorAt('#dashboard-error',e);}finally{if(button.getAttribute('aria-busy')!=='true')button.disabled=false;}};
    $('#demo-button').onclick=async()=>{const button=$('#demo-button');button.disabled=true;try{await api('/api/demo',{method:'POST'});filter='all';await loadDashboard();toast('Exploring synthetic examples. No live actions or notifications.');}catch(e){errorAt('#dashboard-error',e);}finally{button.disabled=false;}};
    loadDashboard();setInterval(()=>{if(!document.hidden)loadDashboard(true);},30000);
  }

  function normalized(value) {return String(value??'').toLowerCase().replace(/\s+/g,' ').trim();}
  function hasEvidence(quote, notes) {const q=normalized(quote);return q.length>0&&normalized(notes).includes(q);}
  function plainMarkdown(value) {
    // Show quoted source text without raw Markdown syntax; nothing but escaped text and http(s) links is rendered.
    return h(String(value??'')).replace(/!?\[([^\]]*)\]\((https?:\/\/[^\s)]+)\)/g,(m,label,url)=>`<a href="${url}" target="_blank" rel="noopener noreferrer">${label||url}</a>`).replace(/\[([^\]]+)\]\([^)]*\)/g,'$1').replace(/^\s{0,3}#{1,6}\s+/gm,'').replace(/(\*\*|__)(.+?)\1/g,'$2').replace(/`([^`]+)`/g,'$1');
  }
  function evidence(quote,label='Release notes') {return `<blockquote class="quote"><span class="quote-label">${h(label)}</span>“${plainMarkdown(quote)}”</blockquote>`;}
  function applicability(release,kind,index) {
    const result=(release.applicability||[]).find(item=>item.kind===kind&&item.index===index);
    if(!result||!result.verified||!hasEvidence(result.evidence,release.notes)||!result.profile_evidence)return '<div class="applicability"><span class="status-chip waiting">'+icon('help')+'Treated as applies</span><p>Personal impact has not been verified by the local model. Review this item before updating.</p></div>';
    const states={applies:['read_first','Applies to you'],might_apply:['waiting','Might apply'],does_not_apply:['ripe','Does not apply']};
    const [color,label]=states[result.status]||states.applies;
    return `<div class="applicability ${h(result.status)}">${chip(color,label)}<p>${h(result.reason)}</p>${evidence(result.evidence,'Changelog evidence')}${evidence(result.profile_evidence,'Your profile')}</div>`;
  }
  function finding(release,item,kind,index) {
    if(!hasEvidence(item.evidence,release.notes))return'';
    const kinds={security:['shield','Security'],breaking:['alert','Breaking change'],migration:['arrow','Migration'],big_features:['sparkles','Feature']};
    const [sym,label]=kinds[kind];
    const extra=kind==='security'?`<p>${item.severity?`<strong>Severity:</strong> ${h(item.severity)}. `:''}${item.requires?`<strong>Requires:</strong> ${h(item.requires)}`:''}</p>`:kind==='breaking'&&item.action_needed?`<p><strong>Action needed:</strong> ${h(item.action_needed)}</p>`:kind==='migration'&&Array.isArray(item.steps)?`<ol>${item.steps.map(s=>`<li>${h(s)}</li>`).join('')}</ol>`:'';
    return `<div class="finding"><div class="finding-title">${icon(sym)}${h(label)}</div><p>${h(item.description)}</p>${extra}<details><summary>Supporting evidence</summary>${evidence(item.evidence)}</details>${['security','breaking','migration'].includes(kind)?applicability(release,kind,index):''}</div>`;
  }
  function releaseCard(release,history=false) {
    const c=release.classification||{};
    const supportedTags=(c.tags||[]).filter(t=>hasEvidence(c.tag_evidence?.[t],release.notes));
    const summary=c.summary && hasEvidence(c.summary_evidence,release.notes) ? `<p class="release-summary">${h(c.summary)}</p><details><summary>Summary evidence</summary>${evidence(c.summary_evidence)}</details>` : '';
    const seenWarnings=new Set();
    const warningItems=(c.keyword_warnings||[]).filter(w=>{const key=normalized(w.quote||w.term||w.message);if(seenWarnings.has(key))return false;seenWarnings.add(key);return true;});
    const warnings=warningItems.length?`<div class="notice warning">${icon('alert')} ${warningItems.length===1?h(warningItems[0].message||`Mentions “${warningItems[0].term}”, not addressed by the model; worth a look.`):`${warningItems.length} passages mention terms the model did not address: ${[...new Set(warningItems.map(w=>h(w.term)).filter(Boolean))].join(', ')}.`}<details><summary>Show unaddressed passages</summary>${warningItems.filter(w=>w.quote&&hasEvidence(w.quote,release.notes)).map(w=>evidence(w.quote,w.term?`Unaddressed passage · ${w.term}`:'Unaddressed passage')).join('')}</details></div>`:'';
    const addressed=(c.keyword_hits_addressed||[]).filter(a=>hasEvidence(a.quote||a.evidence,release.notes));
    const coverage=addressed.length?`<details><summary>Context checks · ${addressed.length} passages addressed</summary>${addressed.map(a=>`<div class="finding"><div class="finding-title">${h(a.term)} · ${a.matters?'Relevant':'Not a concern in context'}</div><p>${h(a.why)}</p>${evidence(a.quote||a.evidence)}</div>`).join('')}</details>`:'';
    const findings=['security','breaking','migration','big_features'].map(kind=>(c[kind]||[]).map((item,i)=>finding(release,item,kind,i)).join('')).join('');
    const notable=Boolean(summary||findings||supportedTags.length);
    const nothing=c.status==='ok'?'<p class="muted release-summary">Nothing notable found.</p>':'<p class="muted release-summary">No evidence-backed classification available yet. The source notes are still available below.</p>';
    return `<article class="release-card"><div class="release-heading"><h3>${h(release.version)}</h3>${release.prerelease?'<span class="tag-chip breaking">Pre-release</span>':''}<time class="release-date">${h(date(release.published_at))}</time></div>${supportedTags.length?`<div class="tag-list">${tagChips(supportedTags,c.tag_evidence)}</div>`:''}${release.model_disagreement?`<div class="notice warning">${icon('scales')} Models disagree on tags or severity. Read the source notes before deciding.</div>`:''}${summary||(notable?'':nothing)}${findings}${supportedTags.length?`<details class="tag-evidence"><summary>Classification evidence · ${supportedTags.length} tags</summary>${supportedTags.map(tag=>`<div class="finding-title">${h(tags[tag]?.[1]||tag)}</div>${evidence(c.tag_evidence[tag])}`).join('')}</details>`:''}${warnings}${coverage}${c.model?`<div class="model-meta"><span>${h(c.provider||'Local')} · ${h(c.model)}</span><span>Prompt ${h(c.prompt_version||'unknown')}</span>${c.confidence&&notable?`<span>${h(c.confidence)} confidence</span>`:''}</div>`:''}<div class="release-footer">${sourceLink(release.source_url)}${release.source_url?'':'<span>No source URL available</span>'}</div><details><summary>Read the original release notes</summary><pre class="raw-notes">${h(release.notes||'No release notes were available from this source.')}</pre></details></article>`;
  }
  const rollupGroups={action:['alert','What you need to do'],security:['shield','Security'],features:['sparkles','New features'],fixes:['bug','Fixes'],under_the_hood:['server','Under the hood']};
  const rollupSources={image:'Container image config (registry API)',manifest:'Dependency manifest changes (GitHub compare)'};
  function rollupItems(view,group) {
    // Every bullet must cite something: a quote found in the release it names, or an image/manifest fact.
    const notes=Object.fromEntries((view.pending||[]).map(r=>[r.version,r.notes]));
    return ((view.rollup?.groups||{})[group]||[]).filter(item=>item.source==='release'?hasEvidence(item.evidence,notes[item.release]):Boolean(item.evidence&&rollupSources[item.source]));
  }
  function rollupItemFor(view,version) {
    for(const group of Object.keys(rollupGroups)){const item=rollupItems(view,group).find(i=>i.source==='release'&&i.release===version);if(item)return item;}
    return null;
  }
  function rollupPanel(view) {
    const rollup=view.rollup;if(!rollup)return '';
    const sections=Object.entries(rollupGroups).map(([group,[sym,label]])=>{
      const items=rollupItems(view,group);if(!items.length)return '';
      return `<div class="rollup-group ${h(group)}"><h3>${icon(sym)}${h(label)}</h3><ul>${items.map(item=>`<li><span>${h(item.text)}</span>${item.source==='release'?` <span class="rollup-from">· ${h(item.release)}</span>`:''}<details><summary>Evidence</summary>${evidence(item.evidence,item.source==='release'?`Release ${item.release}`:rollupSources[item.source])}</details></li>`).join('')}</ul></div>`;
    }).join('');
    const unavailable=rollup.status==='unavailable'?`<div class="notice warning">${icon('alert')} Summary unavailable: ${h(rollup.error||'model unavailable')}. It will be retried at the next check.</div>`:'';
    const body=sections||(rollup.status==='unavailable'?'':`<p class="rollup-empty">${h(rollup.headline||'Nothing notable found.')}</p>`);
    const meta=rollup.model?`<div class="model-meta"><span>${h(rollup.provider||'Local')} · ${h(rollup.model)}</span><span>Prompt ${h(rollup.prompt_version||'unknown')}</span></div>`:'';
    return `<section class="rollup-card" id="rollup"><h2>${icon('sparkles')}Since your version (${h(rollup.installed||view.app.installed_version||'unknown')})</h2>${unavailable}${rollup.span_note?`<p class="muted">${h(rollup.span_note)}</p>`:''}${body}${meta}</section>`;
  }
  function releaseLine(release,view) {
    const c=release.classification||{};
    if(c.summary&&hasEvidence(c.summary_evidence,release.notes))return c.summary;
    const item=rollupItemFor(view,release.version);if(item)return item.text;
    const labelled=(c.tags||[]).filter(t=>hasEvidence(c.tag_evidence?.[t],release.notes)).map(t=>tags[t]?.[1]).filter(Boolean);
    if(labelled.length)return labelled.join(', ');
    return c.status==='ok'?'Nothing notable found':'Not classified yet';
  }
  function releaseRow(release,view,history=false) {
    return `<details class="release-row${history?' history':''}"><summary><span class="release-row-version">${h(release.version)}</span>${release.prerelease?'<span class="tag-chip breaking">Pre-release</span>':''}<time class="release-date">${h(date(release.published_at))}</time><span class="release-row-line">${h(releaseLine(release,view))}</span></summary>${releaseCard(release,history)}</details>`;
  }
  function pendingList(view) {
    const pending=view.pending||[];if(!pending.length)return '';
    const quiet=new Set(view.no_notes||[]);
    const rows=pending.filter(r=>!quiet.has(r.version)).map(r=>releaseRow(r,view)).join('');
    const none=pending.filter(r=>quiet.has(r.version)).map(r=>r.version);
    return `<div class="release-list">${rows}${none.length?`<p class="no-notes-line">${h(none.join(', '))}: no notes</p>`:''}</div>`;
  }
  function historyList(view) {
    const past=(view.history||[]).slice().reverse();
    if(!past.length)return '<div class="empty-section">Releases at or below your installed version appear here after a scan detects that you have updated.</div>';
    const row=r=>releaseRow(r,view,true);
    return `<div class="release-list">${past.slice(0,3).map(row).join('')}${past.length>3?`<details class="history-more"><summary>Show all (${past.length})</summary>${past.slice(3).map(row).join('')}</details>`:''}</div>`;
  }
  function ripeLabel(view) {
    if(!view.next_ripe_at)return '';
    const when=new Date(view.next_ripe_at),past=Number.isFinite(when.valueOf())&&when<=new Date();
    return `<div class="field-help">${past?'Ripe since':'Next ripening'}: ${h(date(view.next_ripe_at,true))}</div>`;
  }
  function autoReasons(app) {
    const ev=app.detected?.profile_evidence||{},origin=app.profile?.origin||{};
    const rows=Object.entries(ev).filter(([k])=>origin[k]!=='user').map(([k,v])=>`<li><strong>${h(profileFields[k]||k)}</strong> · set by Ripe: ${h(v)}</li>`);
    return rows.length?`<div class="notice info"><strong>Filled in automatically.</strong> Edit a field and it becomes yours; Ripe will not change it again.<ul>${rows.join('')}</ul></div>`:'';
  }
  function profileForm(app) {
    const profile=app.profile||{};
    return `<aside class="detail-sidebar"><section class="profile-card" id="profile"><h2>Your setup</h2><p>Help the local model understand what matters to this application.</p>${autoReasons(app)}<form id="profile-form"><div class="form-grid"><label>Priority tier<select name="tier">${Object.entries(tiers).map(([key,value])=>`<option value="${key}"${profile.tier===key?' selected':''}>${h(value.name)}</option>`).join('')}</select></label><label>Exposure<select name="exposure">${[['unknown','Not yet determined'],['internal','Internal only (no published ports)'],['lan','LAN only'],['internet','Internet-facing'],['vpn','VPN only']].map(([key,label])=>`<option value="${key}"${profile.exposure===key?' selected':''}>${h(label)}</option>`).join('')}</select></label><label>Has its own login?<select name="own_login"><option value="unknown"${profile.own_login==null?' selected':''}>Not yet determined</option><option value="true"${profile.own_login===true?' selected':''}>Yes</option><option value="false"${profile.own_login===false?' selected':''}>No</option></select></label><label>Dependencies<input name="dependencies" value="${h((profile.dependencies||[]).join(', '))}" placeholder="postgres, mariadb"><span class="field-help">Comma-separated app names or IDs.</span></label><label class="full-width">Relevant config keys (JSON)<textarea name="config" spellcheck="false" rows="7">${h(JSON.stringify(profile.config||{},null,2))}</textarea><span class="field-help">Only the keys needed to understand a change. Passwords, tokens, and keys are stripped before storage.</span></label><label class="full-width">Context for the local model<textarea name="notes" rows="3" placeholder="For example: No browser clients on other origins.">${h(profile.notes||'')}</textarea></label></div><button class="button primary" type="submit">${icon('check')}Save profile</button></form><div class="privacy-note">${icon('lock')}<div><strong>Local eyes only.</strong><p>Your profile goes only to your configured local Ollama. Gemini never receives it.</p></div></div><section class="mapping-section"><h3>Upstream repository</h3><form id="mapping-form"><label>GitHub owner/repository<input name="upstream_repo" value="${h(app.upstream_repo||'')}" placeholder="rommapp/romm" pattern="[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"><span class="field-help">Override automatic mapping when a container points to the wrong release source.</span></label><button class="button secondary small" type="submit">Save mapping</button></form></section></section></aside>`;
  }
  function renderDetail(view) {
    detailData=view;const app=view.app;demoContext(app.source==='demo');
    document.title=`${app.name} · Ripe`;
    text('#breadcrumb-page',app.name);
    $('#app-detail').innerHTML=`${app.source==='demo'?'<div class="demo-banner">'+icon('flask')+'Synthetic example · no live homelab data</div>':''}<div class="detail-heading">${avatar(app)}<div><h1>${h(app.name)}<span class="accent">.</span></h1><div class="app-source">${h(sources[app.source]||app.source)}${app.image?' · '+h(app.image):''}</div></div>${chip(view.status,statusLabel(view))}</div><div class="detail-overview">${versions(view)}<div class="detail-reason"><strong>${h(view.status_label||labels[view.status])}</strong>${h(view.reason||'Open the evidence below to understand this update.')}${ripeLabel(view)}</div></div>${app.error?`<div class="notice warning">${h(app.error)}</div>`:''}${view.hard_signals?.length?`<div class="notice warning">${hardSignals(view.hard_signals)}</div>`:''}${ignoreButtons(view,'ignore-actions detail-ignore')}${app.ignored?'<div class="notice info">This app is ignored: it is not checked or analysed and is hidden from the Overview.</div>':''}${app.ignored_version?`<div class="notice info">Version ${h(app.ignored_version)} is hidden on the Overview until a newer one appears.</div>`:''}${(view.sidecars||[]).length?`<section class="sidecars detail-sidecars"><h2>Supporting containers</h2><ul>${view.sidecars.map(c=>`<li><a href="/apps/${encodeURIComponent(c.app.id)}">${h(c.app.name)}</a> <span class="sidecar-version">${h(c.app.installed_version||'Unknown')}${c.latest_version&&c.latest_version!==c.app.installed_version?' → '+h(c.latest_version):''}</span>${chip(c.status,statusLabel(c))}</li>`).join('')}</ul></section>`:''}${app.parent_id?`<p class="muted">Supports <a href="/apps/${encodeURIComponent(app.parent_id)}">its parent app</a>${app.detected?.sidecar_reason?' · '+h(app.detected.sidecar_reason):''}.</p>`:''}<nav class="detail-tabs" aria-label="Application sections"><a href="#pending">Pending releases <span class="count-pill">${(view.pending||[]).length}</span></a><a href="#profile">Your setup</a><a href="#history">History <span class="count-pill">${(view.history||[]).length}</span></a></nav><div class="detail-layout"><div class="detail-main"><section id="pending"><div class="subsection-title"><h2>What’s ahead</h2><p>${view.pending?.length?'One summary for the whole span, then each release with its own evidence.':'Each release, its own evidence.'}</p></div>${rollupPanel(view)}${view.pending?.length?pendingList(view):'<div class="empty-section">No pending releases in the current inventory. If a version or source is unknown, set the repository mapping and check again.</div>'}</section>${view.available_notes?.length?`<section class="history-section"><div class="subsection-title"><h2>Available notes — update span unknown</h2><p>The installed version cannot be ordered against these releases. These are not counted as pending.</p></div>${view.available_notes.map(r=>releaseCard(r)).join('')}</section>`:''}<section class="history-section" id="history"><div class="subsection-title"><h2>Update history</h2><p>Releases you already have.</p></div>${historyList(view)}</section></div>${profileForm(app)}</div>`;
    $('#app-detail').onclick=ignoreClick;
    $('#profile-form').onsubmit=async e=>{
      e.preventDefault();const form=e.currentTarget;const data=new FormData(form);let config;
      try{config=JSON.parse(data.get('config'));if(!config||typeof config!=='object'||Array.isArray(config))throw new Error();}catch{toast('Relevant config keys must be a JSON object.');return;}
      const profile={tier:data.get('tier'),exposure:data.get('exposure'),own_login:data.get('own_login')==='unknown'?null:data.get('own_login')==='true',config,dependencies:String(data.get('dependencies')).split(',').map(x=>x.trim()).filter(Boolean),notes:data.get('notes')};
      const button=$('button[type=submit]',form);button.disabled=true;
      try{await api(`/api/apps/${encodeURIComponent(app.id)}/profile`,{method:'PATCH',body:JSON.stringify(profile)});toast('Profile saved. Relevant changes will be analyzed locally.');await loadDetail();}catch(error){errorAt('#detail-error',error);}finally{button.disabled=false;}
    };
    $('#mapping-form').onsubmit=async e=>{e.preventDefault();const form=e.currentTarget;const button=$('button',form);button.disabled=true;try{await api(`/api/apps/${encodeURIComponent(app.id)}/mapping`,{method:'PATCH',body:JSON.stringify({upstream_repo:new FormData(form).get('upstream_repo').trim()||null})});toast('Repository mapping saved. Check for updates to fetch releases.');await loadDetail();}catch(error){errorAt('#detail-error',error);}finally{button.disabled=false;}};
  }
  async function loadDetail() {try{const id=decodeURIComponent(location.pathname.split('/').filter(Boolean).pop());renderDetail(await api(`/api/apps/${encodeURIComponent(id)}`));clearError('#detail-error');}catch(error){$('#app-detail').innerHTML='';errorAt('#detail-error',error);}}

  function setForm(form,settings) {
    const data={...settings,soak_infrastructure:settings.soak_days.infrastructure,soak_internet:settings.soak_days.internet,soak_general:settings.soak_days.general,notification_people:settings.notification_people.join(', ')};
    for(const [name,value] of Object.entries(data)){const field=form.elements.namedItem(name);if(!field)continue;if(field.type==='checkbox')field.checked=!!value;else field.value=value??'';}
  }
  async function initHomeAssistant() {
    const form=$('#ha-connection-form');let connection=null,working=false;
    const render=()=>{
      const locked=working||!connection||connection.demo;
      $$('input,button',form).forEach(field=>field.disabled=locked);
      $('#ha-test').disabled=locked||!connection.configured;
      $('#ha-disconnect').disabled=locked||!connection.configured&&!connection.has_token&&!connection.url;
      if(!connection)return;
      $('#ha-token').placeholder=connection.has_token?'Saved token · leave blank to keep':'Paste your long-lived access token';
      text('#ha-token-help',connection.has_token?'A token is saved. Leave this field empty to keep it, or paste a replacement. It is never returned to the browser.':'Create a token in your Home Assistant profile → Security → Long-lived access tokens. It stays in Ripe’s private config.');
      text('#ha-connection-status',connection.demo?'Leave demo mode to configure your live Home Assistant connection.':connection.last_test?.message||(connection.source==='environment'?'Using the deployment fallback. Save here to move this connection into private app settings.':connection.configured?'Connection saved. Test it to verify access.':'Not connected. Discovery and scripted notifications need a saved connection.'));
    };
    const action=async kind=>{
      if(working||connection?.demo)return;
      const url=$('#ha-url').value.trim(),token=$('#ha-token').value;
      working=true;render();text('#ha-connection-result','Working…');
      try{
        if(kind==='save'){
          connection=await api('/api/connections/home-assistant',{method:'PUT',body:JSON.stringify({url,token})});
          $('#ha-token').value='';text('#ha-connection-result','Connection saved. Discovery will check again; notification preferences are unchanged.');
        }else if(kind==='remove'){
          connection=await api('/api/connections/home-assistant',{method:'DELETE'});$('#ha-token').value='';text('#ha-connection-result','Connection removed. Environment credentials will not be reactivated.');
        }else{
          const result=await api('/api/connections/home-assistant/test',{method:'POST'});connection.last_test=result;text('#ha-connection-result',result.message);
        }
        $('#ha-url').value=connection.url||'';
      }catch(error){text('#ha-connection-result',error.message);}finally{working=false;render();}
    };
    form.onsubmit=event=>{event.preventDefault();void action('save');};
    $('#ha-test').onclick=()=>action('test');$('#ha-disconnect').onclick=()=>action('remove');render();
    try{connection=await api('/api/connections/home-assistant');$('#ha-url').value=connection.url||'';render();}
    catch(error){text('#ha-connection-result',error.message);}
  }
  async function initApiCredential(name) {
    const form=$(`#${name}-form`),input=$(`#${name}-secret`),route=`/api/connections/${name}`;let state=null,working=false;
    const render=()=>{
      const locked=working||!state||state.demo;
      $$('input,button',form).forEach(field=>field.disabled=locked);
      $(`#${name}-test`).disabled=locked||!state.configured;
      $(`#${name}-clear`).disabled=locked||state.source!=='app';
      text(`#${name}-status`,state?(state.demo?'Leave demo mode to configure live credentials.':state.status):'');
    };
    const action=async kind=>{
      if(working||state?.demo)return;
      working=true;render();text(`#${name}-result`,'Working…');
      try{
        if(kind==='save'){const body=name==='github'?{token:input.value}:{api_key:input.value};state=await api(route,{method:'PUT',body:JSON.stringify(body)});input.value='';text(`#${name}-result`,'Saved.');}
        else if(kind==='clear'){state=await api(route,{method:'DELETE'});input.value='';text(`#${name}-result`,'Saved value cleared.');}
        else{const result=await api(route+'/test',{method:'POST'});text(`#${name}-result`,result.message);}
      }catch(error){text(`#${name}-result`,error.message);}finally{working=false;render();}
    };
    form.onsubmit=event=>{event.preventDefault();void action('save');};
    $(`#${name}-test`).onclick=()=>action('test');$(`#${name}-clear`).onclick=()=>action('clear');render();
    try{state=await api(route);render();}catch(error){text(`#${name}-result`,error.message);}
  }
  function initIconCopy(){
    $('#copy-icon-url').onclick=async event=>{
      const field=$('#unraid-icon-url'),button=event.currentTarget;let copied=false;
      try{if(navigator.clipboard?.writeText){await navigator.clipboard.writeText(field.value);copied=true;}}catch{}
      if(!copied){field.focus();field.select();try{copied=!!document.execCommand?.('copy');}catch{}}
      button.textContent=copied?'Copied!':'Select the URL above and copy it';
    };
  }
  async function initSettings() {
    void initHomeAssistant();void initApiCredential('github');void initApiCredential('gemini');initIconCopy();
    let settings;
    try{settings=await api('/api/settings');zone=settings.timezone||zone;demoContext(settings.demo_mode);if(settings.demo_mode){$('#test-notification').disabled=true;$('#test-notification').title='Demo mode never sends notifications';$('#settings-form').insertAdjacentHTML('beforebegin','<div class="demo-banner">'+icon('flask')+'Demo preferences are isolated from your live settings. Leave the demo from Overview to configure your homelab.</div>');}setForm($('#settings-form'),settings);$('#settings-form').hidden=false;$('#settings-loading').hidden=true;text('#footer-timezone',`Ripe · ${zone}`);}catch(error){$('#settings-loading').hidden=true;errorAt('#settings-error',error);return;}
    $('#browser-timezone').onclick=()=>{$('#settings-form').elements.timezone.value=Intl.DateTimeFormat().resolvedOptions().timeZone;};
    $('#settings-form').onsubmit=async e=>{
      e.preventDefault();const form=e.currentTarget;const data=new FormData(form);const button=$('button[type=submit]',form);button.disabled=true;clearError('#settings-error');
      const fields=['quiet_start','quiet_end','timezone','digest_time','dashboard_url','ollama_url','local_model','gemini_model','classification_provider','notification_script','notification_channel','npm_database_path','cloudflared_config_path'];const values={};fields.forEach(name=>values[name]=data.get(name));
      ['patch_soak_days','poll_hours','digest_weekday'].forEach(name=>values[name]=Number(data.get(name)));
      ['patch_exception','notifications_enabled','second_opinion','include_stopped'].forEach(name=>values[name]=form.elements.namedItem(name).checked);
      values.soak_days={infrastructure:Number(data.get('soak_infrastructure')),internet:Number(data.get('soak_internet')),general:Number(data.get('soak_general'))};
      values.notification_people=String(data.get('notification_people')).split(',').map(x=>x.trim()).filter(Boolean);
      try{const saved=await api('/api/settings',{method:'PATCH',body:JSON.stringify(values)});settings={...settings,...values,...saved};setForm(form,settings);zone=settings.timezone;text('#footer-timezone',`Ripe · ${zone}`);text('#settings-save-state','Saved. Your preferences are active.');toast('Settings saved.');}catch(error){errorAt('#settings-error',error);}finally{button.disabled=false;}
    };
    $('#test-notification').onclick=async()=>{const button=$('#test-notification');button.disabled=true;try{const result=await api('/api/notifications/test',{method:'POST'});toast(result.message||(result.sent?'Test sent through your configured Home Assistant script.':'Test notification requested through your configured script.'));}catch(error){errorAt('#settings-error',error);}finally{button.disabled=false;}};
  }

  function stringify(value) {return typeof value==='string'?value:JSON.stringify(value,null,2);}
  function logRow(item) {
    const passed=item.evidence_passed??item.evidence_valid??item.evidence_checks_passed;
    const evidenceStatus=passed===true?'Evidence checked':passed===false?'Evidence rejected':item.status==='error'?'Call failed':'View evidence results';
    const ms=item.duration_ms??(item.duration!=null?item.duration*1000:null);
    const input=item.input_size??item.input_chars??item.input_characters;
    const rows={...item};delete rows.model;
    return `<details class="log-entry"><summary><span class="log-model">${h(item.model||item.provider||'Model call')}</span><span class="log-meta">${item.prompt_version?`<span>Prompt ${h(item.prompt_version)}</span>`:''}${ms!=null?`<span>${h((ms/1000).toFixed(1))}s</span>`:''}${input!=null?`<span>${h(input)} chars</span>`:''}<span>${h(item.retries??item.retry??item.attempt??0)} retries</span></span><span class="status-chip ${passed===false?'read_first':passed===true?'ripe':'unknown'}">${h(evidenceStatus)}</span><time class="log-time">${h(date(item.created_at||item.timestamp||item.at,true))}</time></summary><pre class="raw-notes">${h(stringify(rows))}</pre></details>`;
  }
  async function loadLogs() {
    try{const [data,context]=await Promise.all([api('/api/logs'),api('/api/dashboard')]);demoContext(context.demo);const calls=data.llm||[];const errors=data.errors||[];let rate=Number(data.disagreement_rate||0);if(rate<=1)rate*=100;
      $('#logs-content').innerHTML=`<div class="log-summary"><div class="log-stat"><strong>${calls.length}</strong><span>Recorded model calls</span><p>Prompt versions, timing, and output</p></div><div class="log-stat"><strong>${h(rate.toFixed(1))}%</strong><span>Model disagreement rate</span><p>Compared releases with differing conclusions</p></div><div class="log-stat"><strong>${errors.length}</strong><span>Data-source errors</span><p>Failed or unavailable source requests</p></div></div><section class="log-panel"><h2>Model calls</h2>${calls.length?calls.map(logRow).join(''):'<div class="empty-section">No model calls yet. Ripe logs classification and local profile analysis as releases are collected.</div>'}</section><section class="log-panel"><h2>Data sources</h2>${errors.length?errors.map(item=>`<article class="source-error"><div class="error-top">${icon('alert')}<strong>${h(item.source||item.category||'Source error')}</strong><time>${h(date(item.created_at||item.timestamp||item.at,true))}</time></div><p>${h(item.message||item.error||stringify(item))}</p>${item.first_seen?`<p class="error-meta">Failing since ${h(date(item.first_seen,true))} · ${h(String(item.attempts||1))} attempt${item.attempts>1?'s':''} · last ${h(ageOf(item.last_seen))} ago</p>`:''}</article>`).join(''):'<div class="empty-section">No data-source errors recorded.</div>'}</section><div class="privacy-note">${icon('lock')}<div><strong>A useful trail, without your secrets.</strong><p>Model output and diagnostic records are sanitized before storage. Profiles are used only for local analysis.</p></div></div>`;clearError('#logs-error');
    }catch(error){errorAt('#logs-error',error);}
  }
  function initTheme() {
    let selected;try{selected=localStorage.getItem('ripe-theme');}catch{}
    const theme=selected|| (matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light');
    const setTheme=value=>{document.documentElement.dataset.theme=value;$('#theme-toggle').innerHTML=icon(value==='dark'?'sun':'moon');$('#theme-toggle').setAttribute('aria-label',`Switch to ${value==='dark'?'light':'dark'} theme`);};
    setTheme(theme);$('#theme-toggle').onclick=()=>{const next=document.documentElement.dataset.theme==='dark'?'light':'dark';setTheme(next);try{localStorage.setItem('ripe-theme',next);}catch{}};
  }
  hydrateIcons();initTheme();initProgress();
  const active=page==='app'?'dashboard':page;
  const link=$(`[data-nav="${active}"]`);if(link){link.classList.add('active');link.setAttribute('aria-current','page');}
  text('#breadcrumb-page',{dashboard:'Overview',settings:'Settings',logs:'Activity & logs',app:'Application'}[page]||'Overview');
  if(page==='dashboard')initDashboard();
  if(page==='app')loadDetail();
  if(page==='settings')initSettings();
  if(page==='logs'){document.addEventListener('ripe:poll-finished',loadLogs);void loadLogs();$('#refresh-logs').onclick=loadLogs;}
})();
