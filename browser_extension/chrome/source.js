import {parseComplaint} from './engine.js';
export const CW_ORIGIN = 'https://app.knowledge-trail.com';
export class CWError extends Error {
  constructor(code, message) { super(message); this.code=code; }
}
export async function payload() {
  // Always request the current cookie-authenticated account. Never reuse local JSON.
  let response;
  try {
    response=await fetch(CW_ORIGIN+'/auth/extension/complaint', {
      credentials:'include', cache:'no-store', redirect:'error',
      headers:{'X-CW-Extension':'1'}, signal:AbortSignal.timeout(15000)
    });
  } catch { throw new CWError('SERVER_UNAVAILABLE','CW server unavailable. Check your connection and try again.'); }
  if(response.status===401)throw new CWError('LOGIN_REQUIRED','CW login expired or unavailable. Sign in to CW in this browser profile, then select a complaint.');
  let data;
  try { data=await response.json(); }
  catch { throw new CWError('SERVER_UNAVAILABLE','CW server unavailable: the complaint endpoint returned a webpage instead of JSON. Contact CW support.'); }
  if(response.status===404 && data.code==='NO_SELECTION')throw new CWError('NO_SELECTION','No complaint selected for this login. Open CW and select your complaint again.');
  if(!response.ok)throw new CWError('SERVER_UNAVAILABLE','CW server unavailable or not updated. Contact CW support.');
  try { return parseComplaint(data); }
  catch { throw new CWError('INVALID_DATA','CW returned invalid complaint data. Contact CW support.'); }
}
