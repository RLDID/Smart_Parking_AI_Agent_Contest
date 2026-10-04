// Security regression: a response arriving after access.revoked must not refill
// the old user's incident/manual evidence. Executes the actual console code.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {webcrypto} = require('node:crypto');

const nodes = new Map();
function node(id) {
  if (!nodes.has(id)) nodes.set(id, {textContent:'', value:'', disabled:false, dataset:{}, listeners:{},
    replaceChildren(){this.textContent='';}, append(){},
    addEventListener(type, callback){this.listeners[type]=callback;}});
  return nodes.get(id);
}
let finishFetch;
const context = vm.createContext({console, crypto:webcrypto, Date, JSON, Map, Error,
  setInterval(){}, EventSource:class {},
  document:{getElementById:node, querySelector:node, querySelectorAll(){return [];},
            createElement:node, createElementNS:(_ns,id)=>node(id)},
  fetch:()=>new Promise(resolve=>{finishFetch=resolve;})});
vm.runInContext(fs.readFileSync(path.join(__dirname,'../code/devtools/app.js'),'utf8'),context);

(async()=>{
  vm.runInContext('role="test_operator"; runId="run-synthetic"; csrf="test"; lastState={run_status:"paused"};',context);
  const pending = node('manual-notify').listeners.click();
  assert.equal(typeof finishFetch,'function');
  vm.runInContext('clearDisplayedState()',context);
  finishFetch({status:200,ok:true,json:async()=>({incident_id:'PRIVATE-CANARY',knowledge:{references:[{excerpt:'PRIVATE-CANARY'}]}})});
  await pending;
  assert.equal(node('manual-result').textContent,'실행 없음');
  assert.equal(vm.runInContext('manualResult',context),null);
  assert.equal(vm.runInContext('role',context),null);
  for (const item of nodes.values()) assert.ok(!item.textContent.includes('PRIVATE-CANARY'));
  console.log('PASS: late manual response after session clearing cannot restore private UI data');
  vm.runInContext('role="driver"; runId="run-synthetic"; csrf="test";',context);
  node('agent-goal').value='my_vehicle';
  const queryPending = node('agent-form').listeners.submit({preventDefault(){}});
  vm.runInContext('clearDisplayedState()',context);
  finishFetch({status:200,ok:true,json:async()=>({mode:'mock',tool_results:[{result:'PRIVATE-CANARY'}]})});
  await queryPending;
  assert.equal(node('agent-result').textContent,'실행 없음');
  for (const item of nodes.values()) assert.ok(!item.textContent.includes('PRIVATE-CANARY'));
  console.log('PASS: late mock query cannot restore the cleared user result');
  vm.runInContext('role="driver"; runId="run-synthetic"; csrf="test"; lastState={run_status:"paused"};',context);
  node('agent-provider').value='gemini';
  const livePending = node('agent-form').listeners.submit({preventDefault(){}});
  vm.runInContext('clearDisplayedState()',context);
  finishFetch({status:200,ok:true,json:async()=>({mode:'live',answer:'PRIVATE-CANARY',tool_results:[]})});
  await livePending;
  assert.equal(node('agent-result').textContent,'실행 없음');
  for (const item of nodes.values()) assert.ok(!item.textContent.includes('PRIVATE-CANARY'));
  console.log('PASS: late live query cannot restore the cleared user answer');
  vm.runInContext('role="owner"; runId="run-synthetic"; csrf="test";',context);
  const statusPending = node('refresh-models').listeners.click();
  vm.runInContext('clearDisplayedState()',context);
  finishFetch({status:200,ok:true,json:async()=>({providers:[{model_ref:'PRIVATE-CANARY'}]})});
  await statusPending;
  assert.equal(node('model-status').textContent,'인증 후 제공자 · 비용 확인');
  for (const item of nodes.values()) assert.ok(!item.textContent.includes('PRIVATE-CANARY'));
  console.log('PASS: late model status cannot restore the cleared user metadata');
})().catch(error=>{console.error(error);process.exitCode=1;});
