"""Object annotations embedded in the standalone Sim-BIM viewer."""

ANNOTATION_MARKUP = r"""
    <div id="annotationLayer" hidden></div>
    <form id="annotationEditor" class="viewer-card" role="dialog" aria-labelledby="annotationTarget" hidden>
      <div class="annotation-heading">
        <strong id="annotationTarget"></strong>
        <span id="annotationCount" aria-live="polite" hidden></span>
      </div>
      <textarea id="annotationText" rows="4"></textarea>
      <div class="card-actions">
        <button type="button" id="deleteAnnotation" hidden>
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M9 7V4h6v3M7 7l1 13h8l1-13M10 10v7m4-7v7"/></svg>
        </button>
        <button type="submit" id="confirmAnnotation" disabled><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m4 12 5 5L20 6"/></svg></button>
      </div>
    </form>
"""

ANNOTATION_CSS = r"""
  #annotationLayer {position:absolute;inset:0;pointer-events:none;overflow:hidden;}
  .annotation-leaders {position:absolute;width:100%;height:100%;fill:none;stroke:#8592a3;stroke-width:1;}
  .annotation-pin {position:absolute;pointer-events:auto;display:flex;align-items:flex-start;justify-content:start;gap:9px;
    width:max-content;max-width:min(320px,38%);padding:10px 14px;color:var(--ink);font-size:14px;line-height:1.45;
    font-weight:450;text-align:start;white-space:normal;}
  .annotation-pin::before {content:"";flex:none;width:8px;height:8px;margin-top:6px;border-radius:50%;background:#d53845;}
  .annotation-pin span {min-width:0;display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:4;overflow:hidden;
    overflow-wrap:anywhere;white-space:pre-wrap;}
  .annotation-pin.compact {width:18px;height:18px;align-items:center;justify-content:center;padding:0;border-radius:50%;}
  .annotation-pin.compact::before {margin:0;}
  .annotation-pin.compact span {display:none;}
  .annotation-pin:hover,.annotation-pin:focus-visible {border-color:#889ab2;z-index:1;}
  #annotationEditor {position:absolute;z-index:3;display:grid;width:min(clamp(366px,38%,416px),calc(100% - 24px));
    overflow:hidden;color:var(--ink);}
  .annotation-heading {display:flex;align-items:center;justify-content:space-between;gap:10px;
    margin:var(--card-inset) var(--card-inset) 8px;}
  #annotationTarget {font-size:17px;line-height:1.35;font-weight:650;}
  #annotationCount {color:var(--muted);font-size:13px;}
  #annotationText {box-sizing:border-box;width:calc(100% - 2 * var(--card-inset));min-height:108px;max-height:clamp(108px,28vh,260px);
    margin:0 var(--card-inset) 12px;padding:7px 2px;resize:none;font:inherit;font-size:16px;line-height:1.55;
    color:inherit;border:0;background:transparent;outline:0;}
  #annotationText::placeholder {color:var(--muted);}
  #annotationEditor:focus-within {border-color:#91a6bf;}
  #deleteAnnotation,#confirmAnnotation {width:36px;height:36px;padding:0;border-radius:9px;}
  #deleteAnnotation {background:transparent;border-color:transparent;color:var(--muted);}
  #deleteAnnotation:hover {background:#f1f4f8;}
  #confirmAnnotation {margin-left:auto;color:#fff;background:var(--blue);border-color:var(--blue);}
  #confirmAnnotation:disabled {background:#e8ecf2;border-color:#e8ecf2;color:#748093;cursor:default;}
"""

