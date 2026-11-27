import { button, element as el } from './dom.js';

export function connectionForm(root, { connection, t, save, cancel }) {
  const form=el('form','connection-form');
  form.noValidate=true;
  const addField=(container,labelText,control)=>{
    const label=el('label','dialog-field',labelText);
    label.append(control);
    container.append(label);
  };
  const makeInput=(type,value='')=>{
    const input=el('input');input.type=type;input.value=value;return input;
  };
  const name=makeInput('text',connection?.label), url=makeInput('url',connection?.baseUrl);
  name.placeholder=t('providerHint'); name.autocomplete='off'; name.required=true;
  url.placeholder='https://api.example.com/v1'; url.spellcheck=false; url.autocomplete='off'; url.required=true;
  addField(form,t('providerName'),name);addField(form,t('baseUrl'),url);
  const protocol=makeInput('text',t('chatProtocol'));protocol.readOnly=true;
  addField(form,t('protocol'),protocol);
  const key=makeInput('password');key.spellcheck=false;key.autocomplete='new-password';
  key.placeholder=t(connection?.hasApiKey ? 'keepKey' : 'noAuth');
  addField(form,t('optionalKey'),key);
  const clear=makeInput('checkbox');
  if (connection?.hasApiKey) {
    const row=el('label','check-row clear-key');
    clear.onchange=()=>{key.disabled=clear.checked;if (clear.checked) key.value='';};
    row.append(clear,document.createTextNode(t('clearKey')));form.append(row);
  }
  const ids=el('textarea');ids.rows=2;ids.spellcheck=false;
  ids.value=(connection?.modelIds ?? []).join('\n');
  ids.setAttribute('aria-describedby','model-id-help');
  addField(form,t('modelIds'),ids);
  const help=el('p','hint form-hint',t('modelIdsHint'));help.id='model-id-help';form.append(help);
  const advanced=el('details','advanced-fields'), pair=el('div','parameter-pair');
  advanced.append(el('summary','',t('tokenLimits')),pair);form.append(advanced);
  const context=makeInput('number',connection?.contextWindow ?? 128000), output=makeInput('number',connection?.outputTokens ?? 8192);
  for (const input of [context,output]) {input.min='1';input.step='1';}
  addField(pair,t('contextWindow'),context);addField(pair,t('outputTokens'),output);
  const outputParameter=el('select');
  for (const value of ['auto','max_tokens','max_completion_tokens']) {
    const option=el('option','',value==='auto' ? t('automaticTokenParameter') : value);
    option.value=value;outputParameter.append(option);
  }
  outputParameter.value=connection?.outputTokenParameter ?? 'auto';
  addField(advanced,t('outputTokenParameter'),outputParameter);
  const feedback=el('p','dialog-note');feedback.setAttribute('role','status');
  form.addEventListener('input',()=>{
    if (feedback.classList.contains('error')) {feedback.textContent='';feedback.classList.remove('error');}
  });
  const actions=el('div','dialog-actions');
  const cancelButton=button(t('cancel'),cancel),submit=el('button','button primary',t('saveApi'));
  submit.type='submit';actions.append(cancelButton,submit);form.append(feedback,actions);
  function showError(message,field) {
    feedback.classList.add('error');feedback.textContent=message;
    field?.focus();
  }
  form.addEventListener('submit',async event=>{
    event.preventDefault();
    if (submit.disabled) return;
    if (!name.value.trim()) {showError(t('nameRequired'),name);return;}
    let parsed;
    try {parsed=new URL(url.value.trim());} catch { /* The field below gives a localized correction. */ }
    if (!parsed || !['https:','http:'].includes(parsed.protocol) || parsed.username || parsed.password || parsed.search || parsed.hash) {
      showError(t('invalidUrl'),url);return;
    }
    if (connection?.hasApiKey && parsed.origin!==new URL(connection.baseUrl).origin && !key.value.trim() && !clear.checked) {
      showError(t('changedOrigin'),key);return;
    }
    if (![context,output].every(i=>Number.isSafeInteger(i.valueAsNumber) && i.valueAsNumber>0) || output.valueAsNumber>=context.valueAsNumber) {
      advanced.open=true;showError(t('limitsInvalid'),output);return;
    }
    const payload={
      label:name.value.trim(),baseUrl:url.value.trim(),protocol:'openai-chat',
      modelIds:[...new Set(ids.value.split(/\r?\n/).map(id=>id.trim()).filter(Boolean))],
      contextWindow:context.valueAsNumber,outputTokens:output.valueAsNumber,outputTokenParameter:outputParameter.value
    };
    if (connection) Object.assign(payload,{action:'update',provider:connection.provider,revision:connection.revision});
    else payload.action='create';
    if (key.value.trim()) payload.apiKey=key.value.trim();
    if (clear.checked) payload.clearApiKey=true;
    submit.disabled=true;cancelButton.disabled=true;feedback.textContent=t('statusPending');feedback.classList.remove('error');
    try {await save(payload);} catch (e) {showError(e.message);}
    finally {submit.disabled=false;cancelButton.disabled=false;}
  });
  root.replaceChildren(form);name.focus();
}
