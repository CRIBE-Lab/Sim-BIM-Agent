import { byId as $, element as el, icon, button } from './dom.js';
import { tr, totals, catalogCount } from './language.js';
import { previewAsset } from './preview.js';
import { connectionForm } from './connection-form.js';
import { thicknessPicker } from './thickness.js';

const state = { workspace: null, locale: 'zh-CN', project: {blocks:[],revision:0}, connections: [], run: null };
let fields = {taskBrief:'',coordinator:null,workers:[],thicknessCandidatesMm:{}};
let draft = [], editing = null, saveTimer, runTimer, pollToken = 0, busy = false, loaded = false;
let work = Promise.resolve(), previewController, dialogType, drag = null;
let connectionsTimer, connectionsEpoch=0;
const pendingConnections=new Set();
const t = key=>tr(state.locale,key);
const thickness = thicknessPicker($('thickness-groups'), t, candidates => {
  fields.thicknessCandidatesMm=candidates; queueSave();
  renderRun();
}, renderRun);
const same = (a,b)=>JSON.stringify(a) === JSON.stringify(b);
const modelKey = selection=>selection ? JSON.stringify([selection.provider,selection.model]) : '';
const parseModel = key=>{ const [provider,model]=JSON.parse(key); return {provider,model}; };
const projectBlocks = blocks=>blocks.map(b=>({id:b.id,caption:b.caption,assetIds:b.assets.map(a=>a.id)}));
const dirtyFields = ()=>fields.taskBrief !== state.project.taskBrief || modelKey(fields.coordinator) !== modelKey(state.project.coordinator) ||
  !same(fields.workers,state.project.workers ?? []) || !same(fields.thicknessCandidatesMm,state.project.thicknessCandidatesMm ?? {});
const dirtyBlock = ()=>Boolean(editing || draft.length || $('caption').value.trim());
const fmtName = a=>a.name ?? a.id;
const allModels = ()=>state.connections.flatMap(c=>(c.models ?? []).map(m=>({provider:c.provider,model:m.id,label:fmtName(m),connection:c})));
const mark = (word, value)=>word+' · '+value;
const connectionStatus = conn=>t(conn.custom ? 'savedConnection' : conn.connected ? 'connected' : 'disconnected');

