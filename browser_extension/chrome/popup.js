import {parseComplaint, propose} from './engine.js';
const $ = id => document.getElementById(id);
let complaint = null, scan = null, tabId = null, rows = [];
function status(text) { $('status').textContent = text; }
function invalidate() { scan = null; rows = []; $('fields').replaceChildren(); $('approved').checked = false; $('fill').disabled = true; $('suggested').disabled = true; }
function showCase() {
  $('case').textContent = complaint ? `Complaint ${complaint.complaint_id} · ${complaint.values['business.name'] || 'Business not supplied'}` : 'No complaint loaded.';
  $('jurisdiction').textContent = complaint ? `Intended jurisdiction: ${complaint.values['jurisdiction.country'] || 'not supplied'} / ${complaint.values['jurisdiction.state'] || 'not supplied'}` : '';
}
async function agent(method, args) {
  const result = await chrome.scripting.executeScript({target:{tabId}, func:async (m,a) => {
    try { return {ok:true, value:await globalThis.__cwFormAgent[m](a)}; }
    catch (e) { return {ok:false, error:e.message}; }
  }, args:[method, args || null]});
  if (!result[0]?.result?.ok) throw new Error(result[0]?.result?.error || 'Page unavailable. Reopen the extension and discover again.');
  return result[0].result.value;
}
function element(tag, text, cls) { const e=document.createElement(tag); if(text !== undefined)e.textContent=text; if(cls)e.className=cls; return e; }
function renderFields() {
  rows = [];
  for (const f of scan.fields) {
    const suggestion = propose(f, complaint.values);
    const box = element('div',undefined,'field');
    const tick=document.createElement('input'); tick.type='checkbox';
    const heading=element('h3'); heading.append(tick,document.createTextNode(f.label)); box.append(heading);
    box.append(element('div',`${f.section || 'No party section detected'} · ${f.type}`, 'meta'));
    if(f.currentValue) box.append(element('div',`Currently: ${f.currentValue.slice(0,160)}`, 'existing'));
    const source=document.createElement('select'); source.setAttribute('aria-label',`Data source for ${f.label}`);
    source.append(new Option('Choose a CW field or enter a value below',''));
    for(const [key,value] of Object.entries(complaint.values)) if(value.trim()) source.append(new Option(key,key));
    const value=f.tag === 'select' ? document.createElement('select') : document.createElement('textarea');
    value.setAttribute('aria-label',`Proposed value for ${f.label}`);
    if(f.tag==='select') { value.append(new Option('Choose an available option','')); for(const o of f.options){const opt=new Option(o.label,o.value);opt.disabled=o.disabled;value.append(opt);} }
    if(suggestion) { source.value=suggestion.key; value.value=suggestion.value; }
    const reason=element('div', suggestion ? `${Math.round(suggestion.confidence*100)}% rule score · ${suggestion.reason}` : 'No reliable suggestion — choose a source or enter a value.', 'meta');
    box.append(source,value,reason);
    const overwrite=document.createElement('input'); overwrite.type='checkbox';
    if(f.currentValue) {const label=element('label');label.append(overwrite,document.createTextNode('Replace this existing value'));box.append(label);}
    source.onchange=()=>{
      const proposed=complaint.values[source.value] || '';
      if(f.tag==='select') {const options=f.options.filter(o=>!o.disabled && (o.value===proposed || o.label.toLowerCase()===proposed.toLowerCase()));value.value=options.length===1?options[0].value:'';}
      else value.value=proposed;
      $('approved').checked=false; $('fill').disabled=true;
    };
    for(const control of [tick,value,overwrite])control.addEventListener('input',()=>{$('approved').checked=false;$('fill').disabled=true;});
    rows.push({f,tick,source,value,overwrite,suggestion}); $('fields').append(box);
  }
  $('suggested').disabled=false;
}
$('approved').onchange=()=>{$('fill').disabled=!scan || !$('approved').checked;};
$('suggested').onclick=()=>{
  for(const row of rows)row.tick.checked=!!row.suggestion && row.suggestion.confidence>=.9 && !row.f.currentValue;
  $('approved').checked=false; $('fill').disabled=true;
};
$('import').onchange=async()=>{
  try {
    const file=$('import').files[0]; if(!file)return;
    if(file.size>500000)throw new Error('Complaint file exceeds 500 KB.');
    const imported=parseComplaint(JSON.parse(await file.text()));
    await chrome.storage.session.set({complaint:imported}); complaint=imported; invalidate();showCase();status('Complaint loaded. Discover the agency form next.');
  }catch(e){status(e.message);}
};
$('forget').onclick=async()=>{await chrome.storage.session.clear();complaint=null;invalidate();showCase();$('import').value='';status('Complaint removed from extension memory. Already filled website fields and your downloaded JSON file are unchanged.');};
$('scan').onclick=async()=>{
  invalidate();
  try {
    if(!complaint)throw new Error('Import a complaint first.');
    const [tab]=await chrome.tabs.query({active:true,currentWindow:true}); tabId=tab.id;
    await chrome.scripting.executeScript({target:{tabId}, files:['page-agent.js']});
    scan=await agent('scan');
    $('destination').textContent=`Destination: ${scan.origin}\n${scan.title}`;
    renderFields();status(`${scan.fields.length} supported fields discovered. ${scan.skippedFrames} inaccessible or cross-origin frames left manual. Review all suggestions.`);
  }catch(e){invalidate();status(e.message);}
};
$('fill').onclick=async()=>{
  if(!$('approved').checked || !scan)return;
  const fields=rows.filter(r=>r.tick.checked).map(r=>({token:r.f.token,value:r.value.value,overwrite:r.overwrite.checked}));
  if(!fields.length){status('Select at least one field.');return;}
  $('fill').disabled=true;
  try {
    const results=await agent('fill',{id:scan.id,fields});
    const messages=results.map(r=>`${rows.find(x=>x.f.token===r.token)?.f.label}: ${r.status} — ${r.message}`);
    status(messages.join('\n')+'\nInspect the website. Discover again for any remaining fields.'); invalidate();
  }catch(e){status(e.message);invalidate();}
};
try {
  const saved=await chrome.storage.session.get('complaint');
  if(saved.complaint)complaint=parseComplaint(saved.complaint);showCase();
}catch(e){status(e.message);}
