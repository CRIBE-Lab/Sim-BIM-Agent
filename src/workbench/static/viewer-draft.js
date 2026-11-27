// Shared by the single-file viewer and the local workbench's sandbox host.
const SimBimViewerDraft = (() => {
  const channelName = 'sim-bim:viewer-draft:v1';
  const resultPath = /^\/results\/[a-f0-9]{16}\/run-\d{8}-\d{6}-[a-f0-9]{8}\/Sim-BIM\.html$/;
  const modelId = /^[a-f0-9]{64}$/;
  const empty = () => ({northAngle:null, notes:[], instructions:''});

  function validTarget(t) {
    return t && ['zone','floor','surface'].includes(t.kind) &&
      ['id','label','zone','surface'].every(k => typeof t[k] === 'string') &&
      Array.isArray(t.point) && t.point.length === 3 && t.point.every(Number.isFinite);
  }
  function fields(value) {
    const patch = {};
    if (!value || typeof value !== 'object') return patch;
    if (value.northAngle === null || (Number.isFinite(value.northAngle) && value.northAngle >= 0 && value.northAngle < 360))
      patch.northAngle = value.northAngle;
    if (typeof value.instructions === 'string') patch.instructions = value.instructions;
    if (Array.isArray(value.notes)) {
      patch.notes = value.notes.filter(n => n && typeof n.id === 'string' && typeof n.text === 'string' &&
        Array.isArray(n.targets) && n.targets.length && n.targets.every(validTarget))
        .map(n => ({id:n.id, text:n.text, targets:n.targets.map(t => ({
          kind:t.kind, id:t.id, label:t.label, zone:t.zone, surface:t.surface, point:[...t.point],
        }))}));
    }
    return patch;
  }

  function applyState(state, patch) {
    const notesChanged = Array.isArray(patch.notes) && patch.notes !== state.notes &&
      JSON.stringify(patch.notes) !== JSON.stringify(state.notes);
    if (notesChanged) state.notes = patch.notes;
    if ('northAngle' in patch) state.northAngle = patch.northAngle;
    if ('instructions' in patch) state.instructions = patch.instructions;
    return notesChanged;
  }

  function localDraft(path, model, notify) {
    const key = channelName + ':' + path + ':' + model;
    const state = empty();
    let storage, persistent = false, lastRaw, lastValue, unsaved = {};
    function read() {
      const raw = storage.getItem(key);
      if (raw !== lastRaw) {
        lastRaw = raw;
        try { lastValue = fields(JSON.parse(raw)); }
        catch { lastValue = {}; }
      }
      return lastValue;
    }
    try {
      storage = window.localStorage;
      Object.assign(state, read());
      persistent = true;
    } catch { /* A sandbox or browser preference can deny local storage. */ }
    const onStorage = event => {
      if (event.storageArea !== storage || (event.key !== key && event.key !== null)) return;
      let notesChanged = false;
      try {
        notesChanged = applyState(state, Object.assign(empty(), read(), unsaved));
        persistent = !Object.keys(unsaved).length;
      }
      catch { persistent = false; }
      notify(notesChanged);
    };
    window.addEventListener('storage', onStorage);
    return {
      state, get persistent() { return persistent; },
      dispose() { window.removeEventListener('storage', onStorage); },
      update(changes) {
        const patch = fields(changes);
        Object.assign(unsaved, patch);
        let newState = unsaved;
        if (storage) {
          try {
            // Read the latest copy so editing north does not undo another tab's comments.
            const merged = Object.assign(empty(), read(), unsaved), raw = JSON.stringify(merged);
            storage.setItem(key, raw);
            lastRaw = raw; lastValue = merged; newState = merged;
            unsaved = {}; persistent = true;
          } catch { persistent = false; }
        }
        notify(applyState(state, newState));
      },
    };
  }

  function create(model) {
    const notify = notesChanged => window.dispatchEvent(new CustomEvent('viewer-draft-change', {detail:{notesChanged}}));
    const hosted = resultPath.test(location.pathname) && /^https?:$/.test(location.protocol) &&
      ['127.0.0.1','localhost'].includes(location.hostname);
    const direct = localDraft(location.pathname, model, notify);
    if (direct.persistent || !hosted) return direct;
    if (window.parent === window) {
      // A result served by the workbench has an opaque origin, even in its own tab.
      // Enter the same local host that the workbench normally opens for this result.
      const hostUrl = new URL('/static/viewer.html', location.href);
      if (new URLSearchParams(location.search).get('lang') === 'zh-CN') hostUrl.searchParams.set('lang', 'zh-CN');
      hostUrl.hash = location.pathname;
      location.replace(hostUrl.href);
      return direct;
    }
    direct.dispose();
    const state = empty(), channel = new MessageChannel();
    let persistent = false, connected = false, canRevise = false, next = 0;
    const pending = new Map();
    channel.port1.onmessage = ({data}) => {
      if (!data || data.kind !== 'state') return;
      persistent = data.persistent === true;
      canRevise = data.canRevise === true;
      for (const id of pending.keys()) if (id <= data.ack) pending.delete(id);
      const nextState = Object.assign(empty(), fields(data.value));
      for (const patch of pending.values()) Object.assign(nextState, patch);
      const notesChanged = applyState(state, nextState);
      if (!connected) {
        connected = true;
        for (const [id, patch] of pending) channel.port1.postMessage({id, patch});
      }
      notify(notesChanged);
    };
    window.parent.postMessage({type:channelName, model, revisionControl:true}, new URL(location.href).origin, [channel.port2]);
    return {
      state, get persistent() { return persistent; },
      get canRevise() { return canRevise; },
      reviewRevision() { if (canRevise) channel.port1.postMessage({kind:'reviewRevision'}); },
      update(changes) {
        const patch = fields(changes), id = ++next;
        const notesChanged = applyState(state, patch);
        pending.set(id, patch);
        if (connected) channel.port1.postMessage({id, patch});
        notify(notesChanged);
      },
    };
  }

  function host(frame, path, openViewer, revision) {
    if (!resultPath.test(path)) return false;
    let disconnect;
    window.addEventListener('message', event => {
      const data = event.data, port = event.ports[0];
      if (event.source !== frame.contentWindow || event.origin !== 'null' || !port ||
          !data || data.type !== channelName || typeof data.model !== 'string' || !modelId.test(data.model)) return;
      if (disconnect) disconnect();
      let ack = 0;
      const notify = () => {
        revision?.changed(data.model, store.state, data.revisionControl === true);
        port.postMessage({kind:'state', ack, value:store.state, persistent:store.persistent, canRevise:!!revision});
      };
      const store = localDraft(path, data.model, notify);
      disconnect = () => { store.dispose(); port.close(); };
      port.onmessage = ({data:message}) => {
        if (message?.kind === 'reviewRevision') {revision?.review();return;}
        if (!message || !Number.isSafeInteger(message.id) || message.id <= ack) return;
        ack = message.id;
        store.update(message.patch);
      };
      notify();
    });
    const viewerUrl = new URL(path, location.href);
    if (new URLSearchParams(location.search).get('lang') === 'zh-CN') viewerUrl.searchParams.set('lang', 'zh-CN');
    if (typeof openViewer === 'function') openViewer(viewerUrl);
    else frame.src = viewerUrl.href;
    return true;
  }
  return {create, host};
})();
