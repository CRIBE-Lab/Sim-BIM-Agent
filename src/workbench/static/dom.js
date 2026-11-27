const paths = {
  settings: '<path d="M4 6h8m4 0h4M4 12h3m4 0h9M4 18h10m4 0h2"/><circle cx="14" cy="6" r="2"/><circle cx="9" cy="12" r="2"/><circle cx="16" cy="18" r="2"/>',
  folder: '<path d="M3 7V5a1 1 0 0 1 1-1h5l2 3h9a1 1 0 0 1 1 1v11a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1z"/>',
  chevron: '<path d="m6 9 6 6 6-6"/>', upload: '<path d="M12 16V3m-5 5 5-5 5 5M4 15v4a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-4"/>',
  plus: '<path d="M12 5v14M5 12h14"/>', play: '<path fill="currentColor" stroke="none" d="M7 3.8 20 12 7 20.2z"/>',
  close: '<path d="m6 6 12 12M18 6 6 18"/>',
  grip: '<g fill="currentColor" stroke="none"><circle cx="9" cy="5" r="1.7"/><circle cx="15" cy="5" r="1.7"/><circle cx="9" cy="12" r="1.7"/><circle cx="15" cy="12" r="1.7"/><circle cx="9" cy="19" r="1.7"/><circle cx="15" cy="19" r="1.7"/></g>',
  blocks: '<path d="m12 2-5 2.6v5.8l5 2.6 5-2.6V4.6L12 2zm-5 2.6 5 2.8 5-2.8M12 7.4V13m-5-2.6-5 2.7v5.7l5 2.7 5-2.7v-5.7m-10 0 5 2.8 5-2.8m-5 2.8v5.6m10-11.1 5 2.7v5.7l-5 2.7-5-2.7m0-5.7 5 2.8 5-2.8m-5 2.8v5.6" stroke-width="1.05"/>',
  edit: '<path d="m14 5 5 5M4 20l5-1L20 8a2 2 0 0 0-5-5L4 14z"/>', trash: '<path d="M4 6h16M9 6V4h6v2M6 6l1 14h10l1-14M10 10v6m4-6v6"/>',
  mesh: '<path d="m12 2-9 5v10l9 5 9-5V7zm0 10v10M3 7l9 5 9-5"/>',
  image: '<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="8" cy="9" r="1.5"/><path d="m4 18 5-5 3 3 4-6 5 7"/>',
  declaration: '<path d="M6 3h8l4 4v14H6zM14 3v5h4M9 12h6m-6 4h6"/>',
  external: '<path d="M13 4h7v7m0-7L10 14m0-9H4v15h15v-6"/>',
  download: '<path d="M12 3v13m-5-5 5 5 5-5M4 16v4h16v-4"/>'
};
export function icon(name) {
  const holder = document.createElement('i');
  holder.dataset.icon = name;
  holder.setAttribute('aria-hidden', 'true');
  holder.innerHTML = '<svg viewBox="0 0 24 24" focusable="false">' + (paths[name] || '') + '</svg>';
  return holder;
}
export function element(tag, className, text) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text !== undefined) el.textContent = text;
  return el;
}
export function button(label, onClick, { glyph, style = 'button', iconOnly = false, disabled = false } = {}) {
  const el = element('button', style);
  el.type = 'button';
  el.disabled = disabled;
  if (glyph) el.append(icon(glyph));
  if (!iconOnly) el.append(document.createTextNode(label));
  el.setAttribute('aria-label', label);
  if (iconOnly) el.title = label;
  el.addEventListener('click', onClick);
  return el;
}
export const byId = id => document.getElementById(id);
