"""Self-contained interactive 3D viewer for Sim-BIM source models.

The offline HTML supports orbit and zoom, translucent walls, section cuts,
exploded floors or spaces, vertex measurements, selection and PNG export.
Three.js and OrbitControls are inlined from vendor; model data is embedded
with script-safe JSON encoding, so the viewer opens directly in a browser.

Coincident boundary faces are retained in the model. The viewer hides one of
an overlapping pair at zero separation and displays both in exploded views.
Window offsets keep glazing visible and selectable.

Usage:
    python scripts/tool_scripts/render_geometry_viewer.py source_model.json --out viewer.html
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from threading import Lock

from scripts.tool_scripts.bim_display_language import (
    room_role, room_type_labels, viewer_markup, viewer_messages, viewer_script,
)
from scripts.tool_scripts.bim_viewer_annotations import ANNOTATION_CSS, ANNOTATION_JS, ANNOTATION_MARKUP
from scripts.tool_scripts.bim_viewer_inspection import public_inspection

_VENDOR = Path(__file__).resolve().parent / "vendor"


def _js_embed(obj) -> str:
    """JSON for inline embedding in a <script> — escape the chars that could break
    out of the script element / be mis-tokenised (``<`` → ``</script>``, plus & and
    the JS line separators U+2028/U+2029). These become \\u00xx, which a JS object
    literal parses back to the original char."""
    return (
        json.dumps(obj, ensure_ascii=False)
        .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
        .replace(" ", "\\u2028").replace(" ", "\\u2029")
    )


# --------------------------------------------------------------------------- #
# viewer app (classic script; global THREE + THREE.OrbitControls + window.GEO).
# Standalone constant so it can be `node --check`ed.
# --------------------------------------------------------------------------- #
_APP_JS = r"""
(function () {
  const GEO = window.GEO || { zones: [], surfaces: [], windows: [], roles: {} };
  const $ = (id) => document.getElementById(id);
  let locale = 'en';
  const msg = key => GEO.messages[key]?.[locale === 'en' ? 0 : 1] ?? key;
  const SURF = (GEO.surfaces || []).filter(s => (s.verts || []).length >= 3);
  const WINS = (GEO.windows || []).filter(w => (w.verts || []).length >= 3);
  const OPENS = (GEO.openings || []).filter(o => (o.verts || []).length >= 3);
  const WALL_PARTS = GEO.visible_wall_parts || {};
  const ENC_REGIONS = GEO.enclosure_regions || [];
  const ZONES = (GEO.zones || []).slice().sort((a, b) => b.length - a.length);
  const SOURCE = GEO.source_model || null;
  const viewerDraft = SimBimViewerDraft.create(GEO.draft_model_id);
  const SOURCE_MAP = SOURCE ? (SOURCE.derived || {}) : {};
  const SOURCE_SPACES = Object.fromEntries(((SOURCE && SOURCE.spaces) || []).map(s=>[s.id,s]));
  const SOURCE_BOUNDARIES = Object.fromEntries(((SOURCE && SOURCE.boundaries) || []).map(b=>[b.id,b]));
  let confidenceView=false, editView=false;
  let modelOpacity=1;
  const GRADE_COLORS=[0xb3bac4,0x40145f,0x74399e,0xa46ec7,0xcfabe4,0xf2e6f7];
  const GRADE_LABELS=['Unassessed','Assumed','Indirect support','Partial support','Strong support','Direct evidence'];
  // resolve a window's zone: its parent surface's zone first (parent = "<wall>_<i>"),
  // then a zone-name prefix, then the nearest zone centroid — never returns '?' so a
  // window always pops out + groups/explodes with a real zone.
  const _surfZoneByName = {};
  SURF.forEach(s => { if (s.name) _surfZoneByName[s.name] = s.zone || '?'; });
  const _surfNames = Object.keys(_surfZoneByName).sort((a, b) => b.length - a.length);
  function zoneOfWindow(w){
    const p = w.parent || '';
    for (const n of _surfNames) if (p.startsWith(n)) return _surfZoneByName[n];
    const z = ZONES.find(z => p.startsWith(z)); if (z) return z;
    let cx=0,cy=0,cz=0; const vs=w.verts||[]; vs.forEach(v=>{cx+=v[0];cy+=v[1];cz+=v[2];}); const n=vs.length||1;
    const wc=new THREE.Vector3(cx/n,cy/n,cz/n); let best=ZONES[0]||'?', bd=1e18;
    for (const z in zoneCentroid){ const d=zoneCentroid[z].distanceToSquared(wc); if(d<bd){bd=d;best=z;} }
    return best;
  }

  const FLOOR_COLORS = [0xb0d0e8,0xffe0b2,0xc8e6c9,0xf4c7c7,0xd1c4e9,0xfff59d,0xb2dfdb,0xd7ccc8];
  const TYPE_COLORS = { Wall:0xdfe3e6, Floor:0xc8a165, Ceiling:0x9fa8da, Roof:0xfff3b0 };
  const WINDOW_COLOR = 0x1e5ad2, WHITE = 0xffffff, SEL_COLOR = 0xff9800;
  const ENCLOSURE_COLOR = 0x8b949e, ENCLOSURE_OPACITY = 0.32, LOGICAL_COLOR = 0x596b86;
  // Classification and colors come from the same pinned catalog as generation.
  const ROOM_TYPES = GEO.room_types || {};
  const ROLE_COLORS = Object.fromEntries(Object.entries(ROOM_TYPES).map(([k,v])=>[k,parseInt(v.color.slice(1),16)]));
  const ROLE_DEFAULT = ROLE_COLORS.unknown ?? 0xc9ced4;
  function roleLabel(role, bilingual=true){
    const type=ROOM_TYPES[role] || ROOM_TYPES.unknown;
    return locale==='en' ? type.label_en : type.label_zh+(bilingual?' · '+type.label_en:'');
  }
  const sourceIdOfZone=zone=>SOURCE_MAP.zones?.[zone] || zone;
  const spaceName=zone=>GEO.public_names.spaces[sourceIdOfZone(zone)] || 'Room';
  const ROLES = GEO.roles || {};
  const HAS_ROLES = Object.keys(ROLES).length > 0;
  const roleOf = z => (ROLES[z] || '').toString().toLowerCase();
  // zone-mode fill: by room type if roles are available, else white
  const roleColor = z => { if(!HAS_ROLES) return WHITE; const r=roleOf(z); return r ? (ROLE_COLORS[r] ?? ROLE_DEFAULT) : ROLE_DEFAULT; };
  const hex6 = c => '#'+('000000'+(c>>>0).toString(16)).slice(-6);
  const esc = s => String(s).replace(/[&<>"]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
  // Geometry is drawn at TRUE positions (so measure is exact + no inset gaps).
  // z-fighting is avoided by: (a) at explode=0, HIDING one face of each coincident
  // reciprocal (split-paired) pair — solid clean shell, no gaps; (b) at explode>0,
  // showing ALL faces (each zone a closed box) separated by the explode offset;
  // (c) windows pushed proud of their wall by WIN_POP (also makes them pickable).
  const WIN_POP = 0.03;  // metres

  // ---- floors: bases from FLOOR-type surfaces, per-zone assignment ----
  const zmin = (s) => Math.min(...s.verts.map(v => v[2]));
  function cluster(zs){ const u=[...new Set(zs.map(z=>Math.round(z*10)/10))].sort((a,b)=>a-b);
    const b=[]; for(const z of u) if(!b.length||Math.abs(z-b[b.length-1])>0.3) b.push(z); return b.length?b:[0]; }
  function nearestBase(z,bases){ let bi=0,bd=1e9; bases.forEach((b,i)=>{const d=Math.abs(z-b); if(d<bd){bd=d;bi=i;}}); return bi; }
  const floorSurf = SURF.filter(s => s.type === 'Floor');
  const NAMED_FLOORS = GEO.public_names.floors;
  const BASES = NAMED_FLOORS.length ? NAMED_FLOORS.map(f=>f.z_floor) : cluster((floorSurf.length ? floorSurf : SURF).map(zmin));
  const sourceFloor = i => (SOURCE?.floors||[]).find(f=>f.id===NAMED_FLOORS[i]?.id);
  const floorLabel=i=>NAMED_FLOORS[i]?.name || 'F'+(i+1);
  const _zFloorZ = {}, _zMinZ = {};
  SURF.forEach(s => { const z=s.zone||'?', mn=zmin(s); _zMinZ[z]=Math.min(_zMinZ[z]??1e9,mn);
    if(s.type==='Floor') _zFloorZ[z]=Math.min(_zFloorZ[z]??1e9,mn); });
  const zoneFloor = {}, zoneFloors = {};
  Object.keys(_zMinZ).forEach(z => { const sid=(SOURCE_MAP.zones||{})[z]||z;
    const owner=SOURCE_SPACES[sid]?.floor_id || GEO.primary_floors?.[sid];
    const fi=NAMED_FLOORS.findIndex(f=>f.id===owner);
    zoneFloor[z]=fi>=0 ? fi : nearestBase(_zFloorZ[z]??_zMinZ[z], BASES);
    // Filtering follows declared source membership only. A continuous space
    // remains one volume while appearing in every storey that explicitly
    // lists it; cells without section membership use their geometric floor.
    const declared=(GEO.floor_memberships||{})[z]||[];
    zoneFloors[z]=declared.length ? declared : [zoneFloor[z]];
  });
  const zoneForSpace = Object.fromEntries(Object.entries(SOURCE_MAP.zones||{}).map(([z,s])=>[s,z]));
  const verticalCore = z => ['stairwell','shaft'].includes(roleOf(z) || SOURCE_SPACES[(SOURCE_MAP.zones||{})[z]||z]?.role);
  const onFloor = (u,f) => f<0 || (!verticalCore(u.zone) && (zoneFloors[u.zone]||[u.floor]).includes(f));
  function partVisible(u,f,exploded){
    if(!onFloor(u,f)) return false;
    if(exploded || !u.dup) return true;
    if(!u.occludedBy) return false;
    return !u.occludedBy.some(sid=>{const z=zoneForSpace[sid]||sid; return onFloor({zone:z,floor:zoneFloor[z]},f);});
  }

  // ---- zone centroids (explode-by-zone + window pop-out) ----
  const zoneSum = {};
  SURF.forEach(s => { const z=s.zone||'?'; const a=zoneSum[z]||(zoneSum[z]=[0,0,0,0]);
    s.verts.forEach(v=>{a[0]+=v[0];a[1]+=v[1];a[2]+=v[2];a[3]++;}); });
  const zoneCentroid = {};
  Object.keys(zoneSum).forEach(z=>{const a=zoneSum[z]; zoneCentroid[z]=new THREE.Vector3(a[0]/a[3],a[1]/a[3],a[2]/a[3]);});
  // translate a window's verts a few cm AWAY from its zone centroid → proud of the wall
  const popOut = (verts, zone) => { const c=zoneCentroid[zone]; if(!c) return verts;
    let cx=0,cy=0,cz=0; verts.forEach(v=>{cx+=v[0];cy+=v[1];cz+=v[2];}); const n=verts.length||1; cx/=n;cy/=n;cz/=n;
    const dx=cx-c.x, dy=cy-c.y, dz=cz-c.z; const L=Math.hypot(dx,dy,dz)||1; const t=WIN_POP/L;
    return verts.map(v=>[v[0]+dx*t, v[1]+dy*t, v[2]+dz*t]); };

  // ---- bbox ----
  const bb = new THREE.Box3();
  SURF.forEach(s => s.verts.forEach(v => bb.expandByPoint(new THREE.Vector3(v[0],v[1],v[2]))));
  if (bb.isEmpty()) bb.set(new THREE.Vector3(0,0,0), new THREE.Vector3(10,10,3));
  const center = bb.getCenter(new THREE.Vector3());
  const size = bb.getSize(new THREE.Vector3());
  const radius = Math.max(size.x, size.y, size.z) || 10;
  const zoneDir = {};  // full 3D radial from building centre (zone explode = all directions)
  Object.keys(zoneCentroid).forEach(z=>{ const d=zoneCentroid[z].clone().sub(center);
    zoneDir[z] = d.lengthSq()<1e-6 ? new THREE.Vector3(0,0,0) : d.normalize(); });

  // ---- renderer / scene / camera ----
  const renderer = new THREE.WebGLRenderer({ antialias:true, preserveDrawingBuffer:true });
  const viewport=$('app');
  renderer.setPixelRatio(Math.min(window.devicePixelRatio||1,2));
  renderer.setSize(viewport.clientWidth,viewport.clientHeight,false);
  renderer.localClippingEnabled = true;
  renderer.setClearColor(0xf4f6f8);
  viewport.appendChild(renderer.domElement);
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(42, viewport.clientWidth/Math.max(1,viewport.clientHeight), 0.05, radius*200);
  camera.up.set(0,0,1);
  // The bundled Three renderer sorts transparent objects by their origin, but
  // our face vertices are in building coordinates and share an object origin.
  const sortPoint=new THREE.Vector3();
  function faceDepth(item){
    const object=item.object;
    if(!object.isMesh || !object.geometry.boundingSphere) return item.z;
    if(item.depthFrame!==renderer.info.render.frame){
      item.depthFrame=renderer.info.render.frame;
      item.z=sortPoint.copy(object.geometry.boundingSphere.center).applyMatrix4(object.matrixWorld).project(camera).z;
    }
    return item.z;
  }
  function sortConfidenceFaces(a,b){
    return a.groupOrder-b.groupOrder || a.renderOrder-b.renderOrder || faceDepth(b)-faceDepth(a) || a.id-b.id;
  }
  const controls = new THREE.OrbitControls(camera, renderer.domElement);
  let cameraMoved=false;
  controls.addEventListener('start',()=>{cameraMoved=true;});
  function defaultView(){
    const direction=new THREE.Vector3(1.6,-1.8,1.3).normalize();
    const right=camera.up.clone().cross(direction).normalize(), up=direction.clone().cross(right);
    const tanY=Math.tan(camera.fov*Math.PI/360), tanX=tanY*camera.aspect;
    let distance=0;
    for(const x of [bb.min.x,bb.max.x]) for(const y of [bb.min.y,bb.max.y]) for(const z of [bb.min.z,bb.max.z]){
      const point=new THREE.Vector3(x,y,z).sub(center);
      const fit=Math.max(Math.abs(point.dot(right))/tanX,Math.abs(point.dot(up))/tanY);
      distance=Math.max(distance,point.dot(direction)+1.25*fit);
    }
    camera.position.copy(center).addScaledVector(direction,Math.max(distance,1));
    controls.target.copy(center);controls.update();cameraMoved=false;
  }
  scene.add(new THREE.AmbientLight(0xffffff, 0.8));
  const dl=new THREE.DirectionalLight(0xffffff,0.6); dl.position.set(1,-1,2); scene.add(dl);
  const dl2=new THREE.DirectionalLight(0xffffff,0.3); dl2.position.set(-1,1,0.5); scene.add(dl2);
  const grid=new THREE.GridHelper(radius*6,76,0xd8dee6,0xe5e9ee); grid.rotateX(Math.PI/2);
  grid.material.transparent=true;grid.material.opacity=.48;grid.material.depthWrite=false;
  grid.position.set(center.x, center.y, bb.min.z); scene.add(grid);

  // ---- small text labels (axes + compass) ----
  function label(txt, color, scl){
    const c=document.createElement('canvas');c.height=64;c.width=Math.max(64,txt.length*34);const x=c.getContext('2d');
    x.fillStyle=color; x.font='bold 46px sans-serif'; x.textAlign='center'; x.textBaseline='middle'; x.fillText(txt,c.width/2,34);
    const sp=new THREE.Sprite(new THREE.SpriteMaterial({map:new THREE.CanvasTexture(c), depthTest:false, transparent:true}));
    sp.scale.set(scl*c.width/c.height, scl, 1); return sp;
  }
  // small axis ticks at the origin corner
  const guideSize=Math.max(radius*.35,Math.min(radius,Math.max(size.x,size.y)*1.2));
  const aL=guideSize*.16, aLab=guideSize*.045;
  const axes=new THREE.AxesHelper(aL); axes.position.copy(bb.min); scene.add(axes);
  [['x',0xcc1111,new THREE.Vector3(aL*1.25,0,0)],['y',0x11aa11,new THREE.Vector3(0,aL*1.25,0)],
   ['z',0x1111cc,new THREE.Vector3(0,0,aL*1.25)]].forEach(([t,col,off])=>{
    const s=label(t,'#'+col.toString(16).padStart(6,'0'),aLab); s.position.copy(bb.min).add(off); scene.add(s); });
  // Anchor the compass just outside the south edge so it orbits and exports with the model.
  const footprint=Math.max(size.x,size.y)||radius;
  const compassRadius=Math.max(footprint*.062,Math.min(radius*.052,footprint*.16));
  const compass=new THREE.Group();
  compass.name='ground-compass';
  compass.position.set(center.x-.12*size.x,bb.min.y-2.65*compassRadius,bb.min.z+.002*compassRadius);
  compass.scale.setScalar(compassRadius);
  // Model north_axis runs from true north to +Y; the control shows north clockwise from +Y.
  const wrapBearing=angle=>((angle%360)+360)%360;
  const snapBearing=angle=>wrapBearing(Math.round(angle/5)*5);
  const sourceBearing=Number.isFinite(GEO.north_axis_deg) ? wrapBearing(-GEO.north_axis_deg) : null;
  const compassInk=document.createElement('canvas');compassInk.width=compassInk.height=256;
  const pen=compassInk.getContext('2d');
  pen.translate(128,128);
  pen.beginPath();pen.arc(0,0,116,0,Math.PI*2);
  pen.fillStyle='rgba(255,255,255,.90)';pen.fill();
  pen.strokeStyle='#66768a';pen.lineWidth=8;pen.stroke();
  for(const [tip,fill] of [[-101,'#16478b'],[91,'#96a5b4']]){
    pen.beginPath();pen.moveTo(0,tip);pen.lineTo(25,0);pen.lineTo(0,tip<0?11:-11);pen.lineTo(-25,0);
    pen.closePath();pen.fillStyle=fill;pen.fill();
  }
  const rose=new THREE.Mesh(new THREE.PlaneGeometry(2,2),new THREE.MeshBasicMaterial({
    map:new THREE.CanvasTexture(compassInk),transparent:true,depthWrite:false,side:THREE.DoubleSide
  }));
  compass.add(rose);
  const northLabel=label('N','#16478b',.76), modelNorthLabel=label('N / +Y','#16478b',.76);
  for(const marker of [northLabel,modelNorthLabel]){
    marker.position.set(0,1.27,.04);compass.add(marker);
  }
  scene.add(compass);
  const northHandle=$('northHandle'), northForm=$('northForm'), northInput=$('northBearing');
  let confirmedBearing=viewerDraft.state.northAngle ?? sourceBearing, compassDrag=null;
  let northSaveTimer=0;
  function syncCompassStorage(){
    $('northStorageHint').hidden=viewerDraft.persistent;
    $('northSaveStatus').hidden=viewerDraft.persistent || !northSaveTimer || !northForm.hidden;
  }
  function showBearing(bearing){
    compass.rotation.z=-THREE.MathUtils.degToRad(bearing ?? 0);
    northLabel.visible=bearing!==null;modelNorthLabel.visible=bearing===null;
  }
  showBearing(confirmedBearing);
  function cancelCompassEdit(){
    if(compassDrag && northHandle.hasPointerCapture(compassDrag.id)) northHandle.releasePointerCapture(compassDrag.id);
    compassDrag=null;
    northForm.hidden=true;northHandle.setAttribute('aria-expanded','false');
    showBearing(confirmedBearing);
  }
  function openCompassEdit(){
    if(!northForm.hidden) return;
    if(editView) editMarkers.cancel();
    northForm.hidden=false;northHandle.setAttribute('aria-expanded','true');
    syncCompassStorage();
    northInput.value=snapBearing(confirmedBearing ?? 0);$('northApply').disabled=false;
    showBearing(Number(northInput.value));positionCompassControls();
  }
  window.addEventListener('viewer-draft-change',()=>{
    syncCompassStorage();
    const next=viewerDraft.state.northAngle ?? sourceBearing;
    if(next===confirmedBearing) return;
    confirmedBearing=next;cancelCompassEdit();
  });
  northHandle.onclick=openCompassEdit; // Keyboard activation also opens the calibration.
  northInput.oninput=()=>{
    const value=northInput.valueAsNumber;
    $('northApply').disabled=!Number.isFinite(value);
    if(Number.isFinite(value)) showBearing(snapBearing(value));
  };
  northInput.onchange=()=>{if(Number.isFinite(northInput.valueAsNumber)) northInput.value=snapBearing(northInput.valueAsNumber);};
  northInput.onkeydown=e=>{
    if(e.key==='Enter'){e.preventDefault();$('northApply').focus();}
  };
  $('northApply').onclick=()=>{
    if(!Number.isFinite(northInput.valueAsNumber)) return;
    confirmedBearing=snapBearing(northInput.valueAsNumber);
    clearTimeout(northSaveTimer);
    northSaveTimer=setTimeout(()=>{northSaveTimer=0;syncCompassStorage();},4500);
    viewerDraft.update({northAngle:confirmedBearing});
    cancelCompassEdit();syncCompassStorage();northHandle.focus({preventScroll:true});
  };
  $('northCancel').onclick=()=>{cancelCompassEdit();northHandle.focus({preventScroll:true});};
  document.addEventListener('pointerdown',e=>{
    if(!northForm.hidden && !northForm.contains(e.target) && !northHandle.contains(e.target)) cancelCompassEdit();
  },true);
  document.addEventListener('keydown',e=>{
    if(e.key==='Escape' && !northForm.hidden){cancelCompassEdit();northHandle.focus({preventScroll:true});}
  });
  const compassRay=new THREE.Raycaster(), pointerNdc=new THREE.Vector2(), floorHit=new THREE.Vector3();
  const compassPlane=new THREE.Plane(new THREE.Vector3(0,0,1),-compass.position.z);
  function groundBearing(e){
    const rect=viewport.getBoundingClientRect();
    pointerNdc.set((e.clientX-rect.left)/rect.width*2-1,1-(e.clientY-rect.top)/rect.height*2);
    compassRay.setFromCamera(pointerNdc,camera);
    if(Math.abs(compassRay.ray.direction.z)<.025 || !compassRay.ray.intersectPlane(compassPlane,floorHit)) return null;
    return THREE.MathUtils.radToDeg(Math.atan2(floorHit.x-compass.position.x,floorHit.y-compass.position.y));
  }
  northHandle.onpointerdown=e=>{
    if(e.button!==0) return;
    openCompassEdit();
    const rect=northHandle.getBoundingClientRect();
    const atCentre=Math.hypot(e.clientX-rect.left-rect.width/2,e.clientY-rect.top-rect.height/2)<5;
    compassDrag={id:e.pointerId,x:e.clientX,y:e.clientY,initial:Number(northInput.value),
                 startAngle:atCentre?null:groundBearing(e)};
    northHandle.setPointerCapture(e.pointerId);e.preventDefault();
  };
  northHandle.onpointermove=e=>{
    if(!compassDrag || e.pointerId!==compassDrag.id) return;
    const dx=e.clientX-compassDrag.x, dy=e.clientY-compassDrag.y;
    if(Math.hypot(dx,dy)<3) return;
    const worldAngle=groundBearing(e);
    // Edge-on views flatten the ground dial. A horizontal drag still adjusts its bearing.
    const angle=worldAngle===null ? compassDrag.initial+dx*2 :
      (compassDrag.startAngle===null ? worldAngle : compassDrag.initial+worldAngle-compassDrag.startAngle);
    northInput.value=snapBearing(angle);$('northApply').disabled=false;showBearing(Number(northInput.value));
  };
  northHandle.onpointerup=e=>{
    if(compassDrag?.id!==e.pointerId) return;
    compassDrag=null;northHandle.releasePointerCapture(e.pointerId);
  };
  northHandle.onpointercancel=cancelCompassEdit;
  const compassScreen=new THREE.Vector3();
  let compassLayout='';
  function positionCompassControls(){
    const w=viewport.clientWidth,h=viewport.clientHeight;
    compassScreen.copy(compass.position).project(camera);
    const x=(compassScreen.x+1)*w/2,y=(1-compassScreen.y)*h/2;
    const hidden=confidenceView || measuring || Math.abs(compassScreen.z)>1 || x<0 || x>w || y<0 || y>h;
    if(northHandle.hidden!==hidden) northHandle.hidden=hidden;
    if(hidden){if(!northForm.hidden) cancelCompassEdit();return;}
    const depth=-compassScreen.copy(compass.position).applyMatrix4(camera.matrixWorldInverse).z;
    const diameter=Math.max(30,Math.min(100,compassRadius*h/(depth*Math.tan(THREE.MathUtils.degToRad(camera.fov/2)))+8));
    const px=Math.round(x),py=Math.round(y),d=Math.round(diameter);
    const layout=[px,py,d,w,h,northForm.hidden,viewerDraft.persistent,locale].join('|');
    if(compassLayout===layout) return;
    compassLayout=layout;
    northHandle.style.cssText='left:'+px+'px;top:'+py+'px;width:'+d+'px;height:'+d+'px';
    if(!northForm.hidden){
      const above=py-d/2-northForm.offsetHeight-6;
      const top=above>50?above:py+d/2+6;
      northForm.style.left=Math.max(8,Math.min(px-northForm.offsetWidth/2,w-northForm.offsetWidth-8))+'px';
      northForm.style.top=Math.max(8,Math.min(top,h-northForm.offsetHeight-8))+'px';
    }
  }

  // ---- clip planes (section cuts) ----
  const AX = [
    {key:'X', base:new THREE.Vector3(-1,0,0), min:bb.min.x, max:bb.max.x},
    {key:'Y', base:new THREE.Vector3(0,-1,0), min:bb.min.y, max:bb.max.y},
    {key:'Z', base:new THREE.Vector3(0,0,-1), min:bb.min.z, max:bb.max.z},
  ];
  AX.forEach(a=>{ a.enabled=false; a.flip=false; a.pos=a.max; a.plane=new THREE.Plane(a.base.clone(), a.max); });
  function updatePlane(a){ a.plane.normal.copy(a.flip?a.base.clone().negate():a.base.clone()); a.plane.constant=a.flip?-a.pos:a.pos; }
  function activePlanes(){ return confidenceView ? [] : AX.filter(a=>a.enabled).map(a=>a.plane); }

  // ---- build meshes (keep ALL faces; reciprocal dup hidden at rest, windows popped out) ----
  const surfMeshes=[], winMeshes=[], openingMeshes=[], enclosureMeshes=[], edgeSegs=[], logicalLines=[], enclosureLines=[], windowEdges=[];
  const root=new THREE.Group(); scene.add(root);
  const surfaceEdges=[];
  const sideFaces=[];
  const surfaceByName=new Map(SURF.map(s=>[s.name,s]));
  function registerSide(mesh,ring,holes,host){
    if(!SOURCE || !host) return;
    const key=JSON.stringify([enclosureRegionKey(ring),holes.map(enclosureRegionKey).sort()]);
    sideFaces.push({mesh,key,host});
  }
  function projectionAxis(ring){
    let nx=0,ny=0,nz=0;
    const origin=ring[0]||[0,0,0];
    for(let i=0;i<ring.length;i++){ const a=ring[i].map((v,j)=>v-origin[j]),b=ring[(i+1)%ring.length].map((v,j)=>v-origin[j]);
      nx+=(a[1]-b[1])*(a[2]+b[2]); ny+=(a[2]-b[2])*(a[0]+b[0]); nz+=(a[0]-b[0])*(a[1]+b[1]); }
    const ax=Math.abs(nx), ay=Math.abs(ny), az=Math.abs(nz);
    return ax>=ay && ax>=az ? 0 : ay>=ax && ay>=az ? 1 : 2;
  }
  function projectRing(ring,axis=projectionAxis(ring)){
    return ring.map(v=>axis===0 ? [v[1],v[2]] : axis===1 ? [v[0],v[2]] : [v[0],v[1]]);
  }
  function signedArea2D(pts){ let a=0; for(let i=0;i<pts.length;i++){ const p=pts[i],q=pts[(i+1)%pts.length]; a+=p[0]*q[1]-q[0]*p[1]; } return a/2; }
  function triangulateRings(ring,holes=[]){
    const loops=[ring,...holes].map(r=>r.length>1 && r[0].every((v,i)=>v===r[r.length-1][i]) ? r.slice(0,-1) : r);
    const axis=projectionAxis(loops[0]), projected=loops.map(r=>projectRing(r,axis));
    // A shared local origin avoids cancellation in area validation for valid
    // georeferenced models. The buffer positions retain source coordinates.
    const origin=projected[0][0]?.slice()||[0,0];
    projected.forEach(r=>r.forEach(p=>{p[0]-=origin[0];p[1]-=origin[1];}));
    const vec=r=>r.map(p=>new THREE.Vector2(p[0],p[1]));
    // Earcut handles concave quads and near-collinear returns too. A fan is
    // never a valid fallback for arbitrary source boundaries.
    const indices=THREE.ShapeUtils.triangulateShape(vec(projected[0]),projected.slice(1).map(vec)).flat();
    const points=projected.flat(), expected=Math.abs(signedArea2D(projected[0]))-projected.slice(1).reduce((a,r)=>a+Math.abs(signedArea2D(r)),0);
    let area=0;
    for(let i=0;i<indices.length;i+=3){const [a,b,c]=indices.slice(i,i+3).map(j=>points[j]);
      area+=Math.abs((b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0]))/2;}
    if(Math.abs(area-expected)>Math.max(1e-8,Math.abs(expected)*1e-8)) throw new Error('[[msg:Display triangulation does not cover its boundary]]');
    return {positions:loops.flat().flat(),indices};
  }
  function ringArea3D(ring){
    let x=0,y=0,z=0;
    const origin=ring[0];
    for(let i=1;i<ring.length-1;i++){
      const a=ring[i].map((v,k)=>v-origin[k]),b=ring[i+1].map((v,k)=>v-origin[k]);
      x+=a[1]*b[2]-a[2]*b[1];y+=a[2]*b[0]-a[0]*b[2];z+=a[0]*b[1]-a[1]*b[0];
    }
    return Math.hypot(x,y,z)/2;
  }
  const faceArea=(outer,holes=[])=>Math.max(0,ringArea3D(outer)-holes.reduce((total,r)=>total+ringArea3D(r),0));
  function ringGeom(ring, holes=[]){ const {positions:pos,indices:idx}=triangulateRings(ring,holes);
    const g=new THREE.BufferGeometry(); g.setAttribute('position',new THREE.Float32BufferAttribute(pos,3));
    g.setIndex(idx); g.computeVertexNormals(); return g; }
  function edgeGeom(ring){ const pos=[]; for(let i=0;i<ring.length;i++){const a=ring[i],b=ring[(i+1)%ring.length];
    pos.push(a[0],a[1],a[2], b[0],b[1],b[2]);} const g=new THREE.BufferGeometry();
    g.setAttribute('position',new THREE.Float32BufferAttribute(pos,3)); return g; }
  function segmentGeom(segments){const g=new THREE.BufferGeometry();
    g.setAttribute('position',new THREE.Float32BufferAttribute(segments.flat(2),3)); return g;}
  function dashedEdge(ring,color,dash,gap,userData){
    const line=new THREE.LineSegments(edgeGeom(ring),new THREE.LineDashedMaterial({color,transparent:true,opacity:0.95,dashSize:dash,gapSize:gap,depthTest:false}));
    line.computeLineDistances(); line.renderOrder=800; line.userData=userData; root.add(line); return line; }
  const isDup = (s) => s.obc==='Surface' && s.obc_obj && s.name > s.obc_obj;  // one of each reciprocal pair
  SURF.forEach(s=>{
    const zone=s.zone||'?', fi=zoneFloor[zone] ?? nearestBase(zmin(s),BASES);
    // FLAT (unlit) fill so every face of a zone renders the EXACT same colour — no
    // lighting wash that made horizontal (roof/floor) faces read near-white. Edges keep form.
    const m=new THREE.MeshBasicMaterial({side:THREE.DoubleSide, transparent:true, opacity:1});
    const parts = Object.prototype.hasOwnProperty.call(WALL_PARTS,s.name) ? WALL_PARTS[s.name] : [s];
    parts.forEach(part=>{
      const dup=part.duplicate_at_rest ?? isDup(s);
      const enclosureCondition=part.enclosure_condition || 'physical';
      // Unknown patches are represented by the enclosure-region helper below.
      // Skipping their projected wall part prevents two coincident viewer faces;
      // the authoritative source boundary and display projection remain untouched.
      if(enclosureCondition==='unknown') return;
      const mesh=new THREE.Mesh(ringGeom(part.verts,part.holes||[]), m.clone());
      mesh.userData={zone, floor:fi, type:s.type||'Wall', name:s.name, kind:'surface', dup,
        publicName:part.public_name, draftName:part.draft_public_name, parentName:s.public_name, area:faceArea(part.verts,part.holes),
        occludedBy:part.occluded_by ?? (s.obc_obj ? [_surfZoneByName[s.obc_obj]] : undefined)};
      surfMeshes.push(mesh); root.add(mesh);
      registerSide(mesh,part.verts,part.holes||[],s);
      const segments=part.edge_segments ?? [part.verts,...(part.holes||[])].flatMap(r=>r.map((v,i)=>[v,r[(i+1)%r.length]]));
      const em=new THREE.LineSegments(segmentGeom(segments), new THREE.LineBasicMaterial({color:0x303030}));
      em.userData={...mesh.userData}; edgeSegs.push(em); root.add(em);
      surfaceEdges.push({segments,ranks:part.edge_ranks,userData:mesh.userData});
    });
    logicalLines.push(dashedEdge(s.verts,LOGICAL_COLOR,radius*0.018,radius*0.012,
      {zone,floor:fi,kind:'logical',dup:false}));
    m.dispose();
  });
  const enclosureRegionKeys=new Map();
  function enclosureRegionKey(ring){
    return ring.map(v=>v.map(x=>Math.round(x*1e6)/1e6).join(',')).sort().join('|');
  }
  ENC_REGIONS.forEach(r=>{
    if(!(r.verts||[]).length) return;
    const sourceBoundary=SOURCE_BOUNDARIES[r.boundary_id] || {};
    const zone=r.space_id || sourceBoundary.space_id || '?';
    const fi=zoneFloor[zone] ?? nearestBase(Math.min(...r.verts.map(v=>v[2])),BASES);
    const key=enclosureRegionKey(r.verts), owners=enclosureRegionKeys.get(key)||[], duplicate=owners.length>0;
    enclosureRegionKeys.set(key,[...owners,zone]);
    const userData={zone,floor:fi,kind:'enclosure-region',condition:r.condition,dup:duplicate,
      occludedBy:owners,boundaryId:r.boundary_id,publicName:r.public_name,draftName:r.draft_public_name,parentName:r.parent_name,
      area:faceArea(r.verts,r.holes),baseColor:ENCLOSURE_COLOR};
    // This translucent face is a viewer-only aid. It makes an open extent easy
    // to see without restoring a physical wall, changing volume, or connectivity.
    const material=new THREE.MeshBasicMaterial({color:ENCLOSURE_COLOR,side:THREE.DoubleSide,
      transparent:true,opacity:ENCLOSURE_OPACITY,depthWrite:false});
    const mesh=new THREE.Mesh(ringGeom(r.verts,r.holes||[]),material); mesh.userData=userData;
    enclosureMeshes.push(mesh); root.add(mesh);
    const rings=[r.verts,...(r.holes||[])];
    rings.forEach(ring=>enclosureLines.push(dashedEdge(ring,ENCLOSURE_COLOR,radius*0.012,radius*0.008,{...userData})));
    surfaceEdges.push({segments:rings.flatMap(ring=>ring.map((v,i)=>[v,ring[(i+1)%ring.length]])),
      ranks:r.edge_ranks,userData});
  });
  WINS.forEach(w=>{
    const zone=zoneOfWindow(w), sv=popOut(w.verts, zone);  // proud of wall → clean + pickable
    const m=new THREE.MeshStandardMaterial({color:WINDOW_COLOR, side:THREE.DoubleSide, roughness:0.4});
    const mesh=new THREE.Mesh(ringGeom(sv), m);
    mesh.userData={zone, floor:zoneFloor[zone]??nearestBase(Math.min(...w.verts.map(v=>v[2])),BASES), name:w.name, publicName:w.public_name, draftName:w.draft_public_name, parentName:w.parent_name, host:w.parent, area:faceArea(w.verts), kind:'window', dup:false};
    mesh.modelMaterial=m;
    winMeshes.push(mesh); root.add(mesh);
    // Comparison contours use the exact source geometry, without the picking offset.
    const edge=new THREE.LineSegments(edgeGeom(w.verts),new THREE.LineBasicMaterial({color:0x087d3b}));
    edge.userData={...mesh.userData}; windowEdges.push(edge); root.add(edge);
  });
  OPENS.forEach(o=>{
    const zone=_surfZoneByName[o.parent], dup=Boolean(o.partner && o.name>o.partner);
    const color=o.kind==='door'?0xa5672a:0x16877b;
    const m=new THREE.MeshBasicMaterial({color,side:THREE.DoubleSide,transparent:true,
      opacity:o.state==='closed'?0.7:0.12,depthWrite:false});
    const mesh=new THREE.Mesh(ringGeom(o.verts),m);
    mesh.userData={zone,floor:zoneFloor[zone]||0,name:o.name,
      occludedBy:dup ? [o.other_space_id] : [],
      kind:'opening',dup,baseColor:color,publicName:o.public_name,draftName:o.draft_public_name,
      parentName:o.parent_name,host:o.parent,sourceId:o.source_opening_id,area:faceArea(o.verts)};
    mesh.modelOpacity=m.opacity;
    openingMeshes.push(mesh);root.add(mesh);
    registerSide(mesh,o.verts,[],surfaceByName.get(o.parent));
    const em=new THREE.LineSegments(edgeGeom(o.verts),new THREE.LineBasicMaterial({color}));
    em.userData={...mesh.userData};edgeSegs.push(em);root.add(em);
  });
  const allMeshes = () => surfMeshes.concat(winMeshes,openingMeshes,enclosureMeshes);
  const allPickables = () => allMeshes().concat(enclosureLines);
  // Keep both authored sides selectable at a shared wall or slab. Exact peers swap
  // which face is drawn. A partial counterpart only covers its own contact polygon.
  const pairedSides=[];
  const peersByRing=new Map();
  for(const face of sideFaces){
    const peers=peersByRing.get(face.key)||[];
    peers.push(face.mesh);peersByRing.set(face.key,peers);
  }
  for(const {mesh,key,host} of sideFaces){
    if(!mesh.userData.dup || !mesh.userData.occludedBy?.length) continue;
    const ring=host.verts,normal=new THREE.Vector3(), p=new THREE.Vector3(), q=new THREE.Vector3(), part=new THREE.Vector3();
    const origin=new THREE.Vector3(...ring[0]);
    for(let i=1;i+1<ring.length;i++){
      p.set(...ring[i]).sub(origin);q.set(...ring[i+1]).sub(origin);normal.add(part.crossVectors(p,q));
    }
    if(!normal.lengthSq()) continue;
    const owners=mesh.userData.occludedBy.map(id=>zoneForSpace[id]||id);
    const peers=peersByRing.get(key).filter(m=>!m.userData.dup && owners.includes(m.userData.zone));
    pairedSides.push({mesh,owners,peer:peers.length===1 ? peers[0] : null,
      plane:new THREE.Plane().setFromNormalAndCoplanarPoint(normal.normalize(),origin),enabled:false});
  }
  sideFaces.length=0;peersByRing.clear();
  function faceCamera(){
    for(const side of pairedSides){
      if(!side.enabled) continue;
      const m=side.mesh, inside=side.plane.distanceToPoint(camera.position)-side.plane.normal.dot(m.position)<0;
      m.visible=inside;
      if(side.peer) side.peer.visible=!inside;
    }
  }
  controls.addEventListener('change',faceCamera);

  // The admitted mesh is an offline reference, never a source-BIM entity or pick target.
  const referenceMeshes=[], referenceGroup=new THREE.Group(); scene.add(referenceGroup);
  const reference=GEO.textured_mesh;
  $('meshControls').hidden=!reference;
  let textureFailures=0;
  if(reference){
    const loader=new THREE.TextureLoader();
    const textures=reference.textures.map(uri=>{
      const t=loader.load(uri,undefined,undefined,()=>{textureFailures++; applyFilter();});
      t.wrapS=t.wrapT=THREE.RepeatWrapping;
      // Payload RGB already includes the linear base-colour factor and is sRGB encoded.
      // The existing unlit viewer uses LinearEncoding for both maps and output.
      t.encoding=THREE.LinearEncoding; return t;
    });
    reference.parts.forEach(part=>{
      const geometry=new THREE.BufferGeometry();
      geometry.setAttribute('position',new THREE.Float32BufferAttribute(part.positions,3));
      geometry.setAttribute('uv',new THREE.Float32BufferAttribute(part.uv,2));
      geometry.setIndex(part.indices);
      const material=new THREE.MeshBasicMaterial({map:textures[part.texture],side:THREE.DoubleSide,
        polygonOffset:true,polygonOffsetFactor:1,polygonOffsetUnits:1});
      const mesh=new THREE.Mesh(geometry,material);
      mesh.userData={alphaMode:part.alpha_mode,alphaFactor:part.alpha_factor};
      referenceMeshes.push(mesh); referenceGroup.add(mesh);
    });
  }
  const meshActive = () => Boolean(!confidenceView && reference && $('showMesh').checked && parseFloat($('explode').value)===0);
  function storeyPlanes(){
    const floor=sourceFloor(parseInt($('floorSel').value,10));
    if(floor && Number.isFinite(floor.height) && parseFloat($('explode').value)===0){
      const bottom=floor.z_floor, top=bottom+floor.height;
      // A single textured primitive can span both limits and still contain
      // a triangle exactly on either cap. Allow for camera-space rounding
      // when the GPU evaluates such triangles, even at ordinary BIM origins.
      const viewTolerance=Math.min(0.0005,Math.max(0.00001,radius*4e-6));
      const tolerance=z=>Math.max(viewTolerance,Math.abs(z)*2e-7);
      return [new THREE.Plane(new THREE.Vector3(0,0,1),-bottom+tolerance(bottom)),
        new THREE.Plane(new THREE.Vector3(0,0,-1),top+tolerance(top))];
    }
    return [];
  }
  let pickingPlanes=[];
  const comparisonPlanes=()=>pickingPlanes;
  function clippingForGeometry(geometry,sections,storey){
    if(!storey.length) return sections;
    if(!geometry.boundingBox) geometry.computeBoundingBox();
    const {min,max}=geometry.boundingBox, [bottom,top]=storey;
    // At zero explode the geometry is in model coordinates. A shader plane
    // coincident with a slab can discard alternating fragments as the camera
    // moves. Only clip across a storey limit if this geometry extends past it.
    const below=min.z < -bottom.constant, above=max.z > top.constant;
    if(below && above) return sections.concat(storey);
    if(below) return sections.concat(bottom);
    if(above) return sections.concat(top);
    return sections;
  }
  function applyClipping(){
    const sections=activePlanes(), storey=storeyPlanes();
    pickingPlanes=sections.concat(storey);
    for(const obj of allMeshes().concat(referenceMeshes,edgeSegs,logicalLines,enclosureLines,windowEdges)){
      obj.material.clippingPlanes=clippingForGeometry(obj.geometry,sections,storey);
      obj.material.needsUpdate=true;
      if(obj.selectionOutline) obj.selectionOutline.material.clippingPlanes=obj.material.clippingPlanes;
    }
  }

  // ---- select-by modes + uniform-colour selection ----
  let selected=new Set(); const selGroup=new THREE.Group(); scene.add(selGroup);
  function applyOpacity(){
    const value=Number($('opacity').value);
    function setAlpha(mesh,alpha,depthWrite=alpha===1){
      mesh.material.opacity=alpha;
      mesh.material.transparent=alpha<1;
      mesh.material.depthWrite=depthWrite;
    }
    surfMeshes.forEach(m=>setAlpha(m,value,!confidenceView || value===1));
    winMeshes.forEach(m=>{
      if(confidenceView && !m.confidenceMaterial) m.confidenceMaterial=new THREE.MeshBasicMaterial({side:THREE.DoubleSide});
      m.material=confidenceView ? m.confidenceMaterial : m.modelMaterial;
      if(confidenceView) setAlpha(m,value);
    });
    openingMeshes.forEach(m=>setAlpha(m,confidenceView ? value : m.modelOpacity));
    enclosureMeshes.forEach(m=>setAlpha(m,confidenceView ? value : ENCLOSURE_OPACITY));
  }
  function refreshColors(){
    const mode=$('colorBy').value;
    surfMeshes.forEach(m=>{ let c; if(mode==='floor') c=FLOOR_COLORS[m.userData.floor%FLOOR_COLORS.length];
      else if(mode==='zone') c=roleColor(m.userData.zone);   // colour by room type
      else if(mode==='edge') c=WHITE; else c=TYPE_COLORS[m.userData.type] ?? 0xcccccc;
      m.userData.baseColor=c; });
    winMeshes.forEach(m=>m.userData.baseColor=WINDOW_COLOR);
    enclosureMeshes.forEach(m=>m.userData.baseColor=ENCLOSURE_COLOR);
    for(const m of allPickables()){
      const u=m.userData;
      const color=confidenceView ? GRADE_COLORS[spatialRecord(mode,u)?.grade || 0] : u.baseColor;
      const highlighted=editView && editMarkers.highlighted.has(m);
      const inspectedColor=selected.has(m) && (!confidenceView || !m.isMesh) ? SEL_COLOR : color;
      m.material.color.setHex(highlighted ? 0xe43c44 : inspectedColor);
      if(!confidenceView && m.isMesh && (u.kind==='opening' || u.kind==='enclosure-region')){
        const alpha=u.kind==='opening' ? m.modelOpacity : ENCLOSURE_OPACITY;
        m.material.opacity=highlighted ? Math.max(alpha,.74) : alpha;
      }
    }
    updateLegend(mode);
  }
  function clearSelGroup(){ while(selGroup.children.length){const line=selGroup.children[0];
    selGroup.remove(line);line.geometry.dispose();line.material.dispose();} }
  function selectionOutlines(){
    for(const m of selected){
      if(!confidenceView || !m.isMesh) continue;
      const line=new THREE.LineSegments(new THREE.EdgesGeometry(m.geometry),
        new THREE.LineBasicMaterial({color:SEL_COLOR,transparent:true,depthWrite:false,clippingPlanes:m.material.clippingPlanes}));
      line.renderOrder=890;m.add(line);m.selectionOutline=line;
    }
  }
  function clearOutlines(){
    for(const m of selected) if(m.selectionOutline){
      const line=m.selectionOutline;m.remove(line);line.geometry.dispose();line.material.dispose();delete m.selectionOutline;
    }
  }
  let inspected=null;
  function setSelection(arr){ clearSelGroup();clearOutlines();selected=new Set(arr);
    refreshColors();selectionOutlines();
  }
  function clearSelection(){ setSelection([]); inspected=null; renderInspection(); }

  // ---- measure (button; CONTINUOUS; CAD-style screen-space vertex snap on true geometry) ----
  let measuring=false, measurePts=[],measureLine=null;
  const mGroup=new THREE.Group();scene.add(mGroup);
  // overlay helpers: always-on-top (depthTest off + depthWrite off + high renderOrder) so a
  // highlight is NEVER hidden behind the (transparent) faces — no need to rotate to see it.
  function topMat(color){ return new THREE.MeshBasicMaterial({color, depthTest:false, depthWrite:false, transparent:true}); }
  const markerGeometry=new THREE.SphereGeometry(1,24,16);
  function measureBall(name,color){
    const ball=new THREE.Mesh(markerGeometry,topMat(color));
    ball.name=name;ball.visible=false;ball.renderOrder=1000;
    mGroup.add(ball);return ball;
  }
  // a THICK highlight line as a thin cylinder — WebGL ignores LineMaterial.linewidth, so a
  // real tube is the only way to get a visibly bold line that reads at any camera angle.
  function setLineEndpoints(line,a,b){
    const dir=new THREE.Vector3().subVectors(b,a);
    line.scale.y=dir.length();line.visible=line.scale.y>1e-7;
    line.position.copy(a).addScaledVector(dir,.5);
    if(line.visible) line.quaternion.setFromUnitVectors(new THREE.Vector3(0,1,0),dir.normalize());
  }
  function fatLine(a,b,color,r){
    const line=new THREE.Mesh(new THREE.CylinderGeometry(r,r,1,8),topMat(color));
    line.renderOrder=999;setLineEndpoints(line,a,b);return line;
  }
  const snap=measureBall('vertex-snap',0x007e89);
  const endpoints=[measureBall('measure-start',0xcd165b),measureBall('measure-end',0xcd165b)];
  const viewPoint=new THREE.Vector3();
  function sizeMeasureMarkers(){
    if(!measuring) return;
    camera.updateMatrixWorld();
    const viewHeight=Math.max(1,viewport.clientHeight);
    const pixelRadius=1/(viewHeight*camera.projectionMatrix.elements[5]);
    const touch=viewport.clientWidth<560||matchMedia('(pointer: coarse)').matches;
    for(const ball of mGroup.children){
      if(!ball.visible) continue;
      const depth=Math.max(camera.near,-viewPoint.copy(ball.position).applyMatrix4(camera.matrixWorldInverse).z);
      const unit=depth*pixelRadius;
      if(ball===measureLine){
        // Cylinder height is world-space length. Only its two radial axes follow zoom.
        ball.scale.x=ball.scale.z=3*unit;
      }else{
        const diameter=ball===snap ? (touch?18:15) : (touch?15:12);
        ball.scale.setScalar(diameter*unit);
      }
    }
  }
  controls.addEventListener('change',sizeMeasureMarkers);
  function positionMeasurePair(){
    measurePts.forEach((vertex,i)=>{
      endpoints[i].position.copy(vertex.v).add(explodeOffset(vertex.zone));
      endpoints[i].visible=true;
    });
    if(measurePts.length===2){
      const [a,b]=endpoints;
      if(measureLine) setLineEndpoints(measureLine,a.position,b.position);
      else {measureLine=fatLine(a.position,b.position,0xcd165b,1);mGroup.add(measureLine);}
    }
    sizeMeasureMarkers();
  }
  const CAND=[];  // {v: true world Vector3, zone}
  SURF.forEach(s=>s.verts.forEach(v=>CAND.push({v:new THREE.Vector3(v[0],v[1],v[2]), zone:s.zone||'?'})));
  WINS.forEach(w=>{const z=zoneOfWindow(w); w.verts.forEach(v=>CAND.push({v:new THREE.Vector3(v[0],v[1],v[2]), zone:z}));});
  OPENS.forEach(o=>o.verts.forEach(v=>CAND.push({v:new THREE.Vector3(...v),zone:_surfZoneByName[o.parent]})));
  function clearPair(){
    if(measureLine){
      mGroup.remove(measureLine);measureLine.geometry.dispose();measureLine.material.dispose();measureLine=null;
    }
    endpoints.forEach(ball=>{ball.visible=false;});measurePts=[];
  }
  function clearMeasure(){ measuring=false; snap.visible=false; clearPair();
    $('meas').style.display='none'; renderer.domElement.style.cursor='default'; }
  function startMeasure(){ clearMeasure();clearSelection();measuring=true;renderer.domElement.style.cursor='crosshair';
    $('meas').style.display='block'; renderMeasurement(); }
  function renderMeasurement(){
    if(!measuring) return;
    const distance=measurePts.length===2 ? measurePts[0].v.distanceTo(measurePts[1].v) : null;
    const card=$('meas');
    card.classList.toggle('has-distance',distance!==null);
    if(distance===null){
      card.textContent=measurePts.length ? msg('Select the second vertex') : msg('Select the first vertex');
      return;
    }
    card.innerHTML='<span class="measure-label">'+esc(msg('Distance'))+'</span>'+
      '<strong class="measure-value">'+distance.toFixed(3)+' <small>m</small></strong>';
  }
  // Picking follows wall opacity: when walls are OPAQUE you can only snap/select what is visibly
  // in front (a candidate hidden behind a face is skipped); turn DOWN wall opacity to reach the
  // geometry behind. Keeps the precise screen-nearest aim within the tolerance.
  const _ray=new THREE.Raycaster();
  function occluded(w){ const from=camera.position; const dir=w.clone().sub(from); const dist=dir.length()||1e-6;
    _ray.set(from, dir.divideScalar(dist)); _ray.near=0; _ray.far=dist-radius*0.01;   // only faces strictly IN FRONT of w
    return _ray.intersectObjects(surfMeshes.filter(m=>m.visible), false)
      .some(h=>comparisonPlanes().every(p=>p.distanceToPoint(h.point)>=-0.0001)); }
  function seeThrough(){ return parseFloat($('opacity').value) < 1; }   // transparent → allowed to reach behind
  function snapPick(cx, cy){ const r=renderer.domElement.getBoundingClientRect(); const thru=seeThrough();
    let best=null, bd=24*24;
    for(const c of CAND){ const w=c.v.clone().add(explodeOffset(c.zone)); const p=w.clone().project(camera);
      if(meshActive() && $('meshMode').value==='mesh') continue;
      if(!onFloor({zone:c.zone,floor:zoneFloor[c.zone]},parseInt($('floorSel').value,10))) continue;
      if(!comparisonPlanes().every(plane=>plane.distanceToPoint(w)>=-0.0001)) continue;
      if(p.z<-1||p.z>1) continue; const sx=(p.x*0.5+0.5)*r.width, sy=(-p.y*0.5+0.5)*r.height;
      const d=(sx-cx)**2+(sy-cy)**2; if(d<bd && (thru || !occluded(w))){ bd=d; best=c; } }
    return best; }

  // ---- edge selection on true geometry ----
  const EDGES=[];
  function pushRingEdges(obj,userData){
    obj.verts.forEach((v,i)=>EDGES.push({...userData,
      a:new THREE.Vector3(...v),b:new THREE.Vector3(...obj.verts[(i+1)%obj.verts.length]),
      edgeRank:obj.edge_ranks[i]}));
  }
  surfaceEdges.forEach(({segments,ranks,userData})=>segments.forEach(([a,b],i)=>{
    EDGES.push({...userData,a:new THREE.Vector3(...a),b:new THREE.Vector3(...b),edgeRank:ranks[i]});
  }));
  WINS.forEach((w,i)=>pushRingEdges(w,winMeshes[i].userData));
  OPENS.forEach((o,i)=>pushRingEdges(o,openingMeshes[i].userData));
  // only pick an edge whose source face is currently shown (same predicate as applyFilter)
  function edgeVisible(e){ const f=parseInt($('floorSel').value,10); const exploded=parseFloat($('explode').value)>0;
    if(meshActive() && ($('meshMode').value==='mesh'||!$('showEdges').checked)) return false;
    if(!partVisible(e,f,exploded)) return false;
    if(e.kind==='enclosure-region') return layerVisible('showEnclosure');
    return layerVisible(e.kind==='window' ? 'showWin' : e.kind==='opening' ? 'showOpen' : 'showWalls'); }
  function segDist(px,py,ax,ay,bx,by){ const dx=bx-ax,dy=by-ay, L2=dx*dx+dy*dy||1;
    let t=((px-ax)*dx+(py-ay)*dy)/L2; t=Math.max(0,Math.min(1,t));
    return (px-(ax+t*dx))**2 + (py-(ay+t*dy))**2; }
  function edgePick(ev){ const r=renderer.domElement.getBoundingClientRect();
    const cx=ev.clientX-r.left, cy=ev.clientY-r.top; const thru=seeThrough(); let best=null, bd=18*18;
    for(const e of EDGES){ if(!edgeVisible(e)) continue; const o=explodeOffset(e.zone);
      const A=e.a.clone().add(o), B=e.b.clone().add(o);
      if(!comparisonPlanes().every(p=>p.distanceToPoint(A)>=-0.0001 && p.distanceToPoint(B)>=-0.0001)) continue;
      const pa=A.clone().project(camera), pb=B.clone().project(camera);
      if(pa.z<-1||pa.z>1||pb.z<-1||pb.z>1) continue;  // skip if EITHER endpoint is behind/clipped
      const ax=(pa.x*.5+.5)*r.width, ay=(-pa.y*.5+.5)*r.height, bx=(pb.x*.5+.5)*r.width, by=(-pb.y*.5+.5)*r.height;
      const d=segDist(cx,cy,ax,ay,bx,by);   // opaque: skip an edge hidden behind a face (use its midpoint)
      if(d<bd && (thru || !occluded(A.clone().add(B).multiplyScalar(0.5)))){ bd=d; best={e,A,B}; } }
    clearSelection(); if(!best) return;
    selGroup.add(fatLine(best.A, best.B, SEL_COLOR, radius*0.006));   // whole edge as one thick, always-on-top line
    showInspection('edge',best.e); }

  // ---- selection picking (face raycast, for click-select only) ----
  const raycaster=new THREE.Raycaster();
  raycaster.params.Line.threshold=radius*0.012;
  function pick(ev){ const r=renderer.domElement.getBoundingClientRect();
    const mouse=new THREE.Vector2(((ev.clientX-r.left)/r.width)*2-1, -((ev.clientY-r.top)/r.height)*2+1);
    raycaster.setFromCamera(mouse,camera);
    const hits=raycaster.intersectObjects(allPickables().filter(m=>m.visible),false)
      .filter(h=>comparisonPlanes().every(p=>p.distanceToPoint(h.point)>=-0.0001));
    if(!hits.length) return null;
    return hits.find(h=>h.distance<=hits[0].distance+0.00001 && h.object.material.polygonOffset) || hits[0]; }
  const openingByName=new Map(OPENS.map(o=>[o.name,o]));
  function surfaceBinding(u){
    if(u.kind==='opening') return ['openings',u.sourceId || SOURCE_MAP.openings?.[u.name] || u.name];
    if(u.kind==='window') return ['openings',SOURCE_MAP.windows?.[u.name] || u.name];
    return ['boundaries',u.boundaryId || SOURCE_MAP.surfaces?.[u.name] || u.name];
  }
  function spatialRecord(mode,u){
    const spatial=GEO.inspection.spatial;
    if(mode==='zone' || mode==='floor') return spatial.spaces?.[sourceIdOfZone(u.zone)];
    const [group,id]=surfaceBinding(u);
    return spatial[group]?.[id];
  }
  function confidenceCard(mode,u){
    const record=spatialRecord(mode,u), grade=record?.grade || 0;
    const label=(grade ? grade+' · ' : '')+msg(GRADE_LABELS[grade]);
    const head='<span>'+esc(msg('Spatial confidence'))+'</span><b><i style="background:'+hex6(GRADE_COLORS[grade])+'"></i>'+esc(label)+'</b>';
    if(!record) return '<div class="confidence-row">'+head+'</div>';
    return '<details class="confidence-reason"'+(inspected.reasonOpen?' open':'')+'><summary class="confidence-row" title="'+
      esc(msg('Evidence and reasoning'))+'">'+head+'</summary><p>'+esc(record.reason[locale==='en'?'en':'zh'])+'</p></details>';
  }
  function regionalThicknesses(attributes,point){
    const values=attributes?.thicknesses || [];
    if(!point || !attributes?.regions?.length) return values;
    const indices=new Set();
    for(const r of attributes.regions){
      const axis=projectionAxis(r.vertices);
      const [p]=projectRing([point],axis);
      function contains(ring){
        const v=projectRing(ring,axis);let inside=false;
        for(let i=0,j=v.length-1;i<v.length;j=i++){
          const a=v[j],b=v[i];
          if(segDist(p[0],p[1],a[0],a[1],b[0],b[1])<1e-12) return 0;
          if((b[1]>p[1])!==(a[1]>p[1]) && p[0]<(a[0]-b[0])*(p[1]-b[1])/(a[1]-b[1])+b[0]) inside=!inside;
        }
        return inside ? 1 : -1;
      }
      if(contains(r.vertices)>=0 && !r.holes.some(h=>contains(h)>0)) indices.add(r.index);
    }
    return indices.size ? [...indices].map(i=>values[i]) : values;
  }
  function surfaceAttributes(u,point){
    const [group,id]=surfaceBinding(u), attrs=GEO.inspection.properties[group]?.[id];
    const virtual=u.kind==='enclosure-region' || (u.kind==='opening' && component(u)==='Passage') ||
      SOURCE_BOUNDARIES[id]?.kind==='virtual' || attrs?.applicable===false;
    const fallback=msg(virtual ? 'Not applicable' : 'Not recorded');
    function field(label,value,source){
      const tag=source ? '<small class="source-tag">'+esc(source)+'</small>' : '';
      return '<div class="surface-attribute"><dt><span>'+esc(msg(label))+'</span>'+tag+'</dt><dd>'+
        esc(value)+'</dd></div>';
    }
    const material=!virtual && attrs?.material;
    const thicknesses=virtual ? [] : regionalThicknesses(attrs,point);
    const thickness=thicknesses.map(t=>field('Thickness',
      t.value_mm.toLocaleString(locale,{maximumFractionDigits:6})+' mm',t.source)).join('');
    return '<dl class="surface-attributes">'+
      field('Material',material ? material[locale==='en'?'en':'zh'] : fallback,material?.source)+
      (thickness || field('Thickness',fallback))+'</dl>';
  }
  function component(u){
    const key=u.name;
    const surface=surfaceByName.get(key);
    if(u.kind==='window') return 'Window';
    if(u.kind==='opening') return openingByName.get(key)?.kind==='door' ? 'Door' : 'Passage';
    if(u.kind==='enclosure-region') return u.condition==='open' ? 'Open boundary' : 'Unknown enclosure';
    const type=u.type || surface?.type;
    if(type==='Wall') return surface?.obc==='Outdoors' ? 'Exterior wall' : (surface?.obc==='Surface' ? 'Interior wall' : 'Wall');
    return ({Floor:'Floor slab',Roof:'Roof',Ceiling:'Ceiling'})[type] || 'Boundary';
  }
  function declaredStoreys(zone){
    const floors=zoneFloors[zone];
    if(floors.length<2 || !verticalCore(zone)) return floors;
    const space=SOURCE_SPACES[(SOURCE_MAP.zones||{})[zone]||zone];
    if(!space) return floors;
    // A vertical core can own a full-height source group in addition to its declared storeys.
    return floors.filter(i=>{const f=sourceFloor(i);
      return f?.id!==space.floor_id || ZONES.some(z=>zoneFloor[z]===i && !verticalCore(z)) ||
        !floors.some(j=>j!==i && sourceFloor(j)?.height < f.height-0.001);
    });
  }
  const storeysByZone=Object.fromEntries(Object.keys(zoneFloors).map(z=>[z,declaredStoreys(z)]));
  const actualStoreys=(zone,fallback)=>storeysByZone[zone]||[fallback];
  function selectionFloors(mode,item,u){
    const floors=actualStoreys(u.zone,u.floor);
    if(mode==='zone' || floors.length<2) return floors;
    if(!item.geometry) return floors;
    if(!item.geometry.boundingBox) item.geometry.computeBoundingBox();
    const {min,max}=item.geometry.boundingBox, low=min.z, high=max.z;
    const overlap=floors.filter(i=>{const f=sourceFloor(i);return f && low<f.z_floor+f.height-0.001 && high>f.z_floor+0.001;});
    if(overlap.length) return overlap;
    const touching=floors.filter(i=>{const f=sourceFloor(i);return f && low<=f.z_floor+f.height+0.001 && high>=f.z_floor-0.001;});
    return touching.length ? touching : floors;
  }
  function showInspection(mode,item,floor,point){inspected={mode,item,floor,point,reasonOpen:false};renderInspection();}
  function renderInspection(){
    const el=$('sel'); el.style.display=inspected ? 'block' : 'none';
    if(!inspected){el.replaceChildren();return;}
    const {mode,item,floor}=inspected, u=item.userData || item;
    let name, fields=[], quantities;
    const storeys=()=>selectionFloors(mode,item,u).map(floorLabel).join(' / ');
    if(mode==='zone'){
      name=spaceName(u.zone);
      fields=[['Storey',storeys()],['Function',roleLabel(roleOf(u.zone))]];
      quantities=[['Modeled volume',GEO.metrics.spaces[sourceIdOfZone(u.zone)]?.volume_m3,'m³']];
    }else if(mode==='floor'){
      name=floorLabel(floor);
      const metrics=GEO.metrics.floors[NAMED_FLOORS[floor]?.id] || {};
      fields=[['Space count',metrics.space_count]];
      quantities=[['Elevation',BASES[floor],'m'],['Storey volume',metrics.volume_m3,'m³']];
    }else if(mode==='surface'){
      name=u.publicName;
      fields=[['Type',msg(component(u))],['Parent face',u.parentName!==name && u.parentName],
        ['Room',spaceName(u.zone)],['Storey',storeys()]];
      quantities=[['Face area',u.area,'m²']];
    }else{
      name=u.publicName+'_Edge'+u.edgeRank;
      quantities=[['Length',u.a.distanceTo(u.b),'m']];
    }
    el.dataset.mode=mode;
    const rows=fields.filter(([,v])=>v!==undefined && v!==null && v!==false && v!=='')
      .map(([k,v])=>row(esc(msg(k)),esc(v))).join('');
    const metrics=quantities.filter(([,v])=>Number.isFinite(v)).map(([key,value,unit])=>{
      const number=value.toLocaleString(locale,{minimumFractionDigits:2,maximumFractionDigits:2});
      return '<div class="pick-metric"><span>'+esc(msg(key))+'</span><b>'+number+' <small>'+unit+'</small></b></div>';
    }).join('');
    el.innerHTML='<header class="pick-head"><span class="pick-kind">'+esc(msg(mode))+'</span><strong class="pick-name">'+
      esc(name)+'</strong></header><div class="pick-fields">'+rows+'</div><div class="pick-metrics">'+metrics+'</div>'+
      (mode==='surface' ? surfaceAttributes(u,inspected.point) : '')+
      (mode==='surface'||mode==='zone' ? confidenceCard(mode,u) : '');
    const reason=el.querySelector('.confidence-reason');
    if(reason) reason.ontoggle=()=>{if(inspected && reason.isConnected) inspected.reasonOpen=reason.open;};
  }
  function pickedFloor(hit){
    const mesh=hit.object,u=mesh.userData;
    const floors=actualStoreys(u.zone,u.floor), active=Number($('floorSel').value);
    const z=hit.point.z-mesh.position.z;
    return floors.includes(active) ? active : (floors.find(i=>{const f=sourceFloor(i);
      return f && z>=f.z_floor-0.001 && z<f.z_floor+f.height-0.001;
    }) ?? floors[floors.length-1]);
  }
  function handleClick(ev){
    if(measuring){ const r=renderer.domElement.getBoundingClientRect();
      const p=snapPick(ev.clientX-r.left, ev.clientY-r.top); if(!p) return;
      if(measurePts.length>=2) clearPair();  // continuous: a new click starts a fresh pair (only latest shown)
      snap.visible=false;measurePts.push(p);positionMeasurePair();
      renderMeasurement();
      return; }
    const mode=$('colorBy').value;
    if(editView){editMarkers.pickTarget(pick(ev),mode,ev.ctrlKey || ev.metaKey);return;}
    if(mode==='edge'){ edgePick(ev); return; }
    const hit=pick(ev); if(!hit){ clearSelection(); return; }
    const o=hit.object;
    let sel, floor;
    if(mode==='floor'){
      floor=pickedFloor(hit);
      sel=allPickables().filter(m=>actualStoreys(m.userData.zone,m.userData.floor).includes(floor));
    }else if(mode==='zone') sel=allPickables().filter(m=>m.userData.zone===o.userData.zone); else sel=[o];
    setSelection(sel); showInspection(mode,o,floor,hit.point.clone().sub(o.position).toArray());
  }
""" + ANNOTATION_JS + r"""
  // click vs orbit-drag: only a near-stationary press is a click
  let downXY=null;
  renderer.domElement.addEventListener('pointerdown', e=>{ if(e.button===0) downXY=[e.clientX,e.clientY]; });
  renderer.domElement.addEventListener('pointerup', e=>{ if(e.button!==0||!downXY) return;
    const moved=Math.hypot(e.clientX-downXY[0], e.clientY-downXY[1]); downXY=null; if(moved<=5) handleClick(e); });
  renderer.domElement.addEventListener('pointermove', e=>{ if(!measuring) return;
    const r=renderer.domElement.getBoundingClientRect(); const p=snapPick(e.clientX-r.left, e.clientY-r.top);
    if(p){ snap.position.copy(p.v).add(explodeOffset(p.zone));snap.visible=true;sizeMeasureMarkers(); } else snap.visible=false; });
  renderer.domElement.addEventListener('pointerleave',()=>{snap.visible=false;});

  // ---- display toggles + explode ----
  // At explode=0 the duplicate of each coincident reciprocal pair is HIDDEN (solid
  // clean shell, no gaps, no z-fight); at explode>0 ALL faces show (each zone a
  // closed box) separated by the offset.
  function layerVisible(id){
    const input=$(id);
    return confidenceView ? input.defaultChecked : input.checked;
  }
  function applyFilter(){ const f=parseInt($('floorSel').value,10);
    const exploded=parseFloat($('explode').value)>0;
    const comparison=meshActive(), mode=$('meshMode').value;
    const fills=!comparison || mode==='blend', contours=!comparison || mode!=='mesh';
    const sw=layerVisible('showWalls'), swin=layerVisible('showWin'), se=layerVisible('showEdges');
    const openings=layerVisible('showOpen'), enclosures=layerVisible('showEnclosure');
    const okF=(u)=>partVisible(u,f,exploded);
    surfMeshes.forEach(m=>m.visible = fills && sw && okF(m.userData));
    winMeshes.forEach(m=>m.visible = fills && swin && okF(m.userData));
    openingMeshes.forEach(m=>m.visible=fills && openings && okF(m.userData));
    for(const side of pairedSides){
      const m=side.mesh,u=m.userData, layer=u.kind==='opening' ? openings : sw;
      side.enabled=fills && layer && onFloor(u,f) && side.owners.some(z=>
        onFloor({zone:z,floor:zoneFloor[z]},f) && (!exploded || m.position.distanceToSquared(explodeOffset(z))<1e-12));
      m.material.polygonOffset=side.enabled && !side.peer;
      m.material.polygonOffsetFactor=-1;m.material.polygonOffsetUnits=-2;
    }
    faceCamera();
    enclosureMeshes.forEach(m=>m.visible=fills && enclosures && okF(m.userData));
    edgeSegs.forEach(e=>{e.visible = contours && se && okF(e.userData) && (e.userData.kind!=='opening'||openings);
      e.material.color.setHex(e.userData.kind==='opening' ? (comparison?0xec820e:e.userData.baseColor) : (comparison?0xc51b80:0x303030));});
    windowEdges.forEach(e=>e.visible=comparison && contours && se && swin && okF(e.userData));
    logicalLines.forEach(e=>e.visible=contours && layerVisible('showLogical') && okF(e.userData));
    enclosureLines.forEach(e=>e.visible=contours && enclosures && okF(e.userData));
    referenceGroup.visible=comparison;
    referenceMeshes.forEach(m=>{const v=parseFloat($('meshOpacity').value)*m.userData.alphaFactor;
      m.material.opacity=v; m.material.transparent=v<1||m.userData.alphaMode==='BLEND';
      m.material.depthWrite=!m.material.transparent;});
    $('meshStatus').textContent=textureFailures?'[[msg:贴图加载失败，请重新打开完整 HTML。]]':
      ($('showMesh').checked&&exploded?'[[msg:展开时暂停贴图叠加]]':
      (comparison?'[[msg:粉色：BIM边界 · 绿色：窗 · 橙色：门；遮挡处可剖切查看。]]':''));
    applyClipping(); }
  function explodeOffset(zone){ const amt=parseFloat($('explode').value); if(amt<=0) return new THREE.Vector3();
    if($('explodeMode').value==='floor') return new THREE.Vector3(0,0, (zoneFloor[zone]||0)*amt*radius*1.0);
    return (zoneDir[zone]||new THREE.Vector3()).clone().multiplyScalar(amt*radius*1.2);  // zone: full 3D radial
  }
  function applyExplode(){ surfMeshes.forEach(m=>m.position.copy(explodeOffset(m.userData.zone)));
    winMeshes.forEach(m=>m.position.copy(explodeOffset(m.userData.zone)));
    openingMeshes.forEach(m=>m.position.copy(explodeOffset(m.userData.zone)));
    enclosureMeshes.forEach(m=>m.position.copy(explodeOffset(m.userData.zone)));
    edgeSegs.concat(logicalLines,enclosureLines).forEach(e=>e.position.copy(explodeOffset(e.userData.zone)));
    if(measuring){snap.visible=false;positionMeasurePair();}
    applyFilter(); }  // re-evaluate dup visibility when crossing explode 0 ↔ >0

  // ---- right-side info (structured) ----
  function row(k,v){ return '<div class="kv"><span>'+k+'</span><b>'+v+'</b></div>'; }
  function updateStats(){
    const count=n=>n.toLocaleString(GEO.locale);
    $('dimensions').innerHTML=row('[[msg:Width]]',size.x.toFixed(2)+' m')+row('[[msg:Depth]]',size.y.toFixed(2)+' m')+
      row('[[msg:Height]]',size.z.toFixed(2)+' m');
    $('elementStats').hidden=!SOURCE;
    if(SOURCE) $('elementCounts').innerHTML=
      row('[[msg:Spaces]]',count(SOURCE.spaces.length))+row('[[msg:Boundaries]]',count(SOURCE.boundaries.length))+
      row('[[msg:Openings]]',count(SOURCE.openings.length))+row('[[msg:Connections]]',count((SOURCE.connections||[]).length))+
      (SOURCE.schema_version==='source_bim_v3' ? row('[[msg:半开敞 / 开敞空间]]',count(SOURCE.spaces.filter(s=>['semi_open','open'].includes(s.enclosure)).length))+
        row('[[msg:开敞 / 未知边界]]',count(SOURCE.boundaries.filter(b=>['open','unknown','mixed'].includes(b.enclosure)).length)) : '');
  }
  updateStats();

  // ---- legends ----
  function updateLegend(mode){
    const el=$('legend'); if(!el) return;
    $('confidenceLegend').hidden=!confidenceView;
    $('confidenceUnit').textContent=msg(mode==='zone'||mode==='floor' ? 'zone' : 'surface');
    $('confidenceScale').innerHTML=[5,4,3,2,1,0].map(g=>'<li><i style="background:'+hex6(GRADE_COLORS[g])+
      '"></i><span>'+(g ? g+' · ' : '')+esc(msg(GRADE_LABELS[g]))+'</span></li>').join('');
    $('legendGroup').hidden=confidenceView || !HAS_ROLES;
    if($('legendGroup').hidden){el.innerHTML='';return;}
    const floor=Number($('floorSel').value);
    const visibleZones=(GEO.zones||[]).filter(z=>onFloor({zone:z,floor:zoneFloor[z]},floor));
    const present=[...new Set(visibleZones.map(z=>roleOf(z)||'untyped'))].sort();
    let h='';
    present.forEach(r=>{ const c=(r==='untyped')?ROLE_DEFAULT:(ROLE_COLORS[r]??ROLE_DEFAULT);
      h+='<div class="lg"><span class="sw" style="background:'+hex6(c)+'"></span>'+esc(roleLabel(r))+'</div>'; });
    el.innerHTML=h;
  }

  // ---- section controls ----
  const secDiv=$('sections');
  AX.forEach(a=>{ const wrap=document.createElement('div'); wrap.className='sec'; const step=((a.max-a.min)/200)||0.01;
    wrap.innerHTML='<label class="axis-label"><input type="checkbox" id="en'+a.key+'"><b>'+a.key+'</b></label>'+
      '<input type="range" aria-label="'+a.key+'" id="pos'+a.key+'" min="'+a.min+'" max="'+a.max+'" step="'+step+'" value="'+a.max+'">'+
      '<label class="chk flip"><input type="checkbox" id="flip'+a.key+'"> <span data-i18n="flip side">[[msg:flip side]]</span></label>';
    secDiv.appendChild(wrap);
    const refresh=()=>{a.enabled=$('en'+a.key).checked; a.flip=$('flip'+a.key).checked; a.pos=parseFloat($('pos'+a.key).value); updatePlane(a); applyClipping();};
    $('en'+a.key).onchange=refresh; $('pos'+a.key).oninput=refresh; $('flip'+a.key).onchange=refresh; });

  // ---- floor select + wiring ----
  const fs=$('floorSel'); BASES.forEach((b,i)=>{
    if(!SURF.some(s=>onFloor({zone:s.zone,floor:zoneFloor[s.zone]},i))) return;
    const o=document.createElement('option'); o.value=i; o.textContent=floorLabel(i); fs.appendChild(o);});
  const viewTabs=[$('modelTab'),$('confidenceTab'),$('editTab')];
  let restoreEdge=false, modelComparisonMode;
  function syncEditPanel(){
    const draft=viewerDraft.state, input=$('editInstructions');
    if(input.value!==draft.instructions) input.value=draft.instructions;
    const count=draft.notes.length;
    $('noteCount').textContent=locale==='en' ? count+(count===1?' note':' notes') : count+' 条批注';
    $('draftStorageStatus').textContent=msg(viewerDraft.persistent
      ? 'Notes are saved in this browser for this model.' : 'Changes are available in this tab only.');
    $('revisionActions').hidden=!viewerDraft.canRevise;
    $('reviewRevision').disabled=!count && !draft.instructions.trim() && draft.northAngle===null;
  }
  function setViewMode(active){
    if(active.getAttribute('aria-selected')==='true') return;
    const confidence=active===$('confidenceTab'), edit=active===$('editTab');
    const opacity=$('opacity');
    if(confidence) modelOpacity=Number(opacity.value);
    if(confidenceView || confidence) opacity.value=confidence ? '0.75' : String(modelOpacity);
    cancelCompassEdit();
    if(edit || editView){
      editMarkers.cancel();clearSelection();clearMeasure();
      const by=$('colorBy'), edge=by.querySelector('option[value="edge"]');
      if(edit){restoreEdge=by.value==='edge';if(restoreEdge) by.value='surface';}
      edge.hidden=edit;edge.disabled=edit;
      if(!edit && restoreEdge) by.value='edge';
      // Edit picks BIM faces. Keep them available whenever the reference mesh is overlaid.
      const meshMode=$('meshMode');
      if(edit){modelComparisonMode=meshMode.value;meshMode.value='blend';}
      else meshMode.value=modelComparisonMode;
      meshMode.disabled=edit;
    }
    confidenceView=confidence;editView=edit;clearOutlines();
    renderer.setTransparentSort(confidence ? sortConfidenceFaces : null);
    showRangeValue(opacity);
    applyOpacity();
    for(const tab of viewTabs){
      tab.setAttribute('aria-selected',String(tab===active));tab.tabIndex=tab===active?0:-1;
    }
    $('stage').setAttribute('aria-labelledby',active.id);
    $('editPanel').hidden=!edit;
    $('measure').hidden=edit;
    $('editHint').hidden=true;
    $('meshControls').hidden=confidence || !reference;
    $('layerControls').hidden=confidence;
    $('sectionControls').hidden=confidence;
    refreshColors();applyFilter();selectionOutlines();editMarkers.sync();syncEditPanel();
  }
  for(const tab of viewTabs){
    tab.onclick=()=>setViewMode(tab);
    tab.onkeydown=event=>{
      if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) return;
      event.preventDefault();
      const n=viewTabs.length;
      const next=event.key==='Home'?0:event.key==='End'?n-1:(viewTabs.indexOf(tab)+(event.key==='ArrowRight'?1:-1)+n)%n;
      viewTabs[next].focus();setViewMode(viewTabs[next]);
    };
  }
  $('colorBy').onchange=()=>{
    if(editView){restoreEdge=false;editMarkers.cancel();}
    clearSelection();if(editView) editMarkers.sync();
  };
  fs.onchange=()=>{if(editView) editMarkers.cancel();clearSelection();clearMeasure();applyFilter();if(editView) editMarkers.sync();};
  $('editInstructions').oninput=e=>viewerDraft.update({instructions:e.target.value});
  $('reviewRevision').onclick=()=>{
    if(editMarkers.pending()){$('editHint').hidden=false;return;}
    viewerDraft.reviewRevision();
  };
  window.addEventListener('viewer-draft-change',event=>{
    syncEditPanel();
    if(editView && event.detail?.notesChanged!==false) editMarkers.sync();
  });
  $('showWalls').onchange=applyFilter; $('showWin').onchange=applyFilter; $('showEdges').onchange=applyFilter;
  $('showOpen').onchange=applyFilter; $('showLogical').onchange=applyFilter; $('showEnclosure').onchange=applyFilter;
  $('showMesh').onchange=applyFilter; $('meshMode').onchange=applyFilter; $('meshOpacity').oninput=applyFilter;
  $('opacity').oninput=()=>{applyOpacity();if(editView) refreshColors();};
  $('explode').oninput=applyExplode; $('explodeMode').onchange=applyExplode;
  function showRangeValue(input){
    $(input.id+'Value').value=Math.round(Number(input.value)*100)+'%';
    const fill=(Number(input.value)-Number(input.min))/(Number(input.max)-Number(input.min));
    input.style.setProperty('--progress',fill*100+'%');
  }
  for(const id of ['opacity','explode','meshOpacity']){
    const input=$(id);
    input.addEventListener('input',()=>showRangeValue(input));showRangeValue(input);
  }
  $('measure').onclick=()=>{ measuring ? clearMeasure() : startMeasure(); };  // toggle
  $('clearMeasure').onclick=clearMeasure;
  window.addEventListener('keydown', e=>{ if(e.key==='Escape' && measuring) clearMeasure(); });
  $('reset').onclick=defaultView;
  $('savePng').onclick=()=>{ renderer.render(scene,camera); const a=document.createElement('a');
    a.download='geometry_view.png'; a.href=renderer.domElement.toDataURL('image/png'); a.click(); };

  function applyLocale(next){
    locale=next==='zh-CN' ? 'zh-CN' : 'en';
    GEO.locale=locale;
    document.documentElement.lang=locale;
    $('locale').value=locale;
    document.querySelectorAll('[data-i18n]').forEach(el=>{el.textContent=msg(el.dataset.i18n);});
    for(const attr of ['title','aria-label','placeholder']){
      document.querySelectorAll('[data-i18n-'+attr+']').forEach(el=>el.setAttribute(attr,msg(el.getAttribute('data-i18n-'+attr))));
    }
    Array.from(fs.options).filter(o=>Number(o.value)>=0).forEach(o=>{o.textContent=floorLabel(Number(o.value));});
    updateStats();updateLegend($('colorBy').value);renderInspection();renderMeasurement();applyFilter();
    syncEditPanel();editMarkers.sync();
  }
  $('locale').onchange=e=>applyLocale(e.target.value);
  applyLocale(new URLSearchParams(location.search).get('lang')==='zh-CN' ? 'zh-CN' : 'en');
  applyOpacity(); refreshColors(); applyExplode(); defaultView();
  if(location.hash==='#edit') setViewMode($('editTab'));
  new ResizeObserver(()=>{
    const w=viewport.clientWidth,h=viewport.clientHeight;
    if(!w || !h) return;
    camera.aspect=w/h;camera.updateProjectionMatrix();renderer.setSize(w,h,false);
    if(!cameraMoved) defaultView();
    sizeMeasureMarkers();
  }).observe(viewport);
  (function loop(){ requestAnimationFrame(loop); controls.update(); renderer.render(scene,camera);
    positionCompassControls();if(editView) editMarkers.frame(); })();
})();
"""

_PANEL_HTML = r"""
<header class="masthead">
  <h1><svg class="brand-mark" aria-hidden="true" viewBox="0 0 32 32"><path fill="currentColor" d="M0 19 9 15v15H0zm10-3 6-4v18h-6zM16 2h15v15H16z"/><path fill="none" stroke="currentColor" stroke-width="1.5" d="M23.5 17H31v13h-7.5z"/></svg>Sim-BIM</h1>
  <div class="view-tabs" role="tablist" aria-label="[[msg:Viewer mode]]">
    <button type="button" role="tab" id="modelTab" aria-selected="true" aria-controls="stage">[[msg:Model]]</button>
    <button type="button" role="tab" id="confidenceTab" aria-selected="false" aria-controls="stage" tabindex="-1">[[msg:Confidence]]</button>
    <button type="button" role="tab" id="editTab" aria-selected="false" aria-controls="stage" tabindex="-1">[[msg:Edit]]</button>
  </div>
  <div class="head-actions">
    <select id="locale" aria-label="[[msg:Language]]"><option value="en" lang="en">English</option><option value="zh-CN" lang="zh-CN">中文</option></select>
    <button type="button" id="savePng"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3v12m-5-5 5 5 5-5M4 16v4h16v-4"/></svg><span>[[msg:Export PNG]]</span></button>
  </div>
