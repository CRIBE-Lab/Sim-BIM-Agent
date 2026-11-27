import { element } from './dom.js';

const identity = [1,0,0,0, 0,1,0,0, 0,0,1,0, 0,0,0,1];
function transform(matrix, x, y, z) {
  return [0,1,2].map(i => matrix[i]*x + matrix[i+4]*y + matrix[i+8]*z + matrix[i+12]);
}
function matrixOf(node) {
  if (node.matrix) return node.matrix;
  const [x,y,z,w] = node.rotation ?? [0,0,0,1], [a,b,c] = node.scale ?? [1,1,1];
  return [(1-2*y*y-2*z*z)*a,(2*x*y+2*z*w)*a,(2*x*z-2*y*w)*a,0,
    (2*x*y-2*z*w)*b,(1-2*x*x-2*z*z)*b,(2*y*z+2*x*w)*b,0,
    (2*x*z+2*y*w)*c,(2*y*z-2*x*w)*c,(1-2*x*x-2*y*y)*c,0,...(node.translation ?? [0,0,0]),1];
}
function multiply(left, right) {
  return right.map((_, i) => [0,1,2,3].reduce((s,k) => s + left[(i%4)+k*4]*right[k+4*Math.floor(i/4)], 0));
}
function meshPoints(bytes) {
  const data = new DataView(bytes);
  if (data.getUint32(0,true) !== 0x46546c67 || data.getUint32(4,true) !== 2) throw Error();
  const jsonSize = data.getUint32(12,true);
  const json = JSON.parse(new TextDecoder().decode(new Uint8Array(bytes,20,jsonSize)).trim());
  const binaryStart = 28 + jsonSize;
  const primitives = [];
  function walk(index, parent, ancestors) {
    if (ancestors.has(index)) return;
    const node = json.nodes[index], matrix = multiply(parent,matrixOf(node));
    const lineage = new Set(ancestors).add(index);
    for (const prim of json.meshes?.[node.mesh]?.primitives ?? []) {
      const pos = json.accessors?.[prim.attributes?.POSITION], view = json.bufferViews?.[pos?.bufferView];
      if (pos?.componentType === 5126 && pos.type === 'VEC3' && view?.buffer === 0) primitives.push({pos,view,matrix});
    }
    for (const child of node.children ?? []) walk(child,matrix,lineage);
  }
  for (const node of json.scenes?.[json.scene ?? 0]?.nodes ?? []) walk(node,identity,new Set());
  const all = primitives.reduce((sum,p) => sum+p.pos.count,0);
  if (!all) throw Error();
  const step = Math.max(1,Math.ceil(all/35000));
  const points = [];
  for (const {pos,view,matrix} of primitives) {
    const start = binaryStart + (pos.byteOffset ?? 0) + (view.byteOffset ?? 0);
    for (let i=0; i<pos.count; i+=step) {
      const offset = start+i*(view.byteStride ?? 12);
      const point = transform(matrix,...[0,4,8].map(k => data.getFloat32(offset+k,true)));
      if (point.every(Number.isFinite)) points.push(point);
    }
  }
  if (!points.length) throw Error();
  const min = [Infinity,Infinity,Infinity], max = [-Infinity,-Infinity,-Infinity];
  for (const p of points) for (let i=0;i<3;i++) { min[i]=Math.min(min[i],p[i]); max[i]=Math.max(max[i],p[i]); }
  const center = max.map((x,i)=>(x+min[i])/2), scale = Math.max(1e-9,...max.map((x,i)=>x-min[i]));
  return { points: points.map(p=>p.map((x,i)=>(x-center[i])/scale)), count: all };
}
function showMesh(root, bytes, t, lang) {
  const {points,count} = meshPoints(bytes), figure = element('figure','mesh-preview');
  const canvas = element('canvas');
  canvas.setAttribute('aria-label',t('meshPreview'));
  figure.append(canvas,element('figcaption','hint',t('meshHint')+' · '+t('meshPositions')+' '+count.toLocaleString(lang)));
  root.replaceChildren(figure);
  let yaw = -.6, pitch = .32, zoom = 1, pointer = null;
  function draw() {
    const ratio = Math.min(2,window.devicePixelRatio || 1);
    const width = canvas.clientWidth, height = canvas.clientHeight;
    canvas.width = width*ratio; canvas.height = height*ratio;
    const ctx = canvas.getContext('2d');
    ctx.scale(ratio,ratio);
    const cy=Math.cos(yaw), sy=Math.sin(yaw), cp=Math.cos(pitch), sp=Math.sin(pitch);
    const size=Math.min(width,height)*.79*zoom;
    ctx.fillStyle='#345889'; ctx.globalAlpha=.69;
    for (const [x,y,z] of points) {
      const px=cy*x+sy*z, pz=cy*z-sy*x;
      ctx.fillRect(width/2+px*size,height/2-(cp*y-sp*pz)*size,1.7,1.7);
    }
  }
  const observer = new ResizeObserver(draw); observer.observe(canvas);
  root.closest('dialog').addEventListener('close',()=>observer.disconnect(),{once:true});
  canvas.onpointerdown = e=>{ pointer={x:e.clientX,y:e.clientY}; canvas.setPointerCapture(e.pointerId); };
  canvas.onpointermove = e=>{ if (!pointer) return; yaw+=(e.clientX-pointer.x)*.012; pitch=Math.max(-1.48,Math.min(1.48,pitch+(e.clientY-pointer.y)*.012)); pointer={x:e.clientX,y:e.clientY}; draw(); };
  canvas.onpointerup = canvas.onpointercancel = ()=>{ pointer=null; };
  canvas.addEventListener('wheel',e=>{ e.preventDefault(); zoom=Math.max(.3,Math.min(5,zoom*Math.exp(-e.deltaY*.001))); draw(); },{passive:false});
  draw();
}
export async function previewAsset(asset, root, t, lang, signal) {
  root.replaceChildren(element('p','hint',t('load')));
  try {
    if (asset.kind === 'image') {
      const img = element('img','preview-image'); img.alt=asset.name;
      img.onerror=()=>{ if (!signal.aborted) root.replaceChildren(element('p','hint',t('previewError'))); };
      img.src=asset.url; root.replaceChildren(img); return;
    }
    const response = asset.file ? null : await fetch(asset.url,{signal});
    if (response && !response.ok) throw Error();
    const file = asset.file ?? await response.blob();
    if (asset.kind === 'mesh') {
      const bytes = await file.arrayBuffer();
      if (!signal.aborted) showMesh(root,bytes,t,lang);
    } else {
      const text = await file.text();
      let formatted;
      try { formatted=JSON.stringify(JSON.parse(text),null,2); } catch { formatted=text; }
      if (!signal.aborted) root.replaceChildren(element('pre','json-preview',formatted));
    }
  } catch {
    if (!signal.aborted) root.replaceChildren(element('p','hint',t('previewError')));
  }
}
