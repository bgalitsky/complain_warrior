import test from 'node:test';
import assert from 'node:assert/strict';
import {parseComplaint, propose, SCHEMA} from '../chrome/engine.js';
const values = {'consumer.email':'a@example.com','business.email':'company@example.com',
 'consumer.state':'CA','jurisdiction.state':'Florida','business.name':'Acme',
 'consumer.full_name':'Alice','complaint.narrative':'A missing refund.'};
const field=(label,section='', extra={})=>({label,section,tag:'input',autocomplete:'',...extra});
test('party separation from fieldset and explicit labels',()=>{
 assert.equal(propose(field('Email','Business details'),values).key,'business.email');
 assert.equal(propose(field('Your email','Business details'),values).key,'consumer.email');
 assert.equal(propose(field('Email','Your contact details'),values).key,'consumer.email');
 assert.equal(propose(field('Nombre de la empresa'),values).key,'business.name');
});
test('ambiguous party is never a strong suggestion',()=>{
 assert.ok(propose(field('Email'),values).confidence<.9);
 assert.equal(propose(field('Business consumer email'),values),null);
});
test('jurisdiction is not residence',()=>{
 assert.equal(propose(field('State','Filing jurisdiction'),values).key,'jurisdiction.state');
 assert.equal(propose(field('State','Your address'),values).key,'consumer.state');
});
test('selects require exact unique enabled option',()=>{
 const f=field('State','Your address',{tag:'select',options:[{label:'California',value:'CA',disabled:false}]});
 assert.equal(propose(f,values).value,'CA');
 assert.equal(propose({...f,options:[{label:'California',value:'CA',disabled:true}]},values),null);
 assert.equal(propose({...f,options:[...f.options,...f.options]},values),null);
});
test('unknown fields and missing facts left manual',()=>{
 assert.equal(propose(field('Category'),values),null);
 assert.equal(propose(field('First name'),values),null);
 assert.equal(propose(field('Signature name'),values),null);
 assert.equal(propose(field('Order number'),values),null);
});
test('import strict schema no auth material',()=>{
 assert.deepEqual(parseComplaint({schema:SCHEMA,complaint_id:'1',values}).values,values);
 for(const input of [{schema:'other'}, {schema:SCHEMA,complaint_id:'1',values:{password:'x'}},
  {schema:SCHEMA,complaint_id:'1',values:{'consumer.email':{value:'x'}}}])assert.throws(()=>parseComplaint(input));
});
