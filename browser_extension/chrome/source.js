import {parseComplaint} from './engine.js';
export async function payload() {
  const config=await chrome.storage.local.get(['cwOrigin','sourceMode']);
  if(config.sourceMode==='json') {
    const {complaint}=await chrome.storage.session.get('complaint');
    if(!complaint)throw new Error('Import a JSON file once in extension Settings, or connect CW.');
    return parseComplaint(complaint);
  }
  if(!config.cwOrigin)throw new Error('Open extension Settings once to connect your CW HTTPS address.');
  const url=new URL(config.cwOrigin);
  if(url.protocol!=='https:' || url.origin!==config.cwOrigin)throw new Error('Invalid CW address. Update Settings.');
  if(!await chrome.permissions.contains({origins:[url.origin+'/*']}))throw new Error('Grant access to your CW server in Settings.');
  let response;
  try { response=await fetch(url.origin+'/auth/extension/complaint',{
    credentials:'include',cache:'no-store',redirect:'error',headers:{'X-CW-Extension':'1'},signal:AbortSignal.timeout(15000)
  }); } catch { throw new Error('Cannot reach CW. Check the saved address, network and server, then use Test connection in CW extension Settings.'); }
  if(response.status===401)throw new Error('Sign in to CW in this browser, select a complaint, then click CW again.');
  if(response.status===404)throw new Error('Select a complaint in CW first. If already selected, install the server bridge.');
  if(!response.ok)throw new Error('CW could not provide the complaint. Check the server bridge.');
  // Never fall back to another account's cached JSON on an authentication failure.
  let data;
  try { data=await response.json(); } catch { throw new Error('CW returned a webpage instead of complaint JSON. Check that /auth/extension/complaint routes to the Flask server bridge, then sign in again.'); }
  return parseComplaint(data);
}