</header>
<main class="viewer">
  <section class="stage" id="stage" role="tabpanel" aria-labelledby="modelTab">
    <div id="app"></div>
    <button type="button" id="northHandle" title="[[msg:Adjust north]]" aria-label="[[msg:Adjust north]]" aria-controls="northForm" aria-expanded="false" hidden></button>
    <div id="northForm" class="viewer-card" role="group" aria-label="[[msg:Adjust north]]" hidden>
      <label for="northBearing">[[msg:North bearing]]</label>
      <small id="northReference">[[msg:Clockwise from model +Y]]</small>
      <div class="card-actions">
        <div class="north-number"><input type="number" id="northBearing" step="5" aria-describedby="northReference"><span aria-hidden="true">°</span></div>
        <button type="button" id="northCancel" title="[[msg:Cancel north adjustment]]" aria-label="[[msg:Cancel north adjustment]]"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18"/></svg></button>
        <button type="button" id="northApply" title="[[msg:Confirm north bearing]]" aria-label="[[msg:Confirm north bearing]]"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12 4.5 4.5L19 7"/></svg></button>
      </div>
      <small id="northStorageHint" hidden>[[msg:Changes are available in this tab only.]]</small>
    </div>
    <div id="northSaveStatus" role="status" hidden>[[msg:Changes are available in this tab only.]]</div>
