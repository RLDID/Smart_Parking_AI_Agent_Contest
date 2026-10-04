const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {webcrypto} = require('node:crypto');

const nodes = new Map();
function node(id) {
  if (!nodes.has(id)) nodes.set(id, {textContent:'', classList:{add(){},remove(){}},
    listeners:{}, addEventListener(type, callback){this.listeners[type]=callback;}});
  return nodes.get(id);
}
let replies = [];
let poll;
const sandbox = vm.createContext({crypto:webcrypto, JSON, Error, Number,
  setInterval(callback){poll=callback;},
  document:{getElementById:node,querySelectorAll(){return [];}},
  fetch(){return new Promise(resolve=>replies.push(resolve));}});
vm.runInContext(fs.readFileSync(path.join(__dirname,'../code/devtools/relationships.js'),'utf8'),sandbox);

async function tick(){await new Promise(resolve=>setImmediate(resolve));}
(async()=>{
  await tick();
  assert.equal(replies.length,1);
  replies.shift()({status:200,ok:true,json:async()=>({csrf_token:'csrf',facility_roles:[
    {facility_id:'fac-demo-01',roles:['owner']} ]})});
  await tick();
  assert.equal(replies.length,1);
  replies.shift()({status:200,ok:true,json:async()=>({updated_at:'now',customers:[{display_alias:'SAFE'}]})});
  await tick();
  assert.match(node('snapshot').textContent,/SAFE/);
  poll();
  await tick();
  replies.shift()({status:403,ok:false,json:async()=>({error:{message:'revoked'}})});
  await tick();
  assert.equal(node('snapshot').textContent,'');
  assert.match(node('status').textContent,/세션|권한/);
  console.log('PASS: periodic access check clears relationship data after logout or revocation');
})().catch(error=>{console.error(error);process.exitCode=1;});