async function request(path, data) {
  const options = data === undefined ? {} : data instanceof FormData ? {method:'POST',body:data} :
    {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)};
  const response = await fetch('/api/'+path,options);
  let result;
  try { result=await response.json(); } catch { throw Error(t('browserOffline')); }
  if (!response.ok || result.error) {
    const error = Error(result.error || response.statusText);
    error.status=response.status; error.code=result.code; throw error;
  }
  return result;
}
function notice(message, error = false) {
  $('notice-text').textContent=message; $('notice').classList.toggle('error',error); $('notice').hidden=!message;
}
async function handleError(error) {
  if (error.status === 409 && String(error.code ?? error.message).match(/revision|stale|conflict|changed|已更新|已切换|已不存在/i)) {
    try {
      const pending = {}, workspace=state.workspace;
      for (const key of Object.keys(fields)) if (!same(fields[key],state.project[key])) pending[key]=fields[key];
      const next=await request('state');
      if (next.workspace!==workspace) resetDraft();
      applyState(next);
      if (workspace===state.workspace) {
        Object.assign(fields,pending); $('task-brief').value=fields.taskBrief; render();
      }
    } catch { /* Original conflict is still actionable. */ }
    notice(t('assetsChanged'),true);
  } else notice(mark(t('error'),error.message),true);
}
function enqueue(action) {
  const next=work.then(action);
  work=next.catch(handleError);
  return next;
}
function act(action) {
  if (busy) return;
  busy=true; render();
  return enqueue(action).catch(()=>{}).finally(()=>{busy=false; render();});
}
function applyState(next) {
  Object.assign(state,next);
  state.project ??= {blocks:[],revision:0};
  fields={taskBrief:state.project.taskBrief ?? '',coordinator:state.project.coordinator ?? null,
    workers:state.project.workers ?? [],thicknessCandidatesMm:state.project.thicknessCandidatesMm ?? {}};
  $('task-brief').value=fields.taskBrief;
  if (editing && !state.project.blocks?.some(b=>b.id === editing)) {
    editing=null; draft=draft.filter(asset=>asset.file);
  }
  loaded=true; render(); watchRun(); watchConnections(next.busy);
  if (next.workspaceError) notice(next.workspaceError,true);
}
function renderLanguage() {
  document.documentElement.lang=state.locale;
  document.querySelectorAll('[data-t]').forEach(e=>{e.textContent=t(e.dataset.t);});
  document.querySelectorAll('[data-label]').forEach(e=>{e.setAttribute('aria-label',t(e.dataset.label));e.title=t(e.dataset.label);});
  document.querySelectorAll('[data-placeholder]').forEach(e=>{e.placeholder=t(e.dataset.placeholder);});
  document.querySelectorAll('[data-lang]').forEach(e=>e.setAttribute('aria-pressed',String(e.dataset.lang === state.locale)));
}
function renderModels() {
  const models=allModels(), select=$('coordinator'), selected=modelKey(fields.coordinator);
  select.replaceChildren(new Option(t('chooseModel'),''));
  for (const connection of state.connections) {
    const group=el('optgroup'); group.label=connection.label;
    group.disabled=!connection.connected;
    for (const item of models.filter(m=>m.provider===connection.provider)) group.append(new Option(item.label,modelKey(item)));
    if (group.childElementCount) select.append(group);
  }
  if (selected && !models.some(m=>modelKey(m)===selected)) select.append(new Option(mark(fields.coordinator.model,t('selectionMissing')),selected));
  select.append(new Option(t('settings')+'…','settings')); select.value=selected;
  $('workers-value').textContent=fields.workers.length ? fields.workers.map(x=>models.find(m=>modelKey(m)===modelKey(x))?.label ?? x.model).join(', ') : t('noWorkers');
  $('workers').title=$('workers-value').textContent;
}
function render() {
  renderLanguage(); renderModels();
  thickness.sync(state.thicknessPresetsMm,fields.thicknessCandidatesMm,state.workspace,busy || !loaded);
  $('workspace-value').textContent=state.workspace || t('chooseFolder'); $('workspace').title=state.workspace || t('chooseFolder');
  $('compose-heading').textContent=editing ? t('blockEdit') : t('blockNew');
  $('cancel-edit').hidden=!dirtyBlock(); $('add-label').textContent=editing ? t('saveBlock') : t('add');
  for (const id of ['workspace','settings','coordinator','workers','dropzone','cancel-edit']) $(id).disabled=busy || !loaded;
  $('task-brief').readOnly=busy || !loaded; $('caption').readOnly=busy || !loaded;
  $('add').disabled=busy || !(draft.length || $('caption').value.trim());
  renderFiles(); renderBlocks(); renderRun();
}
function runReason() {
  if (!loaded) return t('browserOffline');
  if (!thickness.valid()) return t('invalidThickness');
  if (!state.workspace) return t('noWorkspace');
  if (dirtyBlock()) return t('pendingBlock');
  if (!(state.project.blocks ?? []).length) return t('noBlocks');
  if (!state.project.blocks.some(b=>b.assets.some(a=>a.kind==='image'||a.kind==='mesh'))) return t('workspaceNeedsVisual');
  if (!fields.coordinator) return t('noCoordinator');
  const ready=new Set(allModels().filter(m=>m.connection.connected).map(modelKey));
  if ([fields.coordinator,...fields.workers].some(m=>!ready.has(modelKey(m)))) return mark(t('selectionMissing'),t('missingSetup'));
  return '';
}
function renderRun() {
  const run=state.run, reason=runReason(), active=run?.status==='running';
  const alreadyRunning=active && !run.parentRunId && (run.revision==null || (run.revision===state.project.revision && !dirtyFields()));
  $('run').disabled=busy || alreadyRunning || Boolean(reason);
  $('run-hint').textContent=reason;
  const panel=$('run-status'); panel.hidden=!run; panel.replaceChildren();
  if (!run) return;
  panel.classList.toggle('error',run.status==='failed'); panel.append(el('strong','',t(active && !alreadyRunning ? 'previousRunning' : run.status)));
  const activity=el('a','link-button',t('activity'));
  activity.href=activityUrl(run.id,active); panel.append(activity);
  if (run.message) {
    const details=el('details','hint'); details.append(el('summary','',t('diagnostic')),el('p','',run.message)); panel.append(details);
  }
  for (const key of ['viewer','download']) if (run[key]) {
    let url;
    try {url=new URL(run[key],location.origin);} catch {continue;}
    if (url.origin!==location.origin) continue;
    if (key==='viewer' && url.pathname.endsWith('/Sim-BIM.html')) {
      const host=new URL('/static/viewer.html',location.origin);
      host.searchParams.set('lang',state.locale);host.hash=url.pathname;url=host;
    }
    const link=el('a','link-button',t(key)); link.href=url.href;
    if (key==='download') link.download='Sim-BIM.html';
    link.append(icon(key==='download'?'download':'external')); panel.append(link);
  }
}
function activityUrl(id, follow=false) {
  const url=new URL('/static/run.html',location.origin);
  url.searchParams.set('id',id);url.searchParams.set('lang',state.locale);
  if (follow) url.hash='live';
  return url.href;
}
function watchRun() {
  clearTimeout(runTimer); const epoch=++pollToken, id=state.run?.id;
  if (state.run?.status!=='running' || !id) return;
  async function poll() {
    if (epoch!==pollToken) return;
    try {
      const {run}=await request('run?id='+encodeURIComponent(id));
      if (epoch!==pollToken) return;
      state.run=run; renderRun(); if (run?.status!=='running') return;
    } catch (error) { if (epoch===pollToken) notice(mark(t('error'),error.message),true); }
    if (epoch===pollToken) runTimer=setTimeout(poll,1800);
  }
  runTimer=setTimeout(poll,1200);
}
function watchConnections(force=false) {
  clearTimeout(connectionsTimer);const epoch=++connectionsEpoch;
  if (!force && !state.connections.some(c=>c.busy)) return;
  async function poll() {
    if (epoch!==connectionsEpoch) return;
    try {
      const next=await request('state');
      if (epoch!==connectionsEpoch) return;
      state.connections=next.connections ?? []; renderModels(); renderRun();
      if (dialogType==='settings' && $('dialog').open) {
        document.querySelectorAll('.connection').forEach(row=>{
          const c=state.connections.find(conn=>conn.provider===row.dataset.provider);
          if (!c) return;
          row.querySelector('.count').textContent=connectionStatus(c);
          row.querySelector('.catalog-count').textContent=c.busy?t('statusPending'):catalogCount(state.locale,c.models?.length ?? 0);
          row.querySelectorAll('.dialog-actions button').forEach(b=>b.disabled=Boolean(c.busy || pendingConnections.has(c.provider)));
          const note=row.querySelector('.dialog-note');
          if (c.message) {note.textContent=c.message;note.classList.add('error');}
          else if (!c.busy && c.connected) {note.textContent='';note.classList.remove('error');}
        });
      }
      if (!next.busy && !state.connections.some(c=>c.busy)) return;
    } catch { /* Preserve unsaved edits while the local service recovers. */ }
    if (epoch===connectionsEpoch) connectionsTimer=setTimeout(poll,1800);
  }
  connectionsTimer=setTimeout(poll,1350);
}
async function saveProject(blocks, force = false) {
  clearTimeout(saveTimer);
  if (!state.workspace || (!force && !dirtyFields())) return;
  const payload={workspace:state.workspace,schema:state.project.schema,revision:state.project.revision,
    blocks:projectBlocks(blocks ?? state.project.blocks),...fields};
  const result=await request('project',payload);
  state.project=result.project; renderRun();
}
function queueSave() {
  clearTimeout(saveTimer);
  if (state.workspace) saveTimer=setTimeout(()=>enqueue(()=>saveProject()).catch(()=>{}),550);
}
function saveModelSelection() {
  renderModels();renderRun();
  if (state.workspace) enqueue(()=>saveProject()).catch(()=>{});
}
function newDialog(type,title) {
  if ($('dialog').open) $('dialog').close();
  previewController?.abort(); dialogType=type;
  const dialog=$('dialog'); dialog.classList.toggle('preview-dialog',type==='preview');
  $('dialog-title').textContent=title;
  $('dialog-body').replaceChildren(); dialog.showModal(); return $('dialog-body');
}
function workspaceDialog(onSelected) {
  const root=newDialog('workspace',t('workspace'));
  const field=el('label','dialog-field',t('path')), input=el('input');
  input.type='text';input.placeholder=t('pathHint'); input.value=state.workspace ?? '';field.append(input);
  const actions=el('div','dialog-actions'), note=el('p','dialog-note');
  async function choose(args) {
    if (!args.browse && !args.path) {input.focus();return;}
    if (state.workspace && dirtyBlock() && !window.confirm(t('discard'))) return;
    const oldWorkspace=state.workspace, oldFields={...fields};
    actions.querySelectorAll('button').forEach(b=>b.disabled=true); note.textContent=t('statusPending');
    try {
      const selected=await enqueue(async()=>{
        await saveProject(); const next=await request('workspace',args);
        if (!next.workspace || next.workspace===oldWorkspace) {note.textContent='';return;}
        if (oldWorkspace) resetDraft();
        applyState(next);
        if (!oldWorkspace && !next.project.revision && (oldFields.coordinator || oldFields.workers.length ||
              Object.keys(oldFields.thicknessCandidatesMm).length || oldFields.taskBrief)) {
          if (oldFields.coordinator) fields.coordinator=oldFields.coordinator;
          if (oldFields.workers.length) fields.workers=oldFields.workers;
          fields.thicknessCandidatesMm=oldFields.thicknessCandidatesMm;
          fields.taskBrief=oldFields.taskBrief;
          $('task-brief').value=fields.taskBrief; await saveProject();
        }
        $('dialog').close();notice('');render();return true;
      });
      if (selected && onSelected) onSelected();
      if (args.path && state.workspace===oldWorkspace && args.path===oldWorkspace) $('dialog').close();
    } catch (e) {note.textContent=mark(t('error'),e.message); note.classList.add('error');}
    finally {actions.querySelectorAll('button').forEach(b=>b.disabled=false);if (note.textContent===t('statusPending')) note.textContent='';}
  }
  actions.append(button(t('systemBrowse'),()=>choose({browse:true}),{glyph:'folder'}),button(t('useFolder'),()=>choose({path:input.value.trim()}),{style:'button primary'}));
  input.addEventListener('keydown',e=>{if (e.key==='Enter') choose({path:input.value.trim()});});
  root.append(field,actions,note); input.focus();
}
function settingsDialog() {
  const root=newDialog('settings',t('settings'));
  const isSettingsOpen=()=>$('dialog').open && (dialogType==='settings' || dialogType==='connection-form');
  async function changeConnection(payload) {
    let result;
    if (payload.provider) pendingConnections.add(payload.provider);
    try {result=await request('connections',payload);}
    catch (error) {
      if (error.status===409) {
        const latest=await request('state');
        state.connections=latest.connections;renderModels();renderRun();watchConnections(latest.busy);
      }
      throw error;
    } finally {if (payload.provider) pendingConnections.delete(payload.provider);}
    if (result.connections) state.connections=result.connections;
    renderModels();renderRun();watchConnections(Boolean(result.busy || payload.action==='login'));
    return result;
  }
  function editConnection(connection) {
    dialogType='connection-form'; $('dialog-title').textContent=t(connection?'editApi':'addApi');
    connectionForm(root,{
      connection,t,cancel:display,
      save:async payload=>{
        const form=root.querySelector('form');
        await changeConnection(payload);
        if (isSettingsOpen() && root.contains(form)) display();
      }
    });
  }
  function display() {
    dialogType='settings';$('dialog-title').textContent=t('settings');
    root.replaceChildren();
    for (const conn of state.connections) {
      const box=el('section','connection'), head=el('div','connection-head'), note=el('p','dialog-note');
      head.append(el('h3','',conn.label),el('span','count',connectionStatus(conn))); box.append(head);
      const oauth=conn.authType==='oauth' || conn.provider==='chatgpt-subscription';
      if (conn.custom) box.append(el('p','hint endpoint',conn.baseUrl));
      else if (!oauth) box.append(el('p','hint',t('usage')));
      box.append(el('p','hint catalog-count',conn.busy ? t('statusPending') : catalogCount(state.locale,conn.models?.length ?? 0)));
      const actions=el('div','dialog-actions');
      async function connectionAction(action,extra={}) {
        actions.querySelectorAll('button').forEach(b=>b.disabled=true); note.textContent=t('statusPending'); note.classList.remove('error');
        try {
          const result=await changeConnection({provider:conn.provider,action,...extra});
          if (!isSettingsOpen() || dialogType!=='settings') return;
          display();
          const target=[...root.querySelectorAll('.connection')].find(e=>e.dataset.provider===conn.provider);
          if (target && (result.message || action==='login')) target.querySelector('.dialog-note').textContent=action==='login' ? t('loginLaunched') : result.message;
        } catch(e) {note.textContent=mark(t('settingsError'),e.message);note.classList.add('error');}
        finally {actions.querySelectorAll('button').forEach(b=>b.disabled=false);}
      }
      box.dataset.provider=conn.provider;
      if (conn.custom) {
        actions.append(button(t('edit'),()=>editConnection(conn),{glyph:'edit'}));
      } else if (oauth) {
        actions.append(button(t('connect'),()=>connectionAction('login')));
      } else {
        const label=el('label','dialog-field',t(conn.connected?'replaceKey':'key')),input=el('input');
        input.type='password';input.autocomplete='off'; input.spellcheck=false; label.append(input);box.append(label);
        actions.append(button(t('configure'),()=>{
          if (!input.value.trim()) {note.textContent=t('noKey');input.focus();return;}
          connectionAction('configure',{apiKey:input.value.trim()}); input.value='';
        }));
      }
      if (conn.message) {note.textContent=conn.message; note.classList.add('error');}
      actions.append(button(t('refresh'),()=>connectionAction('refresh'),{style:'button accent-outline'}));
      if (conn.custom) actions.append(button(t('deleteApi'),()=>{
        if (window.confirm(mark(t('deleteApiConfirm'),conn.label))) connectionAction('delete',{revision:conn.revision});
      },{glyph:'trash',style:'text-button delete-connection'}));
      if (conn.busy || pendingConnections.has(conn.provider)) actions.querySelectorAll('button').forEach(b=>b.disabled=true);
      box.append(actions,note);root.append(box);
    }
    if (!state.connections.length) root.append(el('p','hint',t('browserOffline')));
    const footer=el('div','settings-footer');
    footer.append(button(t('addApi'),()=>editConnection(null),{glyph:'plus',style:'button accent-outline'}));root.append(footer);
  }
  display();
}
function workerDialog() {
  const root=newDialog('workers',t('workers')), selected=new Map(fields.workers.map(x=>[modelKey(x),x]));
  const groups=state.connections.filter(c=>c.connected && c.models?.length);
  if (!groups.length) root.append(el('p','hint',t('missingSetup')));
  const known=new Set();
  function option(parent, key, label) {
    const row=el('label','check-row'), box=el('input');box.type='checkbox';box.checked=selected.has(key);
    box.addEventListener('change',()=>box.checked ? selected.set(key,parseModel(key)) : selected.delete(key)); row.append(box,document.createTextNode(label));parent.append(row);
  }
  for (const c of groups) {
    const group=el('section','model-group');group.append(el('h3','',c.label));
    for (const m of c.models) {const key=modelKey({provider:c.provider,model:m.id}); known.add(key);option(group,key,fmtName(m));}
    root.append(group);
  }
  for (const [key,m] of selected) if (!known.has(key)) option(root,key,mark(m.model,t('selectionMissing')));
  const actions=el('div','dialog-actions');
  actions.append(button(t('settings'),settingsDialog),button(t('done'),()=>{fields.workers=[...selected.values()];$('dialog').close();saveModelSelection();},{style:'button primary'}));root.append(actions);
}

