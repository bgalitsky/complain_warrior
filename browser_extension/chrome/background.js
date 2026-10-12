import {autoPlan} from './engine.js';
import {payload, CWError} from './source.js';
const busy = new Set();
async function call(tabId, method, args=null) {
  const results=await chrome.scripting.executeScript({target:{tabId},func:async(m,a)=>{
    try{return {ok:true,value:await globalThis.__cwFormAgent[m](a)};}
    catch(e){return {ok:false,error:e.message};}
  },args:[method,args]});
  if(!results[0]?.result?.ok)throw new Error(results[0]?.result?.error || 'The page changed. Click CW again.');
  return results[0].result.value;
}
chrome.runtime.onInstalled.addListener(async details=>{
  await chrome.storage.local.remove(['cwOrigin','sourceMode']);
  await chrome.storage.session.clear();
  if(details.reason==='install')await chrome.runtime.openOptionsPage();
});
chrome.action.onClicked.addListener(async tab=>{
  if(busy.has(tab.id))return;
  busy.add(tab.id);
  let injected=false;
  try {
    await chrome.action.setBadgeText({tabId:tab.id,text:'…'});
    try { await chrome.scripting.executeScript({target:{tabId:tab.id},files:['page-agent.js']}); }
    catch { throw new CWError('UNSUPPORTED_FORM','This page cannot be filled. Open a regular website form and click CW there.'); }
    injected=true;
    await call(tab.id,'report','CW: loading your selected complaint…');
    const data=await payload();
    const scan=await call(tab.id,'scan');
    if(tab.url && scan.url !== tab.url)throw new Error('The tab navigated while CW was loading. Click CW again on the intended form.');
    if(!scan.fields.length)throw new CWError('UNSUPPORTED_FORM','No accessible supported form fields found. Open the form itself or move to its next page, then click CW.');
    const plan=autoPlan(scan.fields,data.values);
    if(!plan.fields.length)throw new CWError('NO_MATCHES',`Complaint ${data.complaint_id} is ready, but no blank fields could be matched. Existing values were preserved; fill unmatched fields manually.`);
    const results=plan.fields.length ? await call(tab.id,'fill',{id:scan.id,fields:plan.fields}) : [];
    const filled=results.filter(r=>r.status==='filled').length;
    const warnings=results.filter(r=>r.status!=='filled' || !r.message.startsWith('Value verified'));
    await chrome.action.setTitle({tabId:tab.id,title:'CW: automatically fill this form'});
    await chrome.action.setBadgeText({tabId:tab.id,text:String(filled)});
    await chrome.action.setBadgeBackgroundColor({tabId:tab.id,color:'#166b42'});
    await call(tab.id,'report',`CW filled ${filled} fields from complaint ${data.complaint_id}.\nReview and edit this website before submitting. Existing entries were preserved.\n${scan.fields.length-filled} discovered fields left unchanged; ${scan.skippedFrames} inaccessible frames. ${warnings.length ? 'Some values need checking. ' : ''}Files, declarations and Submit stay manual.`);
    await chrome.storage.session.set({lastResult:`Complaint ${data.complaint_id}: ${filled} fields filled. Review the website.`});
  } catch(e) {
    const message=e.message || 'Autofill could not finish. Check the page and Settings.';
    await chrome.action.setBadgeText({tabId:tab.id,text:'!'});
    await chrome.action.setBadgeBackgroundColor({tabId:tab.id,color:'#a33030'});
    await chrome.action.setTitle({tabId:tab.id,title:message});
    await chrome.storage.session.set({lastResult:message,lastCode:e.code || 'FILL_FAILED'});
    if(injected) {try{await call(tab.id,'report','CW: '+message);}catch{}}
    else await chrome.runtime.openOptionsPage();
  } finally {busy.delete(tab.id);}
});
