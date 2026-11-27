import { byId as $, element as el } from './dom.js';
import { tr } from './language.js';

const url=new URL(location.href), id=url.searchParams.get('id');
const runId=/^run-\d{8}-\d{6}-[a-f0-9]{8}$/;
let locale=url.searchParams.get('lang'), cursor='', run=null, meta={}, runs=[], error='', timer;
let active=true, follow=location.hash==='#live', polling=false;
const events=new Map(), opened=new Set();
const t=key=>tr(locale,key);
const statuses={running:'traceRunning',completed:'traceCompleted',failed:'traceFailed',interrupted:'traceInterrupted'};
const tools={
  view_image:['查看图像','View image'],view_mesh:['查看网格','View mesh'],view_mesh_section:['查看网格剖面','View mesh section'],
  inspect_mesh_directions:['检查建筑朝向','Inspect mesh directions'],measure_mesh_pixels:['测量网格','Measure mesh'],
  build_plan_bim:['构建建筑模型','Build BIM'],finish_bim:['交付 Sim-BIM','Deliver Sim-BIM'],
  delegate_workers:['委派协作任务','Delegate workers'],collect_worker:['收集协作结果','Collect worker result'],
  collect_workers:['收集协作结果','Collect worker results'],spawn_worker:['启动协作任务','Start worker']
};
const statusLabel=value=>t(statuses[value] || 'traceUnknown');
function dateLabel(seconds) {
  if (!Number.isFinite(seconds)) return '';
  return new Intl.DateTimeFormat(locale==='en'?'en-GB':'zh-CN',{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}).format(seconds*1000);
}
function timeLabel(seconds) {
  if (!Number.isFinite(seconds)) return '';
  return new Date(seconds*1000).toLocaleTimeString(locale==='en'?'en-GB':'zh-CN',{hour12:false});
}
function elapsed() {
  if (!Number.isFinite(meta.started_at)) return '';
  const end=meta.ended_at || (run?.status==='running' ? Date.now()/1000 : null);
  if (!end) return '';
  const seconds=Math.max(0,Math.floor(end-meta.started_at));
  return Math.floor(seconds/60)+'m '+String(seconds%60).padStart(2,'0')+'s';
}
function deliveryUrl() {
  if (run?.status!=='completed' || !run.viewer) return null;
  const path=new URL(run.viewer,location.origin);
  if (path.origin!==location.origin || !/^\/results\/[a-f0-9]{16}\//.test(path.pathname) ||
      !path.pathname.endsWith('/'+id+'/Sim-BIM.html')) return null;
  const viewer=new URL('/static/viewer.html',location.origin);
  viewer.searchParams.set('lang',locale);viewer.hash=path.pathname;return viewer;
}
function renderHeader() {
  $('run-title').textContent=meta.workspace_name || t('activity');
  document.title=$('run-title').textContent+' · Sim-BIM';
  const line=$('run-status-line');
  line.textContent=run ? t(run.status) : t('traceConnecting');
  line.dataset.status=run?.status || 'waiting';
  const items=[[t('traceModel'),meta.model],[t('traceStart'),dateLabel(meta.started_at)],[t('traceElapsed'),elapsed()]];
  const fragment=document.createDocumentFragment();
  for (const [name,value] of items) if (value) {
    const item=el('div');item.append(el('dt','',name),el('dd',name===t('traceElapsed')?'elapsed-value':'',value));fragment.append(item);
  }
  if (runId.test(meta.parentRunId || '')) {
    const entry=el('div'), value=el('dd'), link=el('a','',t('openParentRun'));
    const parentUrl=new URL(location.href);parentUrl.searchParams.set('id',meta.parentRunId);parentUrl.hash='';
    link.href=parentUrl.href;value.append(link);entry.append(el('dt','',t('parentRun')),value);fragment.append(entry);
  }
  $('run-meta').replaceChildren(fragment);
  const target=deliveryUrl(), link=$('delivery-link');
  link.hidden=!target;if (target) link.href=target.href;
  const download=$('download-link');download.hidden=!target;
  if (target) download.href=target.hash.slice(1)+'?download=1';
  const warning=error ? t(error) : (run?.status==='failed'?run.message:'');
  $('run-error').textContent=warning || '';$('run-error').hidden=!warning;
  $('events-placeholder').hidden=!!events.size;
  $('events-placeholder').textContent=t(run && run.status!=='running' ? 'traceEmpty':'traceWaiting');
}
function renderHistory() {
  const select=$('run-history');select.parentElement.hidden=runs.length<2;
  select.replaceChildren(...runs.map(record=>new Option([dateLabel(record.started_at) || record.id,
    ...(record.parentRunId?[t('revision')]:[]),statusLabel(record.status)].join(' · '),record.id)));
  select.value=id;
}
function renderLanguage() {
  document.documentElement.lang=locale;
  $('language-switch').setAttribute('aria-label',t('language'));
  document.querySelectorAll('[data-t]').forEach(node=>{node.textContent=t(node.dataset.t);});
  document.querySelectorAll('[data-lang]').forEach(node=>node.setAttribute('aria-pressed',String(node.dataset.lang===locale)));
  renderHeader();renderHistory();renderTimeline();
}
function detail(label,value) {
  const block=el('div','event-data');block.append(el('strong','',label));
  block.append(el('pre','',typeof value==='string'?value:JSON.stringify(value,null,2)));
  return block;
}
function disclosure(identity,caption,sub,status,time) {
  const group=el('details','trace-disclosure'), summary=el('summary');
  group.dataset.identity=identity;group.open=opened.has(identity);
  summary.append(el('span','event-title',caption));
  if (sub) summary.append(el('span','event-sub',sub));
  if (status) {const badge=el('span','event-status',statusLabel(status));badge.dataset.status=status;summary.append(badge);}
  if (Number.isFinite(time)) summary.append(el('time','event-time',timeLabel(time)));
  group.append(summary);
  // Listen to user activation instead of delayed toggle events from reconstructed rows.
  summary.addEventListener('click',()=>{if (group.open) opened.delete(identity);else opened.add(identity);});
  return group;
}
function toolRow(row) {
  const short=(row.tool || '').split('__').pop(), name=tools[short]?.[locale==='en'?1:0] || short || t('traceResult');
  const group=disclosure(row.id,name,'',row.status,row.time);
  if (row.tool) group.append(el('code','tool-name',row.tool));
  if (row.args!=null) group.append(detail(t('traceInputs'),row.args));
  if (row.result!=null) group.append(detail(t('traceResult'),row.result));
  if (row.text) group.append(el('p','trace-text',row.text));
  return group;
}
function eventRow(row) {
  if (row.kind==='tool') return toolRow(row);
  if (row.kind==='message') return el('p','trace-text',row.text || '');
  if (row.kind==='retry') {
    const group=disclosure(row.id,t(row.action==='automatic_delivery_continuation'?'traceContinue':'traceRetry'),'#'+row.attempt,null,row.time);
    if (row.result!=null) group.append(detail(t('traceResult'),row.result));
    else group.append(el('p','worker-model',row.action));
    return group;
  }
  const line=el('div','trace-state');
  line.append(el('strong','',t(row.action==='session'?'traceSessionStart':'traceTurnEnd')));
  if (row.attempt>1) line.append(el('span','event-sub','#'+row.attempt));
  if (row.status==='failed') line.append(el('span','event-status',statusLabel(row.status)));
  if (row.text) line.append(el('p','trace-text',row.text));
  return line;
}
function renderTimeline() {
  const rows=[...events.values()], fragment=document.createDocumentFragment(), steps=new Map();
  const assigned=new Set(), children=new Map(), delegated=new Map();
  const stepKey=r=>[r.attempt ?? 1,r.turn ?? '',r.step].join('/');
  const add=(map,key,row)=>{if (!map.has(key)) map.set(key,[]);map.get(key).push(row);};
  for (const row of rows) {
    if (row.source==='worker') add(children,row.task || row.parent || '',row);
    else if (row.kind==='step') steps.set(stepKey(row),[]);
    if (row.kind==='tool' && Array.isArray(row.workers)) delegated.set(row.id,row.workers);
  }
  function taskGroup(task) {
    assigned.add(task);
    const records=children.get(task) || [];
    const lifecycle=records.filter(r=>r.kind==='worker'), current=lifecycle.at(-1);
    const group=disclosure('worker/'+task,t('traceWorker'),task,current?.status,null);
    if (current?.text) group.append(el('p','trace-text',current.text));
    const activity=records.filter(r=>r.kind!=='worker');
    for (const record of activity) {
      if (record.kind==='step') {
        const step=disclosure(record.id,t('traceModel'),record.model,record.status,record.time);
        if (record.text) step.append(el('p','trace-text',record.text));
        if (record.result!=null) step.append(detail(t('traceResult'),record.result));
        group.append(step);
      } else group.append(eventRow(record));
    }
    if (!activity.length && !current?.text) group.append(el('p','worker-model',task));
    return group;
  }
  function appendEvent(container,row) {
    container.append(eventRow(row));
    for (const task of delegated.get(row.id) || []) if (children.has(task) && !assigned.has(task)) container.append(taskGroup(task));
  }
  const belongsToStep=row=>row.source==='harness' && (row.kind==='message'||row.kind==='tool') && steps.has(stepKey(row));
  for (const row of rows) if (belongsToStep(row)) {
    steps.get(stepKey(row)).push(row);
  }
  // The supervisor and each Harness attempt write separate journals. A replay can read their
  // pages in a different order to a live tail. Place recovery before the attempt it starts.
  const main=rows.filter(row=>row.source!=='worker').sort((a,b)=>
    (a.attempt ?? 1)-(b.attempt ?? 1) || Number(b.kind==='retry')-Number(a.kind==='retry'));
  for (const row of main) {
    if (belongsToStep(row)) continue;
    const item=el('li','trace-item');item.dataset.kind=row.kind;item.dataset.status=row.status || '';
    if (row.kind==='step') {
      const head=el('div','step-head');head.append(el('strong','',t('traceStep')+' '+(row.step ?? '')));
      if (row.status) head.append(el('span','event-status',statusLabel(row.status)));
      if (row.time!=null) head.append(el('time','event-time',timeLabel(row.time)));
      item.append(head);
      for (const entry of steps.get(stepKey(row)) || []) appendEvent(item,entry);
    } else appendEvent(item,row);
    fragment.append(item);
  }
  for (const task of children.keys()) if (!assigned.has(task)) {
    const item=el('li','trace-item');item.append(taskGroup(task));fragment.append(item);
  }
  const list=$('trace-list'), focused=document.activeElement?.closest('.trace-disclosure')?.dataset.identity;
  list.replaceChildren(fragment);
  if (focused) list.querySelector('[data-identity="'+CSS.escape(focused)+'"] > summary')?.focus({preventScroll:true});
}
function setLocale(next) {
  locale=next;url.searchParams.set('lang',locale);
  history.replaceState(null,'',url.href);renderLanguage();
}
async function poll() {
  if (!active || polling) return;
  polling=true;
  let again=true, delay=1100;
  try {
    const query=new URLSearchParams({id});if (cursor) query.set('cursor',cursor);
    const response=await fetch('/api/run/trace?'+query,{cache:'no-store'});
    if (!response.ok) {
      if(response.status===404 || response.status===400){again=false;error='traceMissing';return;}
      throw Error('activity');
    }
    const page=await response.json();error='';
    const atTail=window.scrollY+window.innerHeight>=document.documentElement.scrollHeight-70;
    const reading=!!document.activeElement?.closest('#trace-list');
    for (const row of page.events) events.set(row.id,{...events.get(row.id),...row});
    run=page.run;meta=page.meta || meta;cursor=page.cursor || '';
    if (!url.searchParams.has('lang') && meta.locale && locale!==meta.locale) {
      locale=meta.locale;renderLanguage();
    } else if (page.events.length) renderTimeline();
    if (page.events.length && follow && atTail && !reading && run.status==='running') {
      window.scrollTo(0,document.documentElement.scrollHeight);
    }
    if (page.more) delay=0;
    else if (run.status!=='running') {
      again=false;
      const entry=runs.find(r=>r.id===id);if (entry && entry.status!==run.status) {entry.status=run.status;renderHistory();}
      const viewer=deliveryUrl();
      if (follow && viewer && active) {
        follow=false;url.hash='';history.replaceState(null,'',url.href);location.assign(viewer.href);
      }
    }
  } catch { error='traceReconnect';delay=2400; }
  finally {
    polling=false;renderHeader();
    if (again && active) {clearTimeout(timer);timer=setTimeout(poll,delay);}
  }
}
async function loadHistory() {
  try {
    const response=await fetch('/api/runs',{cache:'no-store'});
    if (response.ok) {runs=(await response.json()).runs;renderHistory();}
  } catch { /* The open run remains available if history cannot be read. */ }
}
locale=locale==='zh-CN'?'zh-CN':'en';
document.querySelectorAll('[data-lang]').forEach(node=>{node.onclick=()=>setLocale(node.dataset.lang);});
$('run-history').onchange=e=>{
  const selected=runs.find(entry=>entry.id===e.target.value);
  if (!selected) return;
  url.searchParams.set('id',selected.id);url.hash=selected.status==='running'?'live':'';location.assign(url.href);
};
window.addEventListener('pagehide',()=>{active=false;clearTimeout(timer);});
window.addEventListener('pageshow',e=>{if (e.persisted){active=true;loadHistory();poll();}});
renderLanguage();
if (runId.test(id)) {loadHistory();poll();} else {error='traceMissing';renderHeader();}