""" + ANNOTATION_MARKUP + r"""
    <div class="view-filters">
      <label class="filter" for="colorBy"><span>[[msg:select by]]</span><select id="colorBy"><option value="zone">[[msg:zone]]</option><option value="floor">[[msg:floor]]</option><option value="surface">[[msg:surface]]</option><option value="edge">[[msg:edge]]</option></select></label>
      <label class="filter" for="floorSel"><span>[[msg:楼层]]</span><select id="floorSel"><option value="-1">[[msg:all floors]]</option></select></label>
    </div>
    <div class="stage-tools">
      <div id="meas" role="status"></div>
      <div class="tool-row">
        <button type="button" id="measure"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m3 16 13-13 5 5L8 21zM7 12l2 2m1-5 2 2m1-5 2 2"/></svg><span>[[msg:Measure]]</span></button>
        <button type="button" id="clearMeasure">[[msg:clear measure]]</button>
        <button type="button" id="reset"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 10a8 8 0 1 1 1 7M4 4v6h6"/></svg><span>[[msg:reset view]]</span></button>
      </div>
    </div>
    <div id="sel" class="viewer-card" aria-live="polite"></div>
    <section id="editPanel" class="viewer-card" aria-labelledby="editHeading" hidden>
      <div class="edit-heading"><h2 id="editHeading"><label for="editInstructions">[[msg:Overall changes]]</label></h2><span id="noteCount"></span></div>
      <textarea id="editInstructions" rows="4" placeholder="[[msg:Describe changes to the model…]]"></textarea>
      <p id="editHint" role="status" hidden>[[msg:Confirm the object note or click empty space to discard it.]]</p>
      <p id="draftStorageStatus" role="status"></p>
      <div id="revisionActions" class="card-actions" hidden><button type="button" id="reviewRevision">[[msg:Submit edits…]]</button></div>
    </section>
  </section>
  <aside id="panel">
    <div class="panel-scroll">
      <section id="hud" aria-label="[[msg:Statistics]]">
        <div id="dimensions" aria-label="[[msg:Dimensions]]"></div>
        <details id="elementStats" open><summary>[[msg:Building elements]]</summary><div id="elementCounts"></div></details>
      </section>
      <section class="settings" aria-label="[[msg:View settings]]">
        <details open><summary>[[msg:display]]</summary>
          <div class="parameter">
            <label for="opacity">[[msg:Opacity]]</label><output id="opacityValue" for="opacity">100%</output>
            <input type="range" id="opacity" min="0.05" max="1" step="0.05" value="1">
          </div>
          <div class="parameter">
            <label for="explode">[[msg:exploded view]]</label><output id="explodeValue" for="explode">0%</output>
            <input type="range" id="explode" min="0" max="1" step="0.02" value="0">
            <select id="explodeMode" aria-label="[[msg:exploded view]]"><option value="floor">[[msg:by floor]]</option><option value="zone">[[msg:by zone]]</option></select>
          </div>
        </details>
        <details id="meshControls" hidden><summary>[[msg:Comparison]]</summary>
          <div class="checks"><label class="chk"><input type="checkbox" id="showMesh"> [[msg:叠加原始 textured mesh]]</label></div>
          <select id="meshMode" aria-label="[[msg:Comparison]]"><option value="outline">[[msg:贴图 + BIM轮廓]]</option><option value="blend">[[msg:贴图 + BIM实体]]</option><option value="mesh">[[msg:仅贴图]]</option></select>
          <div class="parameter">
            <label for="meshOpacity">[[msg:贴图透明度]]</label><output id="meshOpacityValue" for="meshOpacity">100%</output>
            <input type="range" id="meshOpacity" min="0.1" max="1" step="0.05" value="1">
          </div>
          <p id="meshStatus" role="status"></p>
        </details>
        <details id="layerControls"><summary>[[msg:Layers]]</summary><div class="checks">
          <label class="chk"><input type="checkbox" id="showWalls" checked> [[msg:wall / floor / roof faces]]</label>
          <label class="chk"><input type="checkbox" id="showWin" checked> [[msg:windows]]</label>
          <label class="chk"><input type="checkbox" id="showOpen" checked> [[msg:门 / 空开口]]</label>
          <label class="chk"><input type="checkbox" id="showEdges" checked> [[msg:edges]]</label>
          <label class="chk"><input type="checkbox" id="showLogical"> [[msg:逻辑空间边界（细虚线）]]</label>
          <label class="chk"><input type="checkbox" id="showEnclosure" checked> [[msg:开敞 / 未知辅助面]]</label>
        </div></details>
        <details id="sectionControls"><summary>[[msg:section cuts]]</summary><div id="sections"></div></details>
      </section>
    </div>
    <section id="legendGroup" class="panel-legend" aria-labelledby="roomLegendTitle" hidden>
      <h2 id="roomLegendTitle">[[msg:Room types]]</h2><div id="legend"></div>
    </section>
    <section id="confidenceLegend" class="panel-legend" aria-labelledby="confidenceLegendTitle" hidden>
      <div class="legend-heading"><h2 id="confidenceLegendTitle">[[msg:Spatial confidence]]</h2><span id="confidenceUnit"></span></div>
      <ul id="confidenceScale"></ul>
    </section>
  </aside>
