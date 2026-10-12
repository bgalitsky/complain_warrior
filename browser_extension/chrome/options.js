import {payload} from './source.js';
import {parseComplaint} from './engine.js';
import {DEFAULT_CW_ORIGIN, normalizeCwOrigin} from './connection.js';
const $=id=>document.getElementById(id);
$('connect').onclick=async()=>{
  try {
    const u=new URL(normalizeCwOrigin($('origin').value));
    $('origin').value=u.origin;
    const granted=await chrome.permissions.request({origins:[u.origin+'/*']});
    if(!granted)throw new Error('CW host permission was not granted.');
    await chrome.storage.local.set({cwOrigin:u.origin,sourceMode:'server'});
    await chrome.storage.session.remove('complaint');
    $('status').textContent='CW address saved: '+u.origin+'. This saves connection settings; it does not check the server yet. Sign in to CW, select a complaint, then click CW on the agency form.';
  }catch(e){$('status').textContent=e.message;}
};
$('import').onchange=async()=>{
  try {
    const f=$('import').files[0];if(!f)return;
    if(f.size>500000)throw new Error('JSON file exceeds 500 KB.');
    const data=parseComplaint(JSON.parse(await f.text()));
    await chrome.storage.session.set({complaint:data});await chrome.storage.local.set({sourceMode:'json'});
    $('status').textContent=`Complaint ${data.complaint_id} loaded. Click CW on the third-party form to fill automatically.`;
  }catch(e){$('status').textContent=e.message;}
};
$('forget').onclick=async()=>{
  const {cwOrigin}=await chrome.storage.local.get('cwOrigin');
  if(cwOrigin)await chrome.permissions.remove({origins:[cwOrigin+'/*']});
  await chrome.storage.session.clear();await chrome.storage.local.remove(['cwOrigin','sourceMode']);
  $('origin').value='';$('import').value='';$('status').textContent='Disconnected. Website fields and downloaded files are unchanged.';
};
const config=await chrome.storage.local.get(['cwOrigin','sourceMode']);$('origin').value=config.cwOrigin || DEFAULT_CW_ORIGIN;
const {lastResult}=await chrome.storage.session.get('lastResult');$('status').textContent=lastResult || 'Complete one-time setup above.';

$('test').onclick=async()=>{
  $('status').textContent='Checking saved data source…';
  try {
    const data=await payload();
    $('status').textContent=`Ready: complaint ${data.complaint_id}, ${Object.values(data.values).filter(v=>v.trim()).length} nonempty values available. Return to the form tab and click the CW toolbar icon.`;
  } catch(e) { $('status').textContent=e.message; }
};
