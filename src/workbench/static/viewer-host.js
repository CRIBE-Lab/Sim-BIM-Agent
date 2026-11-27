import { byId as $, element as el } from './dom.js';
import { tr } from './language.js';

const path=location.hash.slice(1), locale=new URL(location.href).searchParams.get('lang')==='zh-CN'?'zh-CN':'en';
const [, , workspaceTag, parentRunId]=path.split('/');
const frame=document.querySelector('iframe'), dialog=$('revision-dialog'), fallback=$('submit-edits');
const t=key=>tr(locale,key);
let model, draft, proposal, submitting=false, previousFailed=false;
const requestIds=new Map();

function activityUrl(id,follow=false) {
  const url=new URL('/static/run.html',location.origin);
  url.searchParams.set('id',id);url.searchParams.set('lang',locale);
  if (follow) url.hash='live';
  return url.href;
}
function watchDelivery(viewerUrl) {
  const download=$('download-model');
  download.href=viewerUrl.pathname+'?download=1';download.hidden=false;
  let loadedVersion, checking=false;
  async function checkVersion() {
    if (checking) return;
    checking=true;
    let version;
    try {
      const response=await fetch(viewerUrl,{method:'HEAD',cache:'no-store'});
      if (response.ok) version=response.headers.get('etag');
    } catch { /* A result can still open directly while the version check is unavailable. */ }
    finally { checking=false; }
    if (!frame.hasAttribute('src')) {
      loadedVersion=version;
      const target=new URL(viewerUrl);target.searchParams.set('rev',version || String(Date.now()));
      frame.src=target.href;
    } else if (version) {
      $('update-preview').hidden=version===loadedVersion;
    }
  }
  checkVersion();
  document.addEventListener('visibilitychange',()=>{if (!document.hidden) checkVersion();});
  window.addEventListener('pageshow',event=>{if (event.persisted) checkVersion();});
}
function changed(modelId,value,hasInlineButton) {
  model=modelId;draft=value;
  fallback.hidden=hasInlineButton;
  fallback.disabled=!value.notes.length && !value.instructions.trim() && value.northAngle===null;
}
function review() {
  if (dialog.open || !draft || !(draft.notes.length || draft.instructions.trim() || draft.northAngle!==null)) return;
  // Freeze exactly what the confirmation shows, even if another tab edits the draft.
  proposal={workspaceTag,parentRunId,parentModelId:model,draft:structuredClone(draft)};
  const summary=$('revision-summary'), edits=proposal.draft;
  summary.replaceChildren();
  if (edits.instructions.trim()) summary.append(el('h3','',t('overallEdits')),el('p','revision-text',edits.instructions));
  if (edits.notes.length) {
    const list=el('ol','revision-notes');
    for (const note of edits.notes) {
      const row=el('li');
      row.append(el('strong','',note.targets.map(target=>target.label || target.id).join(' · ')),el('p','revision-text',note.text));
      list.append(row);
    }
    summary.append(el('h3','',t('objectNotes')),list);
  }
  if (edits.northAngle!==null) summary.append(el('h3','',t('revisionNorth')),el('p','',edits.northAngle+'°'));
  previousFailed=false;$('revision-error').hidden=true;$('failed-revision').hidden=true;
  $('confirm-revision').textContent=t('runRevision');
  dialog.showModal();
}
async function requestId(body,renew) {
  const bytes=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(JSON.stringify(body)));
  const hash=Array.from(new Uint8Array(bytes),value=>value.toString(16).padStart(2,'0')).join('');
  const key='sim-bim:revision-request:'+hash;
  let id=requestIds.get(key);
  try {id=id || localStorage.getItem(key);} catch { /* This tab can still retry without browser storage. */ }
  if (!id || renew) {
    id=crypto.randomUUID();
    try {localStorage.setItem(key,id);} catch { /* Fall back to the in-memory key. */ }
  }
  requestIds.set(key,id);
  return id;
}
async function submit() {
  if (submitting || !proposal) return;
  submitting=true;
  $('confirm-revision').disabled=true;$('cancel-revision').disabled=true;
  $('confirm-revision').textContent=t('statusPending');$('revision-error').hidden=true;
  try {
    const id=await requestId(proposal,previousFailed);
    previousFailed=false;$('failed-revision').hidden=true;
    const response=await fetch('/api/revisions',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({...proposal,requestId:id})});
    const result=await response.json().catch(()=>({}));
    if (!response.ok || !/^run-\d{8}-\d{6}-[a-f0-9]{8}$/.test(result.run?.id || '')) {
      throw Error(result.error || t('revisionFailed'));
    }
    if (result.run.status==='failed') {
      previousFailed=true;
      $('failed-revision').href=activityUrl(result.run.id);$('failed-revision').hidden=false;
      $('revision-error').textContent=t('failedRevision');$('revision-error').hidden=false;
      return;
    }
    dialog.close();
    location.assign(activityUrl(result.run.id,result.run.status==='running'));
  } catch (error) {
    $('revision-error').textContent=error instanceof TypeError?t('revisionFailed'):error.message;
    $('revision-error').hidden=false;
  } finally {
    submitting=false;$('confirm-revision').disabled=false;$('cancel-revision').disabled=false;
    $('confirm-revision').textContent=t(previousFailed?'rerunRevision':'runRevision');
  }
}
document.documentElement.lang=locale;
document.querySelector('nav').setAttribute('aria-label',t('workspace'));
document.querySelectorAll('[data-t]').forEach(node=>{node.textContent=t(node.dataset.t);});
$('activity-link').href=activityUrl(parentRunId);
$('update-preview').onclick=()=>location.reload();
fallback.onclick=review;
$('cancel-revision').onclick=()=>dialog.close();
dialog.addEventListener('cancel',event=>{if (submitting) event.preventDefault();});
$('confirm-revision').onclick=submit;
if (!SimBimViewerDraft.host(frame,path,watchDelivery,{changed,review})) location.replace('/');
window.addEventListener('hashchange',()=>location.reload());