function resetDraft() {
  for (const a of draft) if (a.file) URL.revokeObjectURL(a.url);
  draft=[]; editing=null; $('caption').value=''; $('file-input').value='';
}
function addFiles(files) {
  const unsupported=[];
  for (const file of files) {
    const ext=file.name.split('.').at(-1).toLowerCase(), kind=({png:'image',glb:'mesh',json:'declaration'})[ext];
    if (!kind) {unsupported.push(file.name);continue;}
    draft.push({id:crypto.randomUUID(),name:file.name,kind,file,url:URL.createObjectURL(file)});
  }
  notice(unsupported.length ? t('blockedFormat')+' '+mark(t('unsupportedFiles'),unsupported.join(', ')) : '',Boolean(unsupported.length));
  $('file-input').value='';render();
}
function preview(asset) {
  const root=newDialog('preview',asset.name);
  previewController=new AbortController(); previewAsset(asset,root,t,state.locale,previewController.signal);
}
function fileButton(asset, compact, number) {
  const label=t('file')+' '+number+' · '+asset.name;
  const b=button(mark(t('preview'),label),()=>preview(asset),{style:'file-preview',iconOnly:true});
  const thumb=el('span','thumb'), copy=el('span','file-copy');
  if (asset.kind==='image') {const img=el('img');img.alt='';img.draggable=false;img.src=asset.url;img.loading='lazy';thumb.append(img);}
  else thumb.append(icon(asset.kind));
  copy.append(el('span','file-name',asset.name));
  if (!compact) {
    const ext=asset.name.split('.').at(-1).toUpperCase(), bytes=asset.file?.size ?? asset.size;
    const size=typeof bytes==='number' ? (bytes>=1048576 ? (bytes/1048576).toFixed(1)+' MiB' : Math.ceil(bytes/1024)+' KiB') : '';
    copy.append(el('span','file-detail',ext+(size?' · '+size:'')));
  }
  b.append(el('span','file-number',String(number)),thumb,copy);return b;
}
function actionIcon(key,glyph,handler,disabled=false) {
  return button(t(key),handler,{glyph,style:'icon-button',iconOnly:true,disabled});
}
function moveItem(items,sourceId,targetId) {
  const from=items.findIndex(item=>item.id===sourceId), to=items.findIndex(item=>item.id===targetId);
  if (from<0 || to<0 || from===to) return items;
  const next=[...items];next.splice(to,0,next.splice(from,1)[0]);return next;
}
function sortHandle(row,kind,items,index,commit) {
  const id=items[index].id, label=t('reorder')+' · '+t(kind)+' '+(index+1);
  const handle=el('button','icon-button sort-handle');handle.type='button';handle.append(icon('grip'));
  handle.title=label;handle.setAttribute('aria-label',label);handle.dataset.sortId=id;
  handle.setAttribute('aria-keyshortcuts','ArrowUp ArrowDown');
  handle.disabled=busy || items.length<2;handle.draggable=!handle.disabled;
  if (handle.disabled) return handle;
  const dragType='application/x-sim-bim-'+kind;
  handle.addEventListener('keydown',async event=>{
    if (event.key!=='ArrowUp' && event.key!=='ArrowDown') return;
    event.preventDefault();
    const target=items[index+(event.key==='ArrowUp'?-1:1)];
    if (!target) return;
    await commit(id,target.id);
    document.querySelector('.sort-handle[data-sort-id="'+CSS.escape(id)+'"]')?.focus();
  });
  handle.addEventListener('dragstart',event=>{
    drag={kind,id,index};event.dataTransfer.setData(dragType,id);event.dataTransfer.effectAllowed='move';
    event.dataTransfer.setDragImage(row,14,14);event.stopPropagation();
  });
  row.addEventListener('dragover',event=>{
    if (drag?.kind!==kind || drag.id===id || !event.dataTransfer.types.includes(dragType)) return;
    event.preventDefault();event.dataTransfer.dropEffect='move';row.dataset.drop=drag.index<index?'after':'before';
  });
  row.addEventListener('dragleave',event=>{if (!row.contains(event.relatedTarget)) delete row.dataset.drop;});
  row.addEventListener('drop',event=>{
    if (drag?.kind!==kind || !event.dataTransfer.types.includes(dragType)) return;
    event.preventDefault();event.stopPropagation();delete row.dataset.drop;
    const source=drag.id;drag=null;
    if (source!==id) commit(source,id);
  });
  handle.addEventListener('dragend',()=>{drag=null;document.querySelectorAll('[data-drop]').forEach(n=>delete n.dataset.drop);});
  return handle;
}
function renderFiles() {
  const list=$('draft-files');list.replaceChildren();
  function move(from,to) {draft=moveItem(draft,from,to);renderFiles();}
  draft.forEach((asset,index)=>{
    const row=el('div','file-row');
    row.append(sortHandle(row,'file',draft,index,move),fileButton(asset,false,index+1),
      actionIcon('remove','trash',()=>{if (asset.file) URL.revokeObjectURL(asset.url);draft.splice(index,1);render();},busy));
    list.append(row);
  });
}
function editBlock(block) {
  if (dirtyBlock() && !window.confirm(t('discard'))) return;
  resetDraft();editing=block.id;draft=[...block.assets];$('caption').value=block.caption;render();
  $('compose-heading').scrollIntoView({block:'nearest'});$('caption').focus({preventScroll:true});
}
function renderBlocks() {
  const blocks=state.project.blocks ?? [], list=$('blocks');
  $('counts').textContent=totals(state.locale,blocks.length,blocks.reduce((n,b)=>n+b.assets.length,0));list.replaceChildren();
  if (!blocks.length) {const empty=el('div','empty');empty.append(icon('blocks'),el('span','',t('empty'))); list.append(empty);return;}
  const change=transform=>act(async()=>{await saveProject(transform(state.project.blocks),true);notice('');});
  blocks.forEach((block,i)=>{
    const item=el('article','block'), head=el('div','block-heading'), name=t('block')+' '+String(i+1).padStart(2,'0');
    const edit=button(name,()=>editBlock(block),{style:'block-edit',disabled:busy});
    edit.append(icon('edit'));edit.title=mark(t('edit'),name);edit.setAttribute('aria-label',edit.title);
    head.append(sortHandle(item,'block',blocks,i,(from,to)=>change(current=>moveItem(current,from,to))),edit,
      actionIcon('remove','trash',()=>{if (!window.confirm(t('removeConfirm'))) return;change(current=>{
        if (editing===block.id) resetDraft();return current.filter(b=>b.id!==block.id);
      });},busy));
    item.append(head);
    if (block.caption) item.append(el('p','block-detail',block.caption));
    const strip=el('div','asset-strip');block.assets.forEach((asset,index)=>strip.append(fileButton(asset,true,index+1)));item.append(strip);
    list.append(item);
  });
}
function submitBlock() {
  if (!draft.length && !$('caption').value.trim()) {notice(t('emptyBlock'),true);return;}
  if (!state.workspace) {workspaceDialog(submitBlock);return;}
  const caption=$('caption').value, entries=[...draft], blockId=editing;
  act(async()=>{
    await saveProject();
    const multipart=new FormData();
    multipart.set('workspace',state.workspace);multipart.set('revision',String(state.project.revision));multipart.set('caption',caption);
    if (blockId) multipart.set('blockId',blockId);
    let newIndex=0;
    const order=entries.map(a=>{
      if (!a.file) return a.id;
      multipart.append('files',a.file,a.name); return '@'+newIndex++;
    });
    if (blockId) multipart.set('assetOrder',JSON.stringify(order));
    const result=await request('blocks',multipart);
    state.project=result.project;resetDraft();notice('');
  });
}
async function setLanguage(locale) {
  if (locale===state.locale) return;
  await saveProject();
  const priorFields=fields;
  const next=await request('preferences',{locale, workspace:state.workspace, revision:state.project.revision}); applyState(next);
  if (!state.workspace) {
    fields=priorFields;
    $('task-brief').value=fields.taskBrief; render();
  }
  if (!next.workspaceError) notice('');
}
function initialize() {
  document.querySelectorAll('i[data-icon]').forEach(node=>node.replaceWith(icon(node.dataset.icon)));
  render();
  $('settings').onclick=settingsDialog; $('workspace').onclick=()=>workspaceDialog(); $('workers').onclick=workerDialog;
  $('coordinator').onchange=e=>{
    if (e.target.value==='settings') {renderModels();settingsDialog();return;}
    fields.coordinator=e.target.value ? parseModel(e.target.value) : null;saveModelSelection();
  };
  document.querySelectorAll('[data-lang]').forEach(b=>{b.onclick=()=>act(()=>setLanguage(b.dataset.lang));});
  $('dialog-close').onclick=()=>$('dialog').close();
  $('dialog').addEventListener('close',()=>{if (!$('dialog').open) {previewController?.abort();dialogType=null;}});
  $('dismiss').onclick=()=>notice('');
  $('cancel-edit').onclick=()=>{resetDraft();render();};
  $('dropzone').onclick=()=>$('file-input').click();
  $('file-input').onchange=e=>addFiles(e.target.files);
  for (const name of ['dragover','dragenter']) $('dropzone').addEventListener(name,e=>{
    if (!e.dataTransfer.types.includes('Files')) return;
    e.preventDefault();e.dataTransfer.dropEffect='copy';$('dropzone').classList.add('drag-over');
  });
  $('dropzone').addEventListener('dragleave',e=>{if (!$('dropzone').contains(e.relatedTarget)) $('dropzone').classList.remove('drag-over');});
  window.addEventListener('dragover',e=>{if (e.dataTransfer.types.includes('Files')) e.preventDefault();});
  window.addEventListener('drop',e=>{if (e.dataTransfer.types.includes('Files')) e.preventDefault();});
  $('dropzone').addEventListener('drop',e=>{if (!e.dataTransfer.types.includes('Files')) return;e.preventDefault();$('dropzone').classList.remove('drag-over');if (!busy && loaded) addFiles(e.dataTransfer.files);});
  $('caption').oninput=()=>{$('add').disabled=busy || !(draft.length || $('caption').value.trim());$('cancel-edit').hidden=!dirtyBlock();renderRun();};
  $('task-brief').oninput=e=>{fields.taskBrief=e.target.value;queueSave();renderRun();};
  $('task-brief').onblur=()=>{if (dirtyFields()) enqueue(()=>saveProject()).catch(()=>{});};
  $('add').onclick=submitBlock;
  window.addEventListener('pageshow',event=>{if (event.persisted) watchRun();});
  $('run').onclick=()=>{
    thickness.finishInput();
    // Capture the catalog shown at click time, before any queued save or background refresh.
    const providers=new Set([fields.coordinator,...fields.workers].map(route=>route?.provider));
    const connectionRevisions=Object.fromEntries(state.connections
      .filter(conn=>conn.custom && providers.has(conn.provider)).map(conn=>[conn.provider,conn.revision]));
    act(async()=>{
      await saveProject();
      const result=await request('runs',{workspace:state.workspace,revision:state.project.revision,connectionRevisions});
      state.run=result.run;notice('');
      location.assign(activityUrl(result.run.id, result.run.status==='running'));
    });
  };
  enqueue(async()=>applyState(await request('state'))).catch(()=>{});
}
initialize();