</main>
"""

_STYLE = r"""
  :root {color-scheme:light;--ink:#19212e;--muted:#586575;--line:#d9dee6;--blue:#244edc;--sheet:#fff;
    --card-line:#bac7d6;--card-radius:11px;--card-shadow:0 6px 24px #2236551c;--card-inset:clamp(16px,1.2vw,21px);--card-footer:#f3f6fa;
    font:14px/1.45 "Work Sans","Segoe UI","Microsoft YaHei UI","PingFang SC",sans-serif;color:var(--ink);background:white;}
  * {box-sizing:border-box;}
  [hidden] {display:none!important;}
  .viewer-card {color:var(--ink);background:var(--sheet);border:1px solid var(--card-line);border-radius:var(--card-radius);box-shadow:var(--card-shadow);}
  .card-actions {display:flex;align-items:center;justify-content:flex-end;gap:8px;padding:11px var(--card-inset);background:var(--card-footer);border-top:1px solid var(--line);}
  .card-actions button {min-height:37px;border-radius:6px;}
  #northHandle {position:absolute;transform:translate(-50%,-50%);border:0;border-radius:50%;padding:0;background:transparent;touch-action:none;cursor:grab;}
  #northHandle:is(:hover,:focus-visible,[aria-expanded="true"]) {outline:2px solid #5479d7;outline-offset:1px;background:#446bde0d;}
  #northHandle:active {cursor:grabbing;}
  #northForm {position:absolute;z-index:6;width:min(268px,calc(100% - 24px));padding:0;overflow:hidden;}
  #northForm label {display:block;margin:var(--card-inset) var(--card-inset) 3px;font-size:17px;line-height:1.35;font-weight:650;}
  #northReference {display:block;margin:0 var(--card-inset) 14px;color:var(--muted);font-size:13px;}
  #northStorageHint {display:block;padding:8px var(--card-inset);font-size:12px;color:#805b16;}
  #northSaveStatus {position:absolute;right:20px;bottom:24px;z-index:4;max-width:240px;border:1px solid #d7c498;border-radius:6px;padding:7px 11px;background:#fffaf0;color:#704f15;font-size:12px;pointer-events:none;}
  .north-number {display:flex;align-items:center;flex:1;min-width:0;border:1px solid var(--card-line);border-radius:6px;height:39px;background:white;overflow:hidden;}
  .north-number:focus-within {outline:2px solid #7190ef;}
  .north-number input {min-width:0;width:100%;height:100%;border:0;padding:0 0 0 10px;font-size:16px;font-variant-numeric:tabular-nums;}
  .north-number input:focus-visible {outline:0;}
  .north-number span {padding:0 9px 0 2px;color:var(--muted);}
  #northForm button {width:39px;height:39px;padding:8px;}
  #northApply {background:var(--blue);color:white;border-color:var(--blue);}
  #northApply:disabled {opacity:.4;cursor:default;}
  html,body {height:100%;margin:0;}
  body {display:flex;flex-direction:column;overflow:hidden;}
  button,select,input {font:inherit;}
  button,select {color:var(--ink);}
  button,select,summary,input[type=checkbox] {cursor:pointer;}
  :focus-visible {outline:3px solid #7190ef;outline-offset:2px;}
  h1,h2,p {margin:0;}
  .masthead {display:grid;grid-template-columns:1fr auto 1fr;align-items:center;gap:14px;flex:none;height:64px;padding:0 clamp(16px,2.1vw,34px);border-bottom:1px solid var(--line);background:white;}
  h1 {display:flex;align-items:center;gap:10px;font-size:25px;letter-spacing:-.05em;line-height:1;font-weight:720;}
  .brand-mark {height:28px;width:28px;color:var(--blue);}
  .head-actions,.tool-row,.view-filters {display:flex;align-items:center;gap:8px;}
  .head-actions {gap:14px;justify-self:end;}
  .view-tabs {display:flex;gap:2px;padding:3px;border:1px solid #d4dae4;border-radius:7px;background:#f2f4f7;}
  .view-tabs button {border:1px solid transparent;background:transparent;color:var(--muted);padding:5px 15px;font-size:13px;}
  .view-tabs button[aria-selected="true"] {background:white;border-color:#c4ccd7;color:var(--ink);box-shadow:0 1px 3px #1322310d;}
  #confidenceTab[aria-selected="true"] {color:#59298c;border-color:#c2a8d9;}
  button,select {border:1px solid #bdc7d3;border-radius:5px;background:#fff;}
  button {display:inline-flex;align-items:center;justify-content:center;gap:7px;padding:8px 12px;font-weight:570;white-space:nowrap;}
  button svg {display:block;width:17px;height:17px;fill:none;stroke:currentColor;stroke-width:1.65;stroke-linecap:round;stroke-linejoin:round;}
  button:hover,summary:hover {background:#f1f4f8;}
  select {padding:7px 8px;}
  #locale {border-color:transparent;max-width:106px;}
  .viewer {display:grid;grid-template-columns:minmax(0,1fr) clamp(310px,25vw,405px);flex:1;min-height:0;}
  .stage {position:relative;isolation:isolate;min-width:0;min-height:0;overflow:hidden;background:#f4f6f8;}
  #app {position:absolute;inset:0;}
  #app canvas {display:block;width:100%;height:100%;touch-action:none;}
""" + ANNOTATION_CSS + r"""
  .view-filters {position:absolute;left:clamp(12px,1.7vw,26px);top:clamp(12px,1.7vw,24px);align-items:stretch;max-width:calc(100% - 24px);}
  .filter {min-width:0;display:flex;align-items:center;gap:5px;background:#fff;border:1px solid #c6cfda;border-radius:5px;padding-left:10px;box-shadow:0 1px 3px #2236550c;}
  .filter>span {white-space:nowrap;font-size:12px;color:var(--muted);}
  .filter select {background:transparent;border:0;font-weight:570;min-width:0;max-width:clamp(105px,14vw,220px);font-size:13px;}
  .stage-tools {position:absolute;left:clamp(12px,1.7vw,26px);bottom:clamp(12px,1.7vw,24px);display:grid;justify-items:start;gap:9px;max-width:calc(100% - 24px);}
  .tool-row {flex-wrap:wrap;gap:6px;}
  .stage-tools button {font-size:13px;box-shadow:0 2px 5px #2236550e;}
  #meas,#sel {display:none;}
  #clearMeasure {display:none;}
  .stage-tools:has(#meas[style*="block"]) #measure {border-color:var(--blue);color:var(--blue);background:#eff3ff;}
  .stage-tools:has(#meas[style*="block"]) #clearMeasure {display:inline-flex;}
  #meas {padding:9px 14px;border-radius:7px;background:#182940;color:#fff;font-size:14px;box-shadow:0 3px 13px #15284236;}
  #meas.has-distance {padding:11px 17px 12px;border-left:4px solid #fb74a8;}
  .measure-label {display:block;font-size:12px;line-height:1.4;font-weight:650;color:#f5f8fc;}
  .measure-value {display:block;margin:2px 0 0;color:#ff91b9;font-size:clamp(25px,2.25vw,35px);line-height:1.08;letter-spacing:-.02em;font-variant-numeric:tabular-nums;}
  .measure-value small {font-size:.6em;font-weight:550;color:#f5f8fc;}
  #sel,#editPanel {position:absolute;left:clamp(12px,1.7vw,26px);top:50%;transform:translateY(-50%);
    max-height:calc(100% - 144px);overflow:auto;overscroll-behavior:contain;}
  #sel {width:clamp(266px,29%,352px);}
  .pick-head {display:grid;gap:3px;padding:14px var(--card-inset) 8px;}
  #editPanel {width:clamp(308px,32%,372px);padding:0;}
  .edit-heading {display:flex;align-items:baseline;justify-content:space-between;gap:12px;margin:var(--card-inset) var(--card-inset) 8px;}
  .edit-heading h2 {font-size:17px;line-height:1.35;font-weight:650;}
  #noteCount {font-size:13px;color:var(--muted);white-space:nowrap;}
  #editInstructions {display:block;width:calc(100% - 2 * var(--card-inset));min-height:116px;resize:vertical;max-height:36vh;margin:0 var(--card-inset) 12px;padding:7px 2px;box-sizing:border-box;
    font:inherit;font-size:15px;line-height:1.6;color:var(--ink);background:var(--sheet);border:0;}
  #editInstructions::placeholder {color:var(--muted);}
  #editInstructions:focus-visible {outline:2px solid #a3b6ef;outline-offset:3px;border-radius:3px;}
  #editPanel p {font-size:13px;color:var(--muted);margin:0 var(--card-inset) 12px;}
  #reviewRevision {padding:6px 14px;background:var(--blue);border:1px solid var(--blue);color:white;}
  #reviewRevision:disabled {background:#e8ecf2;color:#606c7d;border-color:#e8ecf2;cursor:default;}
  .pick-kind {font-size:11px;font-weight:650;letter-spacing:.055em;text-transform:uppercase;color:var(--blue);}
  .pick-name {font:600 15px/1.36 Consolas,"SFMono-Regular",monospace;overflow-wrap:anywhere;}
  .pick-fields {padding:0 var(--card-inset) 8px;}
  .pick-metrics {display:flex;flex-wrap:wrap;gap:10px 16px;padding:10px var(--card-inset);background:var(--card-footer);border-top:1px solid var(--line);}
  .pick-metric {display:grid;align-content:start;flex:1 1 auto;gap:2px;}
  .pick-metric span {font-size:12px;color:var(--muted);}
  .pick-metric b {font-size:19px;line-height:1.2;font-weight:630;font-variant-numeric:tabular-nums;white-space:nowrap;}
  .pick-metric small {font-size:12px;font-weight:500;color:var(--muted);}
  .pick-fields:empty,.pick-metrics:empty {display:none;}
  .surface-attributes {display:grid;gap:10px;margin:0;padding:11px var(--card-inset) 12px;border-top:1px solid var(--line);}
  .surface-attribute {display:grid;gap:3px;min-width:0;}
  .surface-attribute dt {display:flex;align-items:center;justify-content:space-between;gap:8px;color:var(--muted);font-size:12px;}
  .surface-attribute dd {margin:0;font-size:13px;line-height:1.45;overflow-wrap:anywhere;font-variant-numeric:tabular-nums;}
  .source-tag {flex:none;padding:0 5px;color:#48576b;background:#eef1f5;border-radius:3px;font-size:11px;line-height:1.45;font-weight:500;}
  .confidence-row {display:flex;gap:8px;align-items:baseline;justify-content:space-between;padding:10px var(--card-inset);border-top:1px solid var(--line);font-size:12px;}
  .confidence-row>span {color:var(--muted);}
  .confidence-row b {display:inline-flex;gap:5px;align-items:center;font-size:12px;text-align:right;}
  .confidence-row i,#confidenceScale i {display:inline-block;flex:none;width:10px;height:10px;border:1px solid #24123226;border-radius:3px;}
  .confidence-reason summary {cursor:pointer;list-style:none;}
  .confidence-reason summary b:after {content:"";border-right:1px solid;border-bottom:1px solid;width:5px;height:5px;transform:rotate(45deg);margin:-3px 1px 0 3px;}
  .confidence-reason[open] summary b:after {transform:rotate(225deg);margin-top:3px;}
  .confidence-reason p {font-size:13px;line-height:1.5;padding:0 var(--card-inset) 12px;overflow-wrap:anywhere;}
  .kv {display:flex;align-items:baseline;justify-content:space-between;gap:12px;}
  .kv span {color:var(--muted);}
  .kv b {font-variant-numeric:tabular-nums;font-weight:600;}
  #sel .kv,#elementCounts .kv {padding:3px 0;font-size:13px;}
  #sel .kv b {min-width:0;overflow-wrap:anywhere;text-align:right;}
  #sel .kv span {flex:none;}
  #panel {--panel-inset:clamp(18px,1.65vw,28px);display:flex;flex-direction:column;min-width:0;min-height:0;overflow:hidden;border-left:1px solid var(--line);background:var(--sheet);}
  .panel-scroll {flex:1;min-height:0;overflow-y:auto;padding:var(--panel-inset);}
  .panel-legend {flex:none;max-height:32%;overflow-y:auto;border-top:1px solid var(--line);padding:14px var(--panel-inset) 17px;background:#fafbfd;}
  .panel-legend h2 {font-size:14px;line-height:1.35;font-weight:650;}
  .legend-heading {display:flex;justify-content:space-between;gap:8px;align-items:baseline;}
  #confidenceUnit {font-size:12px;color:var(--muted);}
  #confidenceScale {display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:7px 9px;padding:0;list-style:none;margin:10px 0 0;}
  #confidenceScale li {display:flex;gap:6px;align-items:center;font-size:12px;color:var(--ink);}
  #confidenceScale i {width:16px;height:11px;}
  #dimensions .kv span {font-size:13px;}
  #dimensions {display:grid;grid-template-columns:repeat(3,minmax(0,1fr));padding:0 0 14px;border-bottom:1px solid var(--line);}
  #dimensions .kv {display:flex;flex-direction:column;align-items:flex-start;gap:3px;}
  #dimensions .kv + .kv {padding-left:10px;border-left:1px solid var(--line);}
  #dimensions b {font-size:16px;white-space:nowrap;}
  .settings {margin-top:6px;}
  #panel details {border-bottom:1px solid var(--line);}
  #panel summary {position:relative;padding:11px 2px 11px 18px;font-size:14px;font-weight:590;list-style:none;cursor:pointer;border-radius:2px;}
  #panel summary::-webkit-details-marker {display:none;}
  #panel summary:before {content:"";position:absolute;top:calc(50% - 4px);left:3px;width:6px;height:6px;border-right:1.5px solid #5b687c;border-bottom:1.5px solid #5b687c;transform:rotate(-45deg);}
  #panel details[open]>summary:before {transform:rotate(45deg);}
  #elementCounts,.checks,#sections {padding:0 1px 12px;}
  .parameter {display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:center;gap:4px 10px;margin:2px 0 13px;}
  .parameter label,.checks,.parameter select,#meshMode {font-size:14px;}
  .sec {font-size:13px;}
  .parameter output {font-size:12px;color:var(--muted);font-variant-numeric:tabular-nums;}
  .parameter input,.parameter select {grid-column:1/-1;}
  input[type=range] {width:100%;accent-color:var(--blue);height:20px;margin:3px 0;}
  .parameter input[type=range] {appearance:none;background:transparent;cursor:pointer;}
  .parameter input[type=range]::-webkit-slider-runnable-track {height:4px;border-radius:4px;background:linear-gradient(to right,var(--blue) var(--progress),#d9dfe7 var(--progress));}
  .parameter input[type=range]::-webkit-slider-thumb {appearance:none;width:14px;height:14px;margin-top:-5px;border-radius:50%;background:var(--blue);}
  .parameter input[type=range]::-moz-range-track {height:4px;border-radius:4px;background:#d9dfe7;}
  .parameter input[type=range]::-moz-range-progress {height:4px;background:var(--blue);}
  .parameter input[type=range]::-moz-range-thumb {width:14px;height:14px;border:0;border-radius:50%;background:var(--blue);}
  input[type=checkbox] {width:15px;height:15px;accent-color:var(--blue);margin:0;flex:none;}
  .chk,.axis-label {display:flex;align-items:center;gap:8px;cursor:pointer;}
  .checks {display:grid;gap:9px;padding-top:1px;}
  .sec {display:grid;grid-template-columns:34px minmax(0,1fr) auto;align-items:center;gap:8px;padding:4px 0;}
  .sec .flip {font-size:12px;}
  #meshMode {width:100%;margin:0 0 13px;}
  #meshStatus {color:var(--muted);font-size:12px;margin-bottom:12px;}
  #meshStatus:empty {display:none;}
  #legend {display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:6px 12px;margin-top:9px;}
  .lg {display:flex;align-items:baseline;gap:6px;font-size:12px;line-height:1.35;overflow-wrap:anywhere;}
  .sw {display:inline-block;align-self:flex-start;margin-top:3px;width:10px;height:10px;flex:none;border-radius:2px;border:1px solid #0002;}
  @media (max-width:800px) and (orientation:portrait), (max-width:650px) {
    html,body {height:auto;min-height:100%;}
    body {overflow:auto;}
    .masthead {height:54px;padding:0 13px;}
    .head-actions {gap:3px;}
    .brand-mark {width:23px;height:23px;}
    h1 {font-size:21px;gap:6px;}
    .head-actions button {padding:7px 9px;font-size:12px;}
    .viewer {display:block;}
    .stage {height:60vh;height:60svh;min-height:300px;max-height:640px;}
    #panel {overflow:visible;border-left:none;border-top:1px solid var(--line);}
    .panel-scroll,.panel-legend {flex:none;max-height:none;overflow:visible;}
    .view-filters {gap:5px;}
    .filter {padding-left:6px;gap:1px;}
    .filter>span {font-size:11px;}
    .filter select {font-size:12px;padding:6px 3px;max-width:30vw;}
    .stage-tools {inset:12px;display:flex;align-items:flex-start;flex-direction:column;justify-content:flex-end;pointer-events:none;}
    .stage-tools .tool-row {pointer-events:auto;}
    #meas {position:absolute;top:43px;left:0;}
    #meas.has-distance {padding:7px 12px 8px;}
    #sel {width:min(280px,calc(100% - 24px));max-height:calc(100% - 116px);}
    .pick-head {padding-top:9px;}
  }
  @media (max-height:530px) and (min-width:651px) { .masthead {height:46px;} }
"""

_HTML = r"""<!doctype html>
<html lang="__LOCALE__"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sim-BIM</title>
<style>__STYLE__</style></head>
<body>
__PANEL__
<script>__THREE_JS__</script>
<script>__ORBIT_JS__</script>
<script>window.GEO = __GEO_JSON__;</script>
<script>__APP_JS__</script>
</body></html>
"""


def app_js(locale: str = "zh-CN") -> str:
    """The viewer app script (for embedding + standalone syntax checking)."""
    draft = Path(__file__).resolve().parents[2] / "src/workbench/static/viewer-draft.js"
    return draft.read_text(encoding="utf-8") + "\n" + viewer_script(_APP_JS)


def discover_roles(bg_path: Path) -> dict:
    """Read room-use colours from this display geometry's own zone metadata."""
    try:
        geometry = json.loads(bg_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {
        str(row["name"]): str(row["role"])
        for row in geometry.get("zone_meta", [])
        if row.get("name") and row.get("role")
    }


def _fields(record: dict, keys: tuple) -> dict:
    return {key: record[key] for key in keys if key in record}


def _viewer_source(source: dict) -> dict:
    """Keep model identity, counts and selection metadata without packaging the run journal."""
    result = _fields(source, ("schema_version", "source_model_sha256"))
    result["derived"] = _fields(source.get("derived", {}), ("zones", "surfaces", "windows", "openings"))
    for group, fields in {
        "floors": ("id", "name", "z_floor", "height"),
        "spaces": ("id", "floor_id", "role", "enclosure"),
        "boundaries": ("id", "space_id", "enclosure", "kind"),
        "openings": ("id", "kind"),
        "connections": ("opening_id", "kind", "space_ids", "exterior", "state"),
    }.items():
        result[group] = [_fields(row, fields) for row in source.get(group, ())]
    return result


def _edge_ranks(segments) -> list[int]:
    from src.agent.geometry.source_naming import geometry_key

    keys = [geometry_key(segment) for segment in segments]
    ranks = {key: index for index, key in enumerate(sorted(set(keys)), 1)}
    return [ranks[key] for key in keys]


def _ring_edges(rings):
    return [(v, ring[(i + 1) % len(ring)]) for ring in rings for i, v in enumerate(ring)]


def _selection_names(data: dict, geo: dict, has_source_digest: bool) -> None:
    """Public room, face and side-specific opening names, plus selectable segment ranks."""
    from src.agent.geometry.source_naming import viewer_names

    source = data.get("source_model") or {}
    parts = geo["visible_wall_parts"]
    complete = (source.get("schema_version") in ("source_bim_v2", "source_bim_v3") and
                source.get("floors") and source.get("spaces") and
                all(f.get("footprint") for f in source["floors"]) and all(s.get("polygon") for s in source["spaces"])
                and "boundaries" in source and "openings" in source and "opening_hosts" in source
                and all(o.get("id") in source["opening_hosts"] for o in source["openings"]))
    if complete:
        names = viewer_names(data, parts)
        draft_names = names if has_source_digest else viewer_names(data, parts, draft_anchors=True)
    else:
        from scripts.tool_scripts.bim_viewer_legacy_names import legacy_viewer_names
        names = legacy_viewer_names(data, parts, geo["roles"])
        draft_names = legacy_viewer_names(data, parts, geo["roles"], draft_anchors=True)
        geo["primary_floors"] = names["primary_floors"]
    geo["public_names"] = {"floors": names["floors"], "spaces": names["spaces"]}
    objects, draft_objects = names["objects"], draft_names["objects"]

    def assign(row, field, current, old):
        row[field] = current
        if old != current:
            row["draft_" + field] = old

    for surface in geo["surfaces"]:
        name = surface["name"]
        assign(surface, "public_name", objects[name], draft_objects[name])
        if name not in parts:
            surface["edge_ranks"] = _edge_ranks(_ring_edges([surface["verts"], *surface.get("holes", [])]))
        for i, part in enumerate(parts.get(name, [])):
            assign(part, "public_name", names["parts"][name][i], draft_names["parts"][name][i])
            segments = part.get("edge_segments")
            if segments is None:
                segments = _ring_edges([part["verts"], *part.get("holes", [])])
            part["edge_ranks"] = _edge_ranks(segments)
    for obj in [*geo["windows"], *geo["openings"]]:
        assign(obj, "public_name", objects[obj["name"]], draft_objects[obj["name"]])
        assign(obj, "parent_name", objects.get(obj["parent"]), draft_objects.get(obj["parent"], obj["parent"]))
        obj["edge_ranks"] = _edge_ranks(_ring_edges([obj["verts"]]))
    for i, region in enumerate(geo["enclosure_regions"]):
        name, old = names["regions"][i], draft_names["regions"][i]
        assign(region, "public_name", name, old)
        assign(region, "parent_name", name.rsplit("_", 1)[0], old.rsplit("_", 1)[0])
        region["edge_ranks"] = _edge_ranks(_ring_edges([region["verts"], *region.get("holes", [])]))


class RevisionMeshCache:
    """Keep the last textured payload in memory for one revision's tool session."""

    def __init__(self):
        self._entry = None
        self._lock = Lock()

    def payload(self, path, frame):
        from src.agent.geometry.mesh_observation import MeshObservation

        path = Path(path).resolve()
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        # The in-process bake function identifies the renderer version. Nothing survives a restart.
        key = (path, digest, json.dumps(frame, sort_keys=True), MeshObservation.viewer_payload)
        with self._lock:
            if self._entry is not None and self._entry[0] == key:
                return self._entry[1]
            mesh = MeshObservation(path).viewer_payload(frame)
            if mesh["mesh_sha256"] == digest:
                self._entry = (key, mesh)
            return mesh


def build_viewer_html(data: dict, *, title: str | None = None, roles: dict | None = None,
                      mesh_path: Path | None = None, locale: str = "zh-CN",
                      revision_mesh_cache: RevisionMeshCache | None = None) -> str:
    three_js = (_VENDOR / "three.min.js").read_text(encoding="utf-8")
    orbit_js = (_VENDOR / "OrbitControls.js").read_text(encoding="utf-8")
    from src.agent.geometry.openings import visible_wall_parts

    source = data.get("source_model") or {}
    coordinate_system = source.get("coordinate_system") or data.get("coordinate_system") or {}
    north_axis = coordinate_system.get("north_axis", data.get("north_axis"))
    if isinstance(north_axis, dict):
        north_axis = north_axis.get("value_deg")
    parts = data["display_surface_parts"] if "display_surface_parts" in data else visible_wall_parts(data)
    part_keys = ("verts", "holes", "duplicate_at_rest", "occluded_by", "edge_segments", "enclosure_condition")
    geo = {
        "locale": "en",
        "north_axis_deg": north_axis if type(north_axis) in (float, int) else None,
        "messages": viewer_messages(_HTML, _PANEL_HTML, _APP_JS),
        "zones": data.get("zones", []),
        "surfaces": [_fields(s, ("name", "zone", "type", "obc", "obc_obj", "verts", "holes")) for s in data.get("surfaces", ())],
        "windows": [_fields(w, ("name", "parent", "verts")) for w in data.get("windows", ())],
        "openings": [_fields(o, ("name", "parent", "verts", "source_opening_id", "kind", "space_id",
                                 "other_space_id", "state", "partner")) for o in data.get("openings", ())],
        "visible_wall_parts": {name: [_fields(p, part_keys) for p in rows] for name, rows in parts.items()},
        "enclosure_regions": [_fields(r, ("boundary_id", "space_id", "condition", "verts", "holes"))
                              for r in data.get("enclosure_regions", ())],
        "roles": dict(roles) if roles is not None else {
            **{r["name"]: r["role"] for r in data.get("zone_meta", []) if r.get("name") and r.get("role")},
            **data.get("roles", {})},
        "source_model": _viewer_source(source) if source else None,
        "inspection": public_inspection(source, data.get("sim_inspection")),
    }
    from src.agent.roles import ROOM_TYPES
    geo["room_types"] = room_type_labels(ROOM_TYPES)
    identity = source.get("source_model_sha256")
    has_source_digest = isinstance(identity, str) and re.fullmatch(r"[a-f0-9]{64}", identity) is not None
    _selection_names(data, geo, has_source_digest)
    from src.agent.geometry.viewer_metrics import viewer_metrics
    geo["metrics"] = viewer_metrics(source)
    if mesh_path is not None:
        from src.agent.geometry.mesh_observation import MeshObservation
        frame = source.get("mesh_frame")
        mesh = (MeshObservation(mesh_path).viewer_payload(frame) if revision_mesh_cache is None else
                revision_mesh_cache.payload(mesh_path, frame))
        geo["textured_mesh"] = {**mesh, "mesh_frame": _fields(mesh["mesh_frame"],
                                                            ("mesh_sha256", "yaw_degrees", "translation_m"))}
    spaces = {row["id"]: row for row in source.get("spaces", [])}
    source_floors = {row["id"]: row for row in source.get("floors", [])}
    zone_map = source.get("derived", {}).get("zones", {})
    memberships = {}
    for zone in geo["zones"]:
        sid = zone_map.get(zone, zone)
        space = spaces.get(sid, {})
        role = geo["roles"].get(zone) or space.get("role")
        if role:
            geo["roles"][zone] = room_role(role)
        if not space:
            continue
        indices = [i for i, floor in enumerate(geo["public_names"]["floors"])
                   if space.get("floor_id") == floor["id"]
                   or sid in source_floors.get(floor["id"], {}).get("spanning_space_ids", [])]
        if indices:
            memberships[zone] = indices
    geo["floor_memberships"] = memberships
    # Older display-only models lack a canonical source digest. Derive an identity
    # from their selectable geometry so replacing a file cannot bind stale notes.
    if not has_source_digest:
        geometry = {k: geo[k] for k in ("zones", "roles", "source_model")}
        # Older viewers included their public face labels in this storage key.
        # Keep their exact serialized shape and values across display-name updates.
        for group in ("surfaces", "windows", "openings", "enclosure_regions"):
            geometry[group] = [{k: row.get("draft_" + k, v) for k, v in row.items() if not k.startswith("draft_")}
                               for row in geo[group]]
        identity = hashlib.sha256(json.dumps(geometry, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    geo["draft_model_id"] = identity
    # Replace placeholders in the trusted template once, so an input name or
    # evidence containing another placeholder is preserved literally.
    values = {
        "LOCALE": "en", "STYLE": _STYLE,
        "THREE_JS": three_js, "ORBIT_JS": orbit_js, "GEO_JSON": _js_embed(geo),
        "APP_JS": app_js(),
    }
    template = _HTML.replace("__PANEL__", _PANEL_HTML)
    return re.sub(r"__([A-Z_]+)__", lambda m: values[m[1]], viewer_markup(template, geo["messages"]))


def load_viewer_geometry(path: Path) -> dict:
    """Load either a display geometry file or an authoritative source BIM.

    Source BIM inputs go through their validated display projection.  Unknown
    source schema versions fail rather than opening a plausible-looking empty
    viewer.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    schema = data.get("schema_version")
    if schema in {"source_bim_v2", "source_bim_v3"}:
        from src.agent.geometry.source_bim import source_view_geometry

        data = source_view_geometry(data)
    elif isinstance(schema, str) and schema.startswith("source_bim"):
        raise ValueError(f"unsupported source BIM schema: {schema}")

    source_path = path.with_name("source_model.json")
    if source_path.exists() and not data.get("source_model"):
        from src.agent.geometry.source_model import _digest

        source = json.loads(source_path.read_text(encoding="utf-8"))
        # Attach a source only when its digest binds this display geometry.
        if source.get("derived_geometry_sha256") == _digest(data):
            data["source_model"] = source
    inspection_path = path.with_name("sim_inspection.json")
    if data.get("source_model") and inspection_path.is_file():
        inspection = json.loads(inspection_path.read_text(encoding="utf-8"))
        digest = data["source_model"].get("source_model_sha256")
        if digest and inspection.get("source_model_sha256") == digest:
            data["sim_inspection"] = inspection
    return data


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("json", help="display/building geometry JSON, or validated source_bim_v2/v3 source_model.json")
    ap.add_argument("--out", help="output HTML (default: <json dir>/geometry_viewer.html)")
    ap.add_argument("--title", default="")
    ap.add_argument("--locale", choices=("zh-CN", "en"), default="zh-CN")
    ap.add_argument("--mesh", type=Path, help="admitted original GLB; requires a matching source mesh_frame")
    ap.add_argument("--roles", help="optional JSON {zone: role} to colour zones by room type; "
                                    "default reads roles from the display geometry zone_meta")
    args = ap.parse_args()
    j = Path(args.json)
    data = load_viewer_geometry(j)
    if args.roles:
        roles = json.loads(Path(args.roles).read_text(encoding="utf-8"))
    else:
        roles = discover_roles(j) or None
    out = Path(args.out) if args.out else j.with_name("geometry_viewer.html")
    title = args.title or j.parent.parent.name or j.stem
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_viewer_html(data, title=title, roles=roles, mesh_path=args.mesh, locale=args.locale), encoding="utf-8")
    if roles:
        print(f"  room-type roles: {len(roles)} zones (colour-by-type enabled)")
    kb = out.stat().st_size // 1024
    print(f"wrote {out}  ({kb} KB, offline; orbit / opacity / sections / explode / vertex-measure)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
