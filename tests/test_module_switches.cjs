const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../pages/陪伴小屋/module-switches.js'), 'utf8');
const flush = async () => { for(let i=0;i<8;i++)await new Promise(setImmediate); };
function fixture(page='state') {
  let scope='one', values={enabled:true,mood_enabled:false,calendar_enabled:true,planning_enabled:false}, getFailure=false, postFailure=false;
  const calls=[], listeners={}, legacy={disabled:false}, events=[];
  const root={isConnected:true, dataset:{moduleSwitches:page},innerHTML:'',hidden:false,
    parentElement:{querySelectorAll:()=>[legacy]},addEventListener:(name,fn)=>listeners[name]=fn};
  const role={state_api:'/astrbot_plugin_private_companion/state',reply_api:'/astrbot_plugin_private_companion/reply',
    config_api:'/astrbot_plugin_private_companion/role/config',memory_api:'/astrbot_plugin_private_companion/memory'};
  const window={dispatchEvent:e=>events.push(e),CompanionProduct:{role:()=>role,scopeKey:()=>scope,
    request:async(method,endpoint,body)=>{
      calls.push({method,endpoint,body});
      assert.ok(!endpoint.startsWith('/'));
      assert.ok(!endpoint.includes('astrbot_plugin_private_companion'));
      if(method==='POST'){
        if(postFailure)throw new Error('revision_conflict');
        values={...values,...body.changes};return {ok:true};
      }
      if(getFailure)throw new Error('unavailable');
      if(endpoint==='state')return {settings:{...values},role_edit_token:'role-token'};
      if(endpoint==='reply')return {settings:{enabled:false,enable_humanized_expression:false,voice_enabled:false},edit_token:'reply-token'};
      if(endpoint==='semantic')return {settings:{reactions_enabled:false,revision:2}};
      return {sections:{[body.sections]:{settings:{enabled:false,style_review_enabled:false,voice_action_enabled:false},edit_token:'config-token'}}};
    }}};
  const sandbox={window,AbortController,MutationObserver:class{observe(){}disconnect(){}},crypto:{randomUUID:()=> 'operation'},CustomEvent:class{constructor(name,details){this.name=name;this.detail=details.detail;}},console};
  vm.runInNewContext(source,sandbox);
  const change=(id,checked)=>listeners.change({target:{closest:()=>({dataset:{moduleToggle:id},checked})}});
  const refresh=()=>listeners.click({target:{closest:()=>true}});
  window.CompanionModuleSwitches.mount(root);
  return {root,legacy,calls,events,page:window.CompanionModuleSwitches,change,refresh,
    failRead:()=>getFailure=true,failWrite:()=>postFailure=true,setScope:x=>scope=x};
}
test('reads real state, deduplicates a shared owner, and never writes on mount',async()=>{
  const h=fixture();await flush();
  assert.equal(h.calls.length,1);
  assert.equal(h.calls[0].endpoint,'state');
  assert.match(h.root.innerHTML,/data-module-id="mood"[\s\S]*?已关闭/);
  assert.match(h.root.innerHTML,/data-module-toggle="state" checked/);
});
test('a toggle writes only its field with the current role edit token, then reads confirmation',async()=>{
  const h=fixture();await flush();h.change('mood',true);await flush();
  const writes=h.calls.filter(x=>x.method==='POST');assert.equal(writes.length,1);
  assert.equal(writes[0].endpoint,'state/command');
  assert.deepEqual(JSON.parse(JSON.stringify(writes[0].body)),{action:'configure_role',operation_key:'module:operation',role_edit_token:'role-token',changes:{mood_enabled:true}});
  assert.equal(h.calls.at(-1).method,'GET');
  assert.match(h.root.innerHTML,/data-module-toggle="mood" checked/);
});
test('locked modules show actual status and reject change events including legacy controls',async()=>{
  const h=fixture('expression');await flush();
  assert.match(h.root.innerHTML,/已关闭 · 已锁定/);assert.equal(h.legacy.disabled,true);
  h.change('split',true);await flush();assert.equal(h.calls.filter(x=>x.method==='POST').length,0);
});
test('missing state remains unavailable and never presents it as a confirmed off switch',async()=>{
  const h=fixture();await flush();h.failRead();h.refresh();await flush();
  assert.match(h.root.innerHTML,/读取失败/);assert.doesNotMatch(h.root.innerHTML,/已关闭/);
  h.change('mood',true);await flush();assert.equal(h.calls.filter(x=>x.method==='POST').length,0);
});
test('stale write is re-read, keeps confirmed state, and reports conflict',async()=>{
  const h=fixture();await flush();h.failWrite();h.change('mood',true);await flush();
  assert.match(h.root.innerHTML,/配置已更新，请刷新后重试/);
  assert.doesNotMatch(h.root.innerHTML,/data-module-toggle="mood" checked/);
});
test('a switched role cannot write through a previous mounted control',async()=>{
  const h=fixture();await flush();h.setScope('two');h.change('mood',true);await flush();
  assert.equal(h.calls.filter(x=>x.method==='POST').length,0);
});
test('memory and native access writes use their exact owner contracts',async()=>{
  const h=fixture();await flush();
  const defs=h.page.entries;
  const memory=h.page.commandFor(defs.find(x=>x.id==='memory'),{token:'memory-token'},false,'m');
  assert.equal(memory.suffix,'/command');
  assert.deepEqual(JSON.parse(JSON.stringify(memory.body)),{operation_key:'m',expected_edit_token:'memory-token',changes:{'memory.enabled':false},reset_to_inherit:[]});
  const access=h.page.commandFor(defs.find(x=>x.id==='screen'),{roleRevision:3},true,'s');
  assert.deepEqual(JSON.parse(JSON.stringify(access.body)),{operation_key:'s',expected_revision:3,enabled:true});
});
