import {element as el} from './dom.js';

function numbers(text) {
  const tokens = text.trim().split(/[\s,，;；]+/).filter(Boolean);
  if (!tokens.length || tokens.some(token => !/^(?:\d+(?:\.\d*)?|\.\d+)$/.test(token))) return null;
  const values = tokens.map(Number);
  return values.every(value => Number.isFinite(value) && value > 0) ? values : null;
}

function setChoices(group, chosen) {
  const available = new Set([...group.presets, ...chosen]);
  for (const [value, input] of group.inputs) {
    if (available.has(value)) continue;
    if (document.activeElement === input) group.custom.focus();
    input.parentElement.remove();
    group.inputs.delete(value);
  }
  for (const value of available) {
    let input = group.inputs.get(value);
    if (!input) {
      const chip = el('label');
      input = el('input'); input.type = 'checkbox'; input.value = String(value);
      chip.append(input, el('span','',value)); group.chips.append(chip);
      group.inputs.set(value, input);
    }
    input.checked = chosen.includes(value);
  }
}

export function thicknessPicker(container, t, change, updateRun) {
  const groups = new Map();
  let workspace;
  const checked = group => [...group.inputs.values()].filter(input => input.checked).map(input => Number(input.value));

  function commit(group, values) {
    setChoices(group, values);
    const selected = {};
    for (const [key, item] of groups) {
      const values = checked(item).sort((a,b) => a-b);
      if (values.length) selected[key] = values;
    }
    change(selected);
  }
  function validate(custom) {
    const invalid = custom.value.trim() && !numbers(custom.value);
    custom.setCustomValidity(invalid ? t('invalidThickness') : '');
    if (invalid) custom.setAttribute('aria-invalid', 'true');
    else custom.removeAttribute('aria-invalid');
  }
  function addTyped(group) {
    const {custom} = group;
    if (!custom.value.trim()) return;
    validate(custom);
    if (!custom.reportValidity()) return;
    const values = numbers(custom.value);
    custom.value = '';
    commit(group, [...checked(group), ...values]);
  }
  function createGroup(key) {
    const field = el('fieldset'), legend = el('legend'), chips = el('div','thickness-values');
    const label = el('label','thickness-custom'), labelText = el('span'), custom = el('input');
    const group = {field, legend, chips, labelText, custom, inputs:new Map()};
    field.dataset.group = key; custom.type = 'text'; custom.autocomplete = 'off'; custom.inputMode = 'decimal';
    chips.addEventListener('change', () => commit(group, checked(group)));
    custom.addEventListener('input', () => { validate(custom); updateRun(); });
    custom.addEventListener('keydown', event => {
      if (event.key !== 'Enter' || event.isComposing) return;
      event.preventDefault();
      addTyped(group);
    });
    label.append(labelText,custom); field.append(legend,chips,label); container.append(field);
    return group;
  }
  function sync(presets, selected, activeWorkspace, disabled) {
    for (const [key, values] of Object.entries(presets || {})) {
      if (!groups.has(key)) groups.set(key, createGroup(key));
      const group = groups.get(key);
      group.presets = values; group.field.disabled = disabled;
      group.legend.textContent = t(key+'Thickness'); group.labelText.textContent = t('customThickness');
      group.custom.placeholder = t('customThicknessHint');
      if (workspace !== activeWorkspace) group.custom.value = '';
      validate(group.custom);
      setChoices(group, selected?.[key] || []);
    }
    workspace = activeWorkspace;
  }
  function finishInput() {
    for (const group of groups.values()) addTyped(group);
  }
  return {sync, finishInput, valid: () => [...groups.values()].every(group => group.custom.validity.valid)};
}
