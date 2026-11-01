(() => {
  "use strict";

  const DATA = JSON.parse(document.getElementById("building").textContent);
  const $ = (id) => document.getElementById(id);
  const CATEGORY_NAMES = { wall: "墙", floor: "楼面", window: "窗", door: "门" };
  const CATEGORY_ORDER = ["wall", "floor", "window", "door"];
  const STOREY_GAP_M = 3;
  const SNAP_PX = 14;
  const CLICK_PX = 4;
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const darkQuery = window.matchMedia("(prefers-color-scheme: dark)");

  const themeColour = (name) =>
    new THREE.Color(getComputedStyle(document.documentElement).getPropertyValue(name).trim());

  // Renderer, scene and camera.
  const stage = $("stage");
  const renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.localClippingEnabled = true;
  stage.prepend(renderer.domElement);

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(45, 1, 0.05, 5000);
  const controls = new THREE.OrbitControls(camera, renderer.domElement);
  controls.enableDamping = !reduceMotion;
  scene.add(new THREE.HemisphereLight(0xffffff, 0x9aa09d, 0.85));
  const sun = new THREE.DirectionalLight(0xffffff, 0.55);
  sun.position.set(0.6, 1, 0.45);
  scene.add(sun);

  // Storeys, ordered from the lowest floor up.
  const storeys = [...DATA.storeys].sort((a, b) => a.elevation_m - b.elevation_m);
  const storeyById = new Map(storeys.map((s) => [s.id, s]));
  const storeyGroups = new Map();
  storeys.forEach((storey, index) => {
    const group = new THREE.Group();
    group.userData.index = index;
    scene.add(group);
    storeyGroups.set(storey.id, group);
  });

  // Elements: one mesh and one material each, so they can be highlighted alone.
  const worldPlanes = [];
  const storeyPlanes = new Map(DATA.storeys.map((s) => [s.id, []]));
  const meshes = [];
  const roomIndex = new Map();
  DATA.elements.filter((e) => e.room).forEach((e, i) => roomIndex.set(e.id, i));

  const ROOM_HUES = [28, 190, 95, 45, 160, 210, 12, 130];
  function roomColour(id) {
    const hue = ROOM_HUES[(roomIndex.get(id) || 0) % ROOM_HUES.length];
    const light = darkQuery.matches ? 32 : 76;
    return new THREE.Color(`hsl(${hue}, 40%, ${light}%)`);
  }

  function baseColour(element) {
    if (element.category === "floor" && $("roomColours").checked) return roomColour(element.id);
    const [r, g, b] = DATA.colours[element.category];
    return new THREE.Color(r / 255, g / 255, b / 255);
  }

  for (const element of DATA.elements) {
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute("position", new THREE.Float32BufferAttribute(element.positions, 3));
    geometry.setIndex(element.indices);
    geometry.computeVertexNormals();
    geometry.computeBoundingSphere();
    const glass = element.category === "window";
    const material = new THREE.MeshLambertMaterial({
      color: baseColour(element),
      side: THREE.DoubleSide,
      transparent: glass,
      opacity: glass ? 0.75 : 1,
      clippingPlanes: storeyPlanes.get(element.storey),
      polygonOffset: element.category === "floor",
      polygonOffsetFactor: 1,
      polygonOffsetUnits: 1,
    });
    const mesh = new THREE.Mesh(geometry, material);
    mesh.userData = element;
    storeyGroups.get(element.storey).add(mesh);
    meshes.push(mesh);
  }

  const bounds = new THREE.Box3();
  meshes.forEach((m) => bounds.expandByObject(m));
  if (bounds.isEmpty()) bounds.set(new THREE.Vector3(0, 0, 0), new THREE.Vector3(10, 3, 10));
  const centre = bounds.getCenter(new THREE.Vector3());
  const radius = Math.max(bounds.getSize(new THREE.Vector3()).length() / 2, 1);

  let grid = null;
  function buildGrid() {
    if (grid) scene.remove(grid);
    const size = Math.ceil(radius * 4);
    grid = new THREE.GridHelper(size, size, themeColour("--grid"), themeColour("--grid"));
    grid.position.set(centre.x, bounds.min.y - 0.01, centre.z);
    grid.material.transparent = true;
    grid.material.opacity = 0.6;
    scene.add(grid);
  }

  function applyTheme() {
    scene.background = themeColour("--scene");
    buildGrid();
    meshes.forEach((m) => m.material.color.copy(baseColour(m.userData)));
    refreshHighlights();
  }

  // Visibility of layers and storeys.
  const layerOn = Object.fromEntries(CATEGORY_ORDER.map((c) => [c, true]));
  const storeyOn = new Map(storeys.map((s) => [s.id, true]));
  let solo = null;

  function refreshVisibility() {
    for (const mesh of meshes) {
      const element = mesh.userData;
      const storeyShown = solo ? solo === element.storey : storeyOn.get(element.storey);
      mesh.visible = layerOn[element.category] && storeyShown;
    }
    if (selected && !selected.visible) select(null);
  }

  function row(label, count, checked, onChange) {
    const wrap = document.createElement("label");
    wrap.className = "row";
    const box = document.createElement("input");
    box.type = "checkbox";
    box.checked = checked;
    box.addEventListener("change", () => onChange(box.checked));
    const text = document.createElement("span");
    text.className = "grow";
    text.textContent = label;
    const num = document.createElement("span");
    num.className = "num";
    num.textContent = String(count);
    wrap.append(box, text, num);
    return wrap;
  }

  for (const category of CATEGORY_ORDER) {
    const count = DATA.elements.filter((e) => e.category === category).length;
    if (!count) continue;
    $("layers").append(
      row(CATEGORY_NAMES[category], count, true, (on) => {
        layerOn[category] = on;
        refreshVisibility();
      }),
    );
  }

  const soloButtons = new Map();
  for (const storey of [...storeys].reverse()) {
    const line = document.createElement("div");
    line.className = "storey";
    const box = document.createElement("input");
    box.type = "checkbox";
    box.checked = true;
    box.setAttribute("aria-label", `显示 ${storey.name}`);
    box.addEventListener("change", () => {
      storeyOn.set(storey.id, box.checked);
      refreshVisibility();
    });
    const text = document.createElement("div");
    text.textContent = storey.name;
    const detail = document.createElement("small");
    detail.textContent = `标高 ${storey.elevation_m.toFixed(2)} m，层高 ${storey.height_m.toFixed(2)} m`;
    text.append(detail);
    const button = document.createElement("button");
    button.type = "button";
    button.className = "small";
    button.textContent = "单独";
    button.setAttribute("aria-pressed", "false");
    button.addEventListener("click", () => {
      solo = solo === storey.id ? null : storey.id;
      soloButtons.forEach((b, id) => b.setAttribute("aria-pressed", String(id === solo)));
      refreshVisibility();
    });
    soloButtons.set(storey.id, button);
    line.append(box, text, button);
    $("storeys").append(line);
  }

  $("roomColours").addEventListener("change", applyTheme);

  $("explode").addEventListener("input", (event) => {
    const value = Number(event.target.value);
    storeyGroups.forEach((group) => {
      group.position.y = group.userData.index * value * STOREY_GAP_M;
    });
    $("explodeOut").textContent = `${(value * STOREY_GAP_M).toFixed(1)} m`;
    refreshClipping();
  });

  // Sections. World planes cut east-west and north-south; the plan cut sits at
  // the same height above every storey floor. A plane keeps the side its normal points to.
  const maxStoreyHeight = Math.max(...storeys.map((s) => s.height_m), 1);
  const sections = [
    { label: "东西", axis: "x", min: bounds.min.x, max: bounds.max.x, flip: false, enabled: false },
    { label: "南北", axis: "z", min: bounds.min.z, max: bounds.max.z, flip: false, enabled: false },
    { label: "平面", axis: "plan", min: 0, max: maxStoreyHeight, flip: false, enabled: false },
  ];
  sections.forEach((section) => {
    section.position = section.axis === "plan" ? Math.min(1.2, section.max) : section.max;
  });

  function refreshClipping() {
    worldPlanes.length = 0;
    for (const section of sections) {
      if (!section.enabled || section.axis === "plan") continue;
      const normal = new THREE.Vector3();
      normal[section.axis] = section.flip ? 1 : -1;
      worldPlanes.push(new THREE.Plane(normal, section.flip ? -section.position : section.position));
    }
    const plan = sections[2];
    for (const storey of storeys) {
      const planes = storeyPlanes.get(storey.id);
      planes.length = 0;
      planes.push(...worldPlanes);
      if (plan.enabled) {
        const floor = storey.elevation_m + storeyGroups.get(storey.id).position.y;
        planes.push(new THREE.Plane(new THREE.Vector3(0, -1, 0), floor + plan.position));
      }
    }
    meshes.forEach((m) => {
      m.material.needsUpdate = true;
    });
    refreshReferenceClipping();
  }

  function sectionText(section) {
    const value = section.axis === "plan" ? section.position : section.position - section.min;
    return `${value.toFixed(2)} m`;
  }

  for (const section of sections) {
    const line = document.createElement("div");
    line.className = "section-row";
    const box = document.createElement("input");
    box.type = "checkbox";
    box.setAttribute("aria-label", `启用${section.label}剖切`);
    const name = document.createElement("span");
    name.textContent = section.label;
    const slider = document.createElement("input");
    slider.type = "range";
    slider.min = "0";
    slider.max = "1";
    slider.step = "0.005";
    slider.value = String((section.position - section.min) / (section.max - section.min || 1));
    slider.setAttribute("aria-label", `${section.label}剖切位置`);
    const output = document.createElement("output");
    output.textContent = sectionText(section);
    box.addEventListener("change", () => {
      section.enabled = box.checked;
      refreshClipping();
    });
    slider.addEventListener("input", () => {
      section.position = section.min + Number(slider.value) * (section.max - section.min);
      output.textContent = sectionText(section);
      if (!section.enabled) {
        section.enabled = true;
        box.checked = true;
      }
      refreshClipping();
    });
    line.append(box, name, slider, output);
    if (section.axis !== "plan") {
      const flip = document.createElement("button");
      flip.type = "button";
      flip.className = "small";
      flip.textContent = "翻转";
      flip.setAttribute("aria-pressed", "false");
      flip.addEventListener("click", () => {
        section.flip = !section.flip;
        flip.setAttribute("aria-pressed", String(section.flip));
        refreshClipping();
      });
      line.append(flip);
    }
    $("sections").append(line);
  }

  function clippedAway(point, storeyId) {
    return storeyPlanes.get(storeyId).some((plane) => plane.distanceToPoint(point) < 0);
  }

  // Picking.
  const raycaster = new THREE.Raycaster();
  const pointer = new THREE.Vector2();

  function pick(event) {
    const rect = renderer.domElement.getBoundingClientRect();
    pointer.set(
      ((event.clientX - rect.left) / rect.width) * 2 - 1,
      -((event.clientY - rect.top) / rect.height) * 2 + 1,
    );
    raycaster.setFromCamera(pointer, camera);
    const visible = meshes.filter((m) => m.visible);
    return (
      raycaster
        .intersectObjects(visible, false)
        .find((hit) => !clippedAway(hit.point, hit.object.userData.storey)) || null
    );
  }

  let hovered = null;
  let selected = null;

  function refreshHighlights() {
    const accent = themeColour("--accent");
    for (const mesh of meshes) {
      mesh.material.emissive.setRGB(0, 0, 0);
      if (mesh === hovered) mesh.material.emissive.copy(accent).multiplyScalar(0.25);
      if (mesh === selected) mesh.material.emissive.copy(accent).multiplyScalar(0.5);
    }
  }

  function select(mesh) {
    selected = mesh;
    refreshHighlights();
    const panel = $("inspector");
    panel.replaceChildren();
    if (!mesh) {
      panel.hidden = true;
      return;
    }
    const element = mesh.userData;
    const close = document.createElement("button");
    close.type = "button";
    close.className = "small close";
    close.textContent = "关闭";
    close.addEventListener("click", () => select(null));
    const title = document.createElement("h3");
    title.textContent = element.title;
    const kind = document.createElement("p");
    kind.className = "kind";
    kind.textContent = `${CATEGORY_NAMES[element.category]}，${storeyById.get(element.storey).name}`;
    const list = (pairs) => {
      const dl = document.createElement("dl");
      for (const [key, value] of pairs) {
        const dt = document.createElement("dt");
        dt.textContent = key;
        const dd = document.createElement("dd");
        dd.textContent = value;
        dl.append(dt, dd);
      }
      return dl;
    };
    const trace = document.createElement("details");
    const summary = document.createElement("summary");
    summary.textContent = "内部编号与记录";
    trace.append(summary, list(element.trace));
    panel.append(close, title, kind, list(element.fields), trace);
    panel.hidden = false;
  }

  // Measuring with vertex snapping.
  const measureGroup = new THREE.Group();
  scene.add(measureGroup);
  const overlay = (colour) =>
    new THREE.MeshBasicMaterial({ color: colour, depthTest: false, transparent: true });
  const snapMarker = new THREE.Mesh(new THREE.SphereGeometry(radius * 0.008, 16, 12), overlay(0xffffff));
  snapMarker.renderOrder = 10;
  snapMarker.visible = false;
  scene.add(snapMarker);
  let measuring = false;
  let measurePoints = [];
  let measureLabel = null;

  function snapPoint(hit, event) {
    const rect = renderer.domElement.getBoundingClientRect();
    const position = hit.object.geometry.getAttribute("position");
    const world = new THREE.Vector3();
    const screen = new THREE.Vector3();
    let best = null;
    let bestDistance = SNAP_PX;
    for (let i = 0; i < position.count; i += 1) {
      world.fromBufferAttribute(position, i).applyMatrix4(hit.object.matrixWorld);
      screen.copy(world).project(camera);
      const x = ((screen.x + 1) / 2) * rect.width + rect.left;
      const y = ((1 - screen.y) / 2) * rect.height + rect.top;
      const distance = Math.hypot(x - event.clientX, y - event.clientY);
      if (distance < bestDistance) {
        bestDistance = distance;
        best = world.clone();
      }
    }
    return best || hit.point.clone();
  }

  function clearMeasure() {
    measureGroup.clear();
    measurePoints = [];
    if (measureLabel) measureLabel.remove();
    measureLabel = null;
  }

  function addMeasurePoint(point) {
    if (measurePoints.length === 2) clearMeasure();
    measurePoints.push(point);
    const dot = new THREE.Mesh(new THREE.SphereGeometry(radius * 0.01, 16, 12), overlay(themeColour("--accent")));
    dot.position.copy(point);
    dot.renderOrder = 10;
    measureGroup.add(dot);
    if (measurePoints.length === 2) {
      const [a, b] = measurePoints;
      const line = new THREE.Line(
        new THREE.BufferGeometry().setFromPoints([a, b]),
        new THREE.LineBasicMaterial({ color: themeColour("--accent"), depthTest: false }),
      );
      line.renderOrder = 10;
      measureGroup.add(line);
      const horizontal = Math.hypot(b.x - a.x, b.z - a.z);
      measureLabel = document.createElement("div");
      measureLabel.className = "measure-label";
      measureLabel.textContent =
        `${a.distanceTo(b).toFixed(2)} m（水平 ${horizontal.toFixed(2)}，竖向 ${Math.abs(b.y - a.y).toFixed(2)}）`;
      $("labels").append(measureLabel);
    }
  }

  function setMeasuring(on) {
    measuring = on;
    $("measure").setAttribute("aria-pressed", String(on));
    $("hint").textContent = on
      ? "测量中：依次点击两点，靠近顶点时自动吸附。按 Esc 退出。"
      : "点击构件查看属性。拖动旋转，右键平移，滚轮缩放。";
    snapMarker.visible = false;
    if (!on) clearMeasure();
  }

  $("measure").addEventListener("click", () => setMeasuring(!measuring));

  let downAt = null;
  renderer.domElement.addEventListener("pointerdown", (event) => {
    downAt = { x: event.clientX, y: event.clientY };
  });
  renderer.domElement.addEventListener("pointerup", (event) => {
    if (!downAt || Math.hypot(event.clientX - downAt.x, event.clientY - downAt.y) > CLICK_PX) return;
    const hit = pick(event);
    if (measuring) {
      if (hit) addMeasurePoint(snapPoint(hit, event));
      return;
    }
    select(hit ? hit.object : null);
  });
  renderer.domElement.addEventListener("pointermove", (event) => {
    if (event.buttons) return;
    const hit = pick(event);
    if (measuring) {
      snapMarker.visible = Boolean(hit);
      if (hit) snapMarker.position.copy(snapPoint(hit, event));
      return;
    }
    const next = hit ? hit.object : null;
    if (next !== hovered) {
      hovered = next;
      renderer.domElement.style.cursor = hovered ? "pointer" : "";
      refreshHighlights();
    }
  });
  window.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    if (measuring) setMeasuring(false);
    else select(null);
  });

  // Reference mesh overlay for mesh-mode buildings.
  let reference = null;
  function refreshReferenceClipping() {
    if (!reference) return;
    reference.traverse((node) => {
      if (!node.isMesh) return;
      (Array.isArray(node.material) ? node.material : [node.material]).forEach((m) => {
        m.clippingPlanes = worldPlanes;
        m.needsUpdate = true;
      });
    });
  }
  if (DATA.reference) {
    $("reference").hidden = false;
    let loading = false;
    const setOpacity = () => {
      const value = Number($("referenceOpacity").value);
      $("referenceOut").textContent = `${Math.round(value * 100)}%`;
      if (!reference) return;
      reference.traverse((node) => {
        if (!node.isMesh) return;
        const materials = Array.isArray(node.material) ? node.material : [node.material];
        materials.forEach((m) => {
          m.transparent = value < 1;
          m.opacity = value;
          m.depthWrite = value >= 1;
          m.clippingPlanes = worldPlanes;
        });
      });
    };
    $("referenceOpacity").addEventListener("input", setOpacity);
    $("referenceOn").addEventListener("change", () => {
      const on = $("referenceOn").checked;
      if (reference) {
        reference.visible = on;
        return;
      }
      if (!on || loading) return;
      loading = true;
      $("referenceStatus").textContent = "正在加载原始网格。";
      new THREE.GLTFLoader().load(
        `assets/${DATA.reference.file}`,
        (gltf) => {
          reference = gltf.scene;
          const matrix = new THREE.Matrix4();
          matrix.set(...DATA.reference.matrix);
          reference.matrixAutoUpdate = false;
          reference.matrix.copy(matrix);
          reference.visible = $("referenceOn").checked;
          scene.add(reference);
          setOpacity();
          $("referenceStatus").textContent = "原始网格已叠加，随东西向和南北向剖切。";
        },
        undefined,
        () => {
          loading = false;
          $("referenceOn").checked = false;
          $("referenceStatus").textContent =
            location.protocol === "file:"
              ? "浏览器禁止从本地文件加载网格，请通过查看服务（http）打开本页。"
              : `未能加载 assets/${DATA.reference.file}。`;
        },
      );
    });
  }

  // View, screenshot, compass and panel.
  function resetView() {
    const distance = radius / Math.sin(THREE.MathUtils.degToRad(camera.fov / 2)) * 1.05;
    const direction = new THREE.Vector3(0.75, 0.6, 1).normalize();
    camera.position.copy(centre).addScaledVector(direction, distance);
    camera.near = distance / 500;
    camera.far = distance * 20;
    camera.updateProjectionMatrix();
    controls.target.copy(centre);
    controls.update();
  }
  $("resetView").addEventListener("click", resetView);

  $("screenshot").addEventListener("click", () => {
    renderer.render(scene, camera);
    const link = document.createElement("a");
    link.download = `${DATA.name}.png`;
    link.href = renderer.domElement.toDataURL("image/png");
    link.click();
  });

  $("panelToggle").addEventListener("click", () => $("panel").classList.toggle("open"));

  const needle = document.querySelector("#compass .needle");
  const northLabel = document.querySelector("#compass .north");
  const north = new THREE.Vector3();
  const origin = new THREE.Vector3();
  function updateCompass() {
    origin.copy(controls.target).project(camera);
    north.copy(controls.target).add(new THREE.Vector3(0, 0, -1)).project(camera);
    const angle = Math.atan2(north.x - origin.x, north.y - origin.y);
    needle.style.transform = `rotate(${angle}rad)`;
    northLabel.style.transform =
      `translate(${Math.sin(angle) * 19}px, ${-Math.cos(angle) * 19}px)`;
  }

  const labelPoint = new THREE.Vector3();
  function updateLabels() {
    if (!measureLabel) return;
    const rect = renderer.domElement.getBoundingClientRect();
    labelPoint.addVectors(measurePoints[0], measurePoints[1]).multiplyScalar(0.5).project(camera);
    measureLabel.style.left = `${((labelPoint.x + 1) / 2) * rect.width}px`;
    measureLabel.style.top = `${((1 - labelPoint.y) / 2) * rect.height}px`;
  }

  function resize() {
    const { clientWidth, clientHeight } = stage;
    renderer.setSize(clientWidth, clientHeight);
    camera.aspect = clientWidth / Math.max(clientHeight, 1);
    camera.updateProjectionMatrix();
  }
  new ResizeObserver(resize).observe(stage);

  // Text panels.
  document.title = `${DATA.name}，Sim-BIM 查看器`;
  $("name").textContent = DATA.name;
  const count = (category) => DATA.elements.filter((e) => e.category === category).length;
  $("meta").textContent =
    `${storeys.length} 层，${count("floor")} 个空间，${count("window") + count("door")} 个门窗`;

  function fill(listId, countId, items, emptyText) {
    const list = $(listId);
    $(countId).textContent = String(items.length);
    if (!items.length) {
      const li = document.createElement("li");
      li.className = "empty";
      li.textContent = emptyText;
      list.append(li);
      return;
    }
    for (const item of items) {
      const li = document.createElement("li");
      li.textContent = item;
      list.append(li);
    }
  }
  fill("issueList", "issueCount", DATA.unresolved, "没有待处理事项。");
  fill("assumptionList", "assumptionCount", DATA.assumptions, "没有记录假设。");

  darkQuery.addEventListener("change", applyTheme);
  applyTheme();
  refreshVisibility();
  resize();
  resetView();
  renderer.setAnimationLoop(() => {
    controls.update();
    updateCompass();
    updateLabels();
    renderer.render(scene, camera);
  });
})();
