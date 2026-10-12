import {payload} from './source.js';
const status=document.getElementById('status');
async function check() {
  status.textContent='Checking your CW login and selected complaint…';
  try {
    const data=await payload();
    status.textContent=`Ready to fill from complaint ${data.complaint_id}. Open the form and click CW.`;
    status.dataset.state='READY';
  } catch(e) { status.textContent=e.message; status.dataset.state=e.code || 'ERROR'; }
}
document.getElementById('test').onclick=check;
check();