ANNOTATION_JS = r"""
  const editMarkers=(()=>{
    const layer=$('annotationLayer'), form=$('annotationEditor'), input=$('annotationText');
    const heading=$('annotationTarget'), confirm=$('confirmAnnotation'), remove=$('deleteAnnotation');
    const leaders=document.createElementNS('http://www.w3.org/2000/svg','svg');
    leaders.setAttribute('class','annotation-leaders');leaders.setAttribute('aria-hidden','true');
    const overlays=[$('editPanel'),document.querySelector('.view-filters'),document.querySelector('.stage-tools'),$('northForm'),$('northHandle')];
    const highlighted=new Set(), byTarget=new Map(), byAnchor=new Map();
    const pins=[], projected=new THREE.Vector3();
    let targets=[], editingId=null, indexed=false;
    let renderedNotes=null, renderedView=null, renderedLocale=null, previousLayout='';
    const localized=(en,zh)=>locale==='en' ? en : zh;
    const notes=()=>viewerDraft.state.notes;
    const faceName=u=>u.draftName ?? u.publicName;
    const targetKey=(kind,id,surface)=>JSON.stringify(kind==='surface' ? [kind,id,surface] : [kind,id]);
    const key=t=>targetKey(t.kind,t.id,t.surface);
    const floorId=i=>String(NAMED_FLOORS[i]?.id ?? i);
    const anchorKey=(zone,surface)=>JSON.stringify([zone,surface]);
    function addIndex(map,id,mesh){
      if(!map.has(id)) map.set(id,[]);
      map.get(id).push(mesh);
    }
    function indexMeshes(){
      if(indexed) return;
      for(const mesh of allPickables()){
        const u=mesh.userData, surface=faceName(u);
        addIndex(byAnchor,anchorKey(String(u.zone),surface),mesh);
        addIndex(byTarget,targetKey('surface',String(surfaceBinding(u)[1]),surface),mesh);
        addIndex(byTarget,targetKey('zone',String(sourceIdOfZone(u.zone))),mesh);
        for(const floor of actualStoreys(u.zone,u.floor)){
          addIndex(byTarget,targetKey('floor',floorId(floor)),mesh);
        }
      }
      indexed=true;
    }
    function targetAt(hit,kind){
      const mesh=hit.object,u=mesh.userData,surface=faceName(u);
      let id,label;
      if(kind==='floor'){
        const floor=pickedFloor(hit);
        id=floorId(floor);label=floorLabel(floor);
      }else if(kind==='zone'){
        id=sourceIdOfZone(u.zone);label=spaceName(u.zone);
      }else{
        id=surfaceBinding(u)[1];label=u.publicName;
      }
      return {kind,id:String(id),label:String(label),zone:String(u.zone),surface,
        point:hit.point.clone().sub(mesh.position).toArray()};
    }
    function recolor(){
      highlighted.clear();
      function include(t){for(const mesh of byTarget.get(key(t)) || []) highlighted.add(mesh);}
      if(editView){
        for(const note of notes()) for(const target of note.targets) include(target);
        targets.forEach(include);
      }
      refreshColors();
    }
    function pending(){
      if(!targets.length) return false;
      if(!editingId) return Boolean(input.value.trim());
      const saved=notes().find(n=>n.id===editingId);
      return !!saved && (input.value.trim()!==saved.text || targets.length!==saved.targets.length ||
        targets.some((t,i)=>key(t)!==key(saved.targets[i])));
    }
    function fitText(){
      if(form.hidden) return;
      const scroll=input.scrollTop;
      input.style.height='0';
      input.style.height=input.scrollHeight+'px';
      input.scrollTop=scroll;
    }
    window.addEventListener('resize',fitText);
    function editorLabels(){
      heading.textContent=editingId ? localized('Edit comment','编辑批注') : localized('Add a comment','添加批注');
      const count=$('annotationCount');
      count.hidden=targets.length<2;
      count.textContent=count.hidden ? '' : localized(targets.length+' selected','已选 '+targets.length+' 项');
      input.placeholder=localized('Describe the change…','描述要做的修改…');
      input.setAttribute('aria-label',localized('Object note','构件批注'));
      for(const [button,label] of [[confirm,localized('Confirm note','确认批注')],[remove,localized('Delete note','删除批注')]]){
        button.title=label;button.setAttribute('aria-label',label);
      }
      confirm.disabled=!input.value.trim();remove.hidden=!editingId;
      form.hidden=!editView || !targets.length;
      $('editHint').hidden=!pending();
      fitText();
    }
    function pickTarget(hit,kind,extend){
      if(!hit || !['floor','zone','surface'].includes(kind)){cancel();return;}
      const target=targetAt(hit,kind), i=targets.findIndex(t=>key(t)===key(target));
      if(extend){
        if(i<0) targets.push(target);
        else targets.splice(i,1);
      }else if(i===0 && targets.length===1){
        targets[0]=target;
      }else{
        editingId=null;input.value='';targets=[target];
      }
      if(!targets.length){cancel();return;}
      sync(true);input.focus({preventScroll:true});
    }
    function open(note){
      editingId=note.id;targets=note.targets.slice();input.value=note.text;
      sync(true);input.focus({preventScroll:true});
    }
    function cancel(){
      editingId=null;targets=[];input.value='';
      $('editHint').hidden=true;
      sync(true);
    }
    form.onsubmit=event=>{
      event.preventDefault();
      const text=input.value.trim();
      if(!editView || !text || !targets.length) return;
      const id=editingId || (globalThis.crypto?.randomUUID?.() ?? String(Date.now())+'-'+Math.random().toString(36).slice(2));
      const note={id,text,targets:targets.slice()};
      const next=editingId ? notes().map(n=>n.id===id ? note : n) : [...notes(),note];
      viewerDraft.update({notes:next});
      cancel();
    };
    remove.onclick=()=>{
      if(!editingId) return;
      viewerDraft.update({notes:notes().filter(note=>note.id!==editingId)});
      cancel();
    };
    input.oninput=()=>{
      fitText();
      confirm.disabled=!input.value.trim();
      $('editHint').hidden=!pending();
    };
    window.addEventListener('keydown',event=>{
      if(!editView || !targets.length || event.isComposing) return;
      if(event.key==='Escape' && !document.querySelector('dialog[open]')){
        cancel();
      }
      if(event.target===input && event.key==='Enter' && (event.ctrlKey || event.metaKey)){
        event.preventDefault();if(!confirm.disabled) form.requestSubmit();
      }
    });
    function makePin(note,target){
      const el=document.createElement('button'), text=document.createElement('span');
      const line=document.createElementNS(leaders.namespaceURI,'line');
      leaders.appendChild(line);
      el.type='button';el.className='annotation-pin viewer-card';el.hidden=true;el.dataset.noteId=note.id;
      el.title=note.text;el.setAttribute('aria-label',localized('Edit comment: ','编辑批注，')+note.text);
      text.textContent=note.text;el.appendChild(text);el.onclick=()=>open(note);
      layer.appendChild(el);
      return {el,target,line};
    }
    function sync(force=false){
      const saved=notes(), snapshot=editView ? JSON.stringify(saved) : '';
      if(!force && renderedView===editView && renderedLocale===locale && renderedNotes===snapshot) return;
      renderedView=editView;renderedLocale=locale;renderedNotes=snapshot;
      if(editView) indexMeshes();
      leaders.replaceChildren();layer.replaceChildren(leaders);pins.length=0;layer.hidden=!editView;previousLayout='';
      if(editView){
        if(editingId && !saved.some(note=>note.id===editingId)) editingId=null;
        for(const note of saved){
          if(note.id!==editingId) pins.push(note.targets.map(target=>makePin(note,target)));
        }
      }
      editorLabels();recolor();frame();
    }
    function screenPoint(target,width,height){
      const faces=byAnchor.get(anchorKey(target.zone,target.surface));
      const mesh=faces?.find(m=>m.visible);
      if(!mesh) return null;
      projected.fromArray(target.point).add(mesh.position);
      if(comparisonPlanes().some(p=>p.distanceToPoint(projected)<-0.0001)) return null;
      projected.project(camera);
      if(Math.abs(projected.x)>1 || Math.abs(projected.y)>1 || Math.abs(projected.z)>1) return null;
      return {x:Math.round((projected.x+1)*width/2),y:Math.round((1-projected.y)*height/2)};
    }
    function place(el,x,y){
      const left=x+'px',top=y+'px';
      if(el.style.left!==left) el.style.left=left;
      if(el.style.top!==top) el.style.top=top;
    }
    // Try either side of the anchor, then the nearest free row above or below an obstacle.
    function anchoredBox(point,w,h,width,height,occupied,offsetX,offsetY){
      const within=(value,max)=>Math.max(8,Math.min(value,max-8));
      const anchor={left:point.x-6,top:point.y-6,right:point.x+6,bottom:point.y+6};
      const obstacles=[anchor,...occupied], preferredY=point.y+offsetY;
      let best=null, leastOverlap=Infinity, shortest=Infinity;
      for(const idealX of [point.x+offsetX,point.x-offsetX-w]){
        const x=within(idealX,width-w);
        const near=obstacles.filter(r=>x<r.right+6 && x+w>r.left-6);
        const rows=[preferredY,...near.flatMap(r=>[r.top-h-6,r.bottom+6])];
        for(const row of rows){
          const y=within(row,height-h);
          const overlap=near.reduce((area,r)=>area+
            (Math.min(x+w,r.right+6)-Math.max(x,r.left-6))*
            Math.max(0,Math.min(y+h,r.bottom+6)-Math.max(y,r.top-6)),0);
          const distance=Math.abs(x-idealX)+Math.abs(y-preferredY);
          if(overlap<leastOverlap || (overlap===leastOverlap && distance<shortest)){
            best={left:x,right:x+w,top:y,bottom:y+h};leastOverlap=overlap;shortest=distance;
            if(!distance && !overlap) return best;
          }
        }
      }
      return best;
    }
    function frame(){
      if(!editView || (!pins.length && !targets.length)) return;
      const view=viewport.getBoundingClientRect(),width=view.width,height=view.height;
      const occupied=overlays.filter(el=>!el.hidden).map(el=>{
        const r=el.getBoundingClientRect();
        return {left:r.left-view.left,top:r.top-view.top,right:r.right-view.left,bottom:r.bottom-view.top};
      });
      const points=pins.map(group=>group.map(pin=>screenPoint(pin.target,width,height)));
      const active=targets.length ? screenPoint(targets[targets.length-1],width,height) : null;
      const layout=JSON.stringify([width,height,form.offsetHeight,occupied,points,active]);
      if(previousLayout===layout) return;
      previousLayout=layout;
      if(targets.length){
        const point=active || {x:width/2,y:height/2};
        const box=anchoredBox(point,form.offsetWidth,form.offsetHeight,width,height,occupied,24,16);
        place(form,box.left,box.top);occupied.push(box);
      }
      pins.forEach((group,g)=>{
        let labeled=false;
        group.forEach(({el,line},i)=>{
          const point=points[g][i];
          el.hidden=!point;line.style.display='none';
          if(!point) return;
          el.classList.toggle('compact',labeled);
          if(labeled){place(el,point.x-el.offsetWidth/2,point.y-el.offsetHeight/2);return;}
          const box=anchoredBox(point,el.offsetWidth,el.offsetHeight,width,height,occupied,12,-18);
          place(el,box.left,box.top);occupied.push(box);labeled=true;
          const x=Math.min(box.right,Math.max(box.left,point.x)),y=Math.min(box.bottom,Math.max(box.top,point.y));
          line.setAttribute('x1',point.x);line.setAttribute('y1',point.y);
          line.setAttribute('x2',x);line.setAttribute('y2',y);line.style.display='';
        });
      });
    }
    return {highlighted,pickTarget,cancel,pending,sync,frame};
  })();
"""
