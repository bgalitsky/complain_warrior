import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createRequire} from 'node:module';
const require = createRequire(import.meta.url);
// npm install --prefix /tmp/cw-dom jsdom; or set CW_JSDOM to a local jsdom package.
const {JSDOM} = require(process.env.CW_JSDOM || 'jsdom');
const source=readFileSync(new URL('../chrome/page-agent.js',import.meta.url),'utf8');
function fixture(html) {
 const dom=new JSDOM(html,{url:'https://agency.example/complaint',runScripts:'outside-only',pretendToBeVisual:true});
 const w=dom.window;
 // jsdom has no layout: visibility geometry is simulated in these DOM unit tests.
 w.HTMLElement.prototype.getClientRects=function(){return this.hidden ? [] : [{width:100,height:20}];};
 w.eval(source); return {dom,w,agent:w.__cwFormAgent};
}
const form='<form><fieldset><legend>Your contact details</legend><label>Name<input id="name"></label><label>Email<input id="email" type="email"></label></fieldset><label>Password<input type="password"></label><label>CAPTCHA<input></label><label>Consent<input type="checkbox"></label><label>Upload<input type="file"></label><input type="hidden"><button>Submit</button></form>';
test('only supported visible controls; native events; no submit',async()=>{
 const {dom,w,agent}=fixture(form);let submits=0,events=0;
 w.document.querySelector('form').addEventListener('submit',()=>submits++);
 w.document.querySelector('#name').addEventListener('input',()=>events++);
 const s=agent.scan();assert.deepEqual(Array.from(s.fields,f=>f.label),['Name','Email']);
 const result=await agent.fill({id:s.id,fields:[{token:s.fields[0].token,value:'Alice',overwrite:false}]});
 assert.equal(result[0].status,'filled');assert.equal(events,1);assert.equal(submits,0);
 assert.equal(w.document.querySelector('#name').value,'Alice');dom.window.close();
});
test('stale labels and changed value refuse whole batch',async()=>{
 const {dom,w,agent}=fixture(form);const s=agent.scan();
 w.document.querySelector('#email').value='manual@example.com';
 await assert.rejects(agent.fill({id:s.id,fields:s.fields.map(f=>({token:f.token,value:'x',overwrite:true}))}),/changed/);
 assert.equal(w.document.querySelector('#name').value,'');dom.window.close();
});
test('existing values require explicit overwrite approval',async()=>{
 const {dom,w,agent}=fixture(form);w.document.querySelector('#name').value='Bob';const s=agent.scan();
 await assert.rejects(agent.fill({id:s.id,fields:[{token:s.fields[0].token,value:'Alice',overwrite:false}]}),/replacing/);
 assert.equal(w.document.querySelector('#name').value,'Bob');dom.window.close();
});
test('page navigation invalidates approved snapshot',async()=>{
 const {dom,w,agent}=fixture(form);const s=agent.scan();w.history.pushState({},'', '/different-case');
 await assert.rejects(agent.fill({id:s.id,fields:[]}),/Page changed/);dom.window.close();
});
test('dynamic re-render stops partial fill rather than guessing new elements',async()=>{
 const {dom,w,agent}=fixture(form);const s=agent.scan();
 w.document.querySelector('#name').addEventListener('input',()=>w.document.querySelector('#email').replaceWith(w.document.createElement('input')));
 const results=await agent.fill({id:s.id,fields:s.fields.map(f=>({token:f.token,value:'x',overwrite:false}))});
 assert.equal(results[0].status,'filled');assert.equal(results[1].status,'stopped');
 assert.equal(w.document.querySelectorAll('fieldset input')[1].value,'');dom.window.close();
});
test('disabled field and disabled option cannot be filled',async()=>{
 const {dom,w,agent}=fixture('<label>State<select><option value="">Choose</option><option disabled value="CA">California</option></select></label><fieldset disabled><label>Email<input></label></fieldset>');
 const s=agent.scan();assert.equal(s.fields.length,1);
 await assert.rejects(agent.fill({id:s.id,fields:[{token:s.fields[0].token,value:'CA',overwrite:false}]}),/no longer available/);dom.window.close();
});
test('open shadow root controls discovered and dropdown labels exclude options',()=>{
 const {dom,w,agent}=fixture('<div id="host"></div><label>State<select><option value="CA">California</option></select></label>');
 w.document.querySelector('#host').attachShadow({mode:'open'}).innerHTML='<label>Email<input type="email"></label>';
 const s=agent.scan();assert.deepEqual(Array.from(s.fields,f=>f.label),['Email','State']);dom.window.close();
});
