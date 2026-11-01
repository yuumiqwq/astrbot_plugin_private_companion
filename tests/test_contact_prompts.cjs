const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');

const source = fs.readFileSync(path.join(__dirname,'../pages/陪伴小屋/contact-prompts.js'),'utf8');
const purposes = ['affection','care','play','repair','chat','greeting','followup','invitation','reminder','meal','life','creation','news','environment','screen'];
const items = [...purposes,'mobile_location','reaction_image'].map(id => ({id,label:id}));
const endpoint = '/astrbot_plugin_private_companion/targets/t/roles/r/models';
const flush = () => new Promise(resolve => setImmediate(resolve));
const settle = async () => { for(let i=0;i<5;i++) await flush(); };
function section(key,prompt = '实际原文：'+key) {
  return {key,label:key,condition:'条件 '+key,prompt,default_prompt:'默认原文：'+key,editable:true,
    variables:key === 'length' ? ['chars'] : [],edit_token:'a'.repeat(64),configuration_layer:'window',
    field:{value:prompt,inherited_value:'上级原文：'+key,source:'role',explicit:false}};
}
function task() {
  return {kind:'proactive.compose',edit_token:'b'.repeat(64),default_prompt:'默认任务提示',
    fields:{prompt:{value:'任务原文',inherited_value:'上级任务提示',explicit:false,source:'role'}},
    prompt_sections:[...purposes.map(id => section('purpose.'+id)),
      ...['business','expression','history','output','terminal','length','diagnostic_image','diagnostic_source','origin.reality_location_event','voice','simple','simple_output','simple_terminal'].map(id => section(id))],
    prompt_assembly:{current_mode:'companion',mode:{value:'companion',edit_token:'m'.repeat(64),configuration_layer:'window'},
      purpose_sections:Object.fromEntries(purposes.map(id => [id,'purpose.'+id])),
      common_sections:['business','expression','history','output','terminal'],
      conditional_sections:['length','diagnostic_image','origin.reality_location_event','voice'],
      diagnostic_variants:{mobile_location:{purpose:'environment',sections:['origin.reality_location_event','diagnostic_source']},
        reaction_image:{purpose:'chat',sections:['diagnostic_image']}},
      order:[{key:'role_instruction',label:'当前人格与预设',sections:[]},
        {key:'request_instruction',label:'业务提示',sections:['business','purpose.chat']},
        {key:'task_prompt',label:'模型任务提示',sections:[]},
        {key:'materials',label:'会话历史与本轮资料',sections:[]}],
      simple_order:[{key:'role_instruction',label:'当前人格与预设',sections:[]},
        {key:'request_instruction',label:'简洁主动提示',sections:['simple']},
        {key:'materials',label:'会话历史',sections:[]}]}};
}
function harness() {
  const requests = [], chosen = [], modes = [];
  let modelApi = endpoint;
  const root = {innerHTML:'',isConnected:true,handlers:{},querySelector:()=>null,
    addEventListener(type,handler){this.handlers[type]=handler;},removeEventListener(type){delete this.handlers[type];}};
  const window = {CompanionProduct:{role:()=>({model_api:modelApi}),identity:()=>({installation_id:'i',operator_id:'o'}),
    bridgeNow:()=>({apiGet:(route,body,options)=>new Promise((resolve,reject)=>requests.push({method:'GET',route,body,options,resolve,reject})),
      apiPost:(route,body,options)=>new Promise((resolve,reject)=>requests.push({method:'POST',route,body,options,resolve,reject}))})}};
  vm.runInNewContext(source,{window,console,setTimeout,clearTimeout});
  const page=window.CompanionContactPrompts;
  const mount=(selected='chat',mode)=>page.mount(root,{items,selected,mode,onSelect:id=>chosen.push(id),onModeChange:value=>modes.push(value)});
  function event(type,selector,node) {return root.handlers[type]?.({target:{closest:query=>query===selector?node:null}});}
  const action=(name,key)=>event('click','[data-prompt-action]',{dataset:{promptAction:name,promptKey:key}});
  const edit=(key,value)=>event('input','[data-prompt-text]',{dataset:{promptText:key},value});
  const reply=(request,data)=>request.resolve({status:'ok',data});
  async function boot(value=task()) {mount();await settle();reply(requests.at(-1),{tasks:[value]});await settle();}
  return {page,root,requests,chosen,modes,mount,event,action,edit,reply,boot,setPath:value=>{modelApi=value;}};
}

test('只读取当前窗口的真实模型提示，17入口均能定位目的原文',async()=>{
  const h=harness();await h.boot();
  assert.equal(h.requests.length,1);assert.equal(h.requests[0].method,'GET');
  assert.equal(h.requests[0].route,'targets/t/roles/r/models');assert.equal(h.requests[0].body.layer,'window');
  assert.equal((h.root.innerHTML.split('data-prompt-entry')[1].split('</select>')[0].match(/<option value=/g)||[]).length,17);
  for(const item of items){h.page.select(item.id);const p=item.id==='mobile_location'?'environment':item.id==='reaction_image'?'chat':item.id;
    assert.match(h.root.innerHTML,new RegExp('data-prompt-text="purpose\\.'+p+'"'));
    assert.ok(h.root.innerHTML.includes('实际原文：purpose.'+p));}
  assert.equal(h.requests.length,1);
});

test('目的、共用、条件及任务提示都可以编辑，组装顺序来自接口',async()=>{
  const h=harness();await h.boot();
  for(const key of ['purpose.chat','business','length','task_prompt','output','terminal'])
    assert.ok(h.root.innerHTML.includes('data-prompt-text="'+key+'"'));
  assert.match(h.root.innerHTML,/格式变量 <code>\{chars\}<\/code>/);
  h.edit('length','保留字数 {chars}');
  h.event('change','[data-prompt-section]',{value:'voice'});
  assert.match(h.root.innerHTML,/data-prompt-text="voice"/);
  assert.doesNotMatch(h.root.innerHTML,/data-prompt-text="length"/);
  h.event('change','[data-prompt-section]',{value:'length'});
  assert.match(h.root.innerHTML,/保留字数 \{chars\}/);
  assert.ok(h.root.innerHTML.indexOf('当前人格与预设')<h.root.innerHTML.indexOf('<strong>业务提示'));
  assert.ok(h.root.innerHTML.includes('会话历史与本轮资料'));
  h.page.select('mobile_location');
  const primary=h.root.innerHTML.split('ct-prompt-primary')[1].split('data-prompt-group="common"')[0];
  assert.match(primary,/origin\.reality_location_event/);
  assert.match(primary,/diagnostic_source/);
  h.page.select('reaction_image');
  assert.match(h.root.innerHTML.split('ct-prompt-primary')[1].split('data-prompt-group="common"')[0],/diagnostic_image/);
});

test('切换入口、重新挂载和切换配置层保留各自草稿',async()=>{
  const h=harness();await h.boot();h.edit('purpose.chat','窗口草稿');
  h.page.select('care');h.page.select('chat');assert.match(h.root.innerHTML,/窗口草稿/);
  h.page.unmount();h.mount();assert.match(h.root.innerHTML,/窗口草稿/);
  h.event('change','[data-prompt-layer]',{value:'role'});await settle();
  h.reply(h.requests.at(-1),{tasks:[task()]});await settle();assert.doesNotMatch(h.root.innerHTML,/窗口草稿/);
  h.edit('purpose.chat','角色草稿');
  h.event('change','[data-prompt-layer]',{value:'window'});await settle();h.reply(h.requests.at(-1),{tasks:[task()]});await settle();
  assert.match(h.root.innerHTML,/窗口草稿/);assert.doesNotMatch(h.root.innerHTML,/角色草稿/);
});

test('保存一个片段只提交该片段，并采用返回的实际内容',async()=>{
  const h=harness();await h.boot();h.edit('purpose.chat','新原文');h.action('save','purpose.chat');await settle();
  const request=h.requests.at(-1);
  assert.equal(request.route,'targets/t/roles/r/models/configure');assert.equal(request.body.kind,'proactive.compose');
  assert.equal(request.body.section,'purpose.chat');assert.equal(request.body.layer,'window');
  assert.deepEqual(JSON.parse(JSON.stringify(request.body.changes)),{prompt:'新原文'});
  assert.deepEqual(JSON.parse(JSON.stringify(request.body.reset)),[]);
  const saved=task();saved.prompt_sections[4]=section('purpose.chat','新原文');saved.prompt_sections[4].field.explicit=true;
  h.reply(request,{task:saved});await settle();
  assert.equal(h.page.state.drafts.get('purpose.chat').dirty,false);assert.match(h.root.innerHTML,/已保存/);
});

test('默认与继承操作先留在草稿，保存时分别发文本或reset',async()=>{
  const h=harness();await h.boot();h.action('default','purpose.chat');
  assert.equal(h.requests.length,1);assert.equal(h.page.state.drafts.get('purpose.chat').text,'默认原文：purpose.chat');
  h.action('save','purpose.chat');await settle();let request=h.requests.at(-1);
  assert.equal(request.body.changes.prompt,'默认原文：purpose.chat');h.reply(request,{task:task()});await settle();
  h.action('inherit','purpose.chat');h.action('save','purpose.chat');await settle();request=h.requests.at(-1);
  assert.deepEqual(JSON.parse(JSON.stringify(request.body.changes)),{});assert.deepEqual(JSON.parse(JSON.stringify(request.body.reset)),['prompt']);
  h.reply(request,{task:task()});await settle();
});

test('模型任务提示使用任务令牌且不传section',async()=>{
  const h=harness();await h.boot();h.edit('task_prompt','新任务');h.action('save','task_prompt');await settle();
  const request=h.requests.at(-1);assert.equal(request.body.edit_token,'b'.repeat(64));assert.equal(request.body.section,undefined);
  h.reply(request,{task:task()});await settle();
});

test('桥接明确拒绝保留输入，冲突读取新值后等待用户采用新版本',async()=>{
  const h=harness();await h.boot();h.edit('purpose.chat','我的草稿');h.action('save','purpose.chat');await settle();
  h.requests.at(-1).reject(new Error('configuration_revision_conflict'));await settle();
  const changed=task();changed.prompt_sections[4]=section('purpose.chat','其他页面的新值');changed.prompt_sections[4].edit_token='c'.repeat(64);
  h.reply(h.requests.at(-1),{tasks:[changed]});await settle();
  assert.equal(h.page.state.drafts.get('purpose.chat').text,'我的草稿');assert.equal(h.page.state.drafts.get('purpose.chat').conflict,true);
  assert.match(h.root.innerHTML,/其他页面的新值/);
  const count=h.requests.length;h.action('save','purpose.chat');await settle();assert.equal(h.requests.length,count);
  h.action('rebase','purpose.chat');h.action('save','purpose.chat');await settle();
  assert.equal(h.requests.at(-1).body.edit_token,'c'.repeat(64));h.reply(h.requests.at(-1),{task:changed});await settle();
});

test('网络中断保留原操作，核对保存重用同一请求而不发送QQ',async()=>{
  const h=harness();await h.boot();h.edit('purpose.chat','网络草稿');h.action('save','purpose.chat');await settle();
  const first=h.requests.at(-1);first.reject(new Error('Failed to fetch'));await settle();
  assert.match(h.root.innerHTML,/保存结果尚未确认/);h.page.select('care');h.page.select('chat');
  h.action('save','purpose.chat');await settle();assert.equal(h.requests.at(-1).body,first.body);
  h.reply(h.requests.at(-1),{task:task()});await settle();
  assert.ok(h.requests.every(r=>/\/models(?:\/configure)?$/.test(r.route)));
});

test('读取过程中重新挂载仍展示完成结果，其他角色草稿隔离',async()=>{
  const h=harness();h.mount();await settle();h.mount();h.reply(h.requests[0],{tasks:[task()]});await settle();
  assert.match(h.root.innerHTML,/实际原文：purpose.chat/);h.edit('purpose.chat','角色一草稿');
  h.setPath(endpoint.replace('/r/','/other/'));h.mount();await settle();h.reply(h.requests.at(-1),{tasks:[task()]});await settle();
  assert.doesNotMatch(h.root.innerHTML,/角色一草稿/);
});

test('提示词文本转义后展示，不能结束textarea插入页面节点',async()=>{
  const value=task();value.prompt_sections[4]=section('purpose.chat','</textarea><img src=x onerror=alert(1)>');
  const h=harness();await h.boot(value);assert.doesNotMatch(h.root.innerHTML,/<img src=x/);
  assert.match(h.root.innerHTML,/&lt;\/textarea&gt;&lt;img/);
});

test('共享表达设置的实际继承文本和来源保持可见',async()=>{
  const value=task();const shared=section('length','当前表达页的实际文本');
  shared.field.source='presentation';shared.field.inherited_source='presentation';shared.field.inherited_value='当前表达页的实际文本';
  value.prompt_sections=value.prompt_sections.filter(s=>s.key!=='length').concat(shared);
  const h=harness();await h.boot(value);assert.match(h.root.innerHTML,/当前表达页的实际文本/);
  assert.match(h.root.innerHTML,/data-prompt-state>表达设置<\/span>/);
  h.action('inherit','length');assert.equal(h.page.state.drafts.get('length').text,'当前表达页的实际文本');
});

test('模式切换保存真实mode字段，简洁与陪伴插件提示草稿分别保留',async()=>{
  const h=harness();await h.boot();h.edit('purpose.chat','陪伴模式草稿');
  h.event('click','[data-prompt-mode]',{dataset:{promptMode:'simple'}});await settle();
  const request=h.requests.at(-1);
  assert.equal(request.body.section,'mode');assert.equal(request.body.edit_token,'m'.repeat(64));
  assert.deepEqual(JSON.parse(JSON.stringify(request.body.changes)),{mode:'simple'});
  assert.equal(h.page.state.task.prompt_assembly.current_mode,'companion');
  const simple=task();simple.prompt_assembly.current_mode='simple';simple.prompt_assembly.mode.value='simple';
  simple.prompt_sections.find(s=>s.key==='simple').variables=['current_time','unanswered_count'];
  h.reply(request,{task:simple});await settle();
  assert.equal((h.root.innerHTML.match(/data-prompt-text=/g)||[]).length,1);
  assert.match(h.root.innerHTML,/data-prompt-text="simple"/);
  assert.doesNotMatch(h.root.innerHTML,/data-prompt-entry|data-prompt-text="simple_output"|data-prompt-text="simple_terminal"|data-prompt-text="purpose.chat"|data-prompt-group="common"|data-prompt-group="conditional"/);
  assert.match(h.root.innerHTML,/<code>\{\{current_time\}\}<\/code>/);
  assert.doesNotMatch(h.root.innerHTML,/简洁输出格式|简洁末尾约束/);
  assert.deepEqual(h.modes,['companion','simple']);
  h.edit('simple','简洁模式草稿');
  h.event('click','[data-prompt-mode]',{dataset:{promptMode:'companion'}});await settle();h.reply(h.requests.at(-1),{task:task()});await settle();
  assert.match(h.root.innerHTML,/陪伴模式草稿/);assert.doesNotMatch(h.root.innerHTML,/data-prompt-text="simple"/);
  assert.equal(h.page.state.drafts.get('simple').text,'简洁模式草稿');
  assert.equal(h.requests.filter(r=>r.method==='POST').length,2);
});

test('模式切换明确失败不改当前模式，网络未知时仅核对原操作',async()=>{
  const h=harness();await h.boot();h.edit('purpose.chat','已有草稿');
  h.event('click','[data-prompt-mode]',{dataset:{promptMode:'simple'}});await settle();
  h.requests.at(-1).reject(new Error('model_configuration_invalid'));await settle();
  assert.equal(h.page.state.task.prompt_assembly.current_mode,'companion');assert.match(h.root.innerHTML,/已有草稿/);
  h.event('click','[data-prompt-mode]',{dataset:{promptMode:'simple'}});await settle();
  const pending=h.requests.at(-1);pending.reject(new Error('NetworkError'));await settle();
  assert.match(h.root.innerHTML,/模式切换结果尚未确认/);
  h.action('retry-mode');await settle();assert.equal(h.requests.at(-1).body,pending.body);
  const simple=task();simple.prompt_assembly.current_mode='simple';simple.prompt_assembly.mode.value='simple';
  h.reply(h.requests.at(-1),{task:simple});await settle();assert.equal(h.page.state.modePending,null);
  assert.equal(h.page.state.task.prompt_assembly.current_mode,'simple');
});

test('简洁页只编辑旧提示词，保存时不修改陪伴片段或隐藏格式模板',async()=>{
  const h=harness(), simple=task();simple.prompt_assembly.current_mode='simple';
  await h.boot(simple);h.edit('simple','旧插件主动提示词的新草稿');h.action('save','simple');await settle();
  const request=h.requests.at(-1);
  assert.equal(request.body.section,'simple');
  assert.deepEqual(JSON.parse(JSON.stringify(request.body.changes)),{prompt:'旧插件主动提示词的新草稿'});
  const saved=task();saved.prompt_assembly.current_mode='simple';
  saved.prompt_sections.find(item=>item.key==='simple').prompt='旧插件主动提示词的新草稿';
  h.reply(request,{task:saved});await settle();
  assert.equal((h.root.innerHTML.match(/data-prompt-text=/g)||[]).length,1);
  assert.equal(h.page.state.drafts.get('simple').dirty,false);
  assert.equal(h.requests.filter(r=>r.method==='POST').length,1);
});

test('父页面刷新模式时沿用缓存草稿，旧缓存模式不会覆盖最新选择',async()=>{
  const h=harness();await h.boot();h.edit('purpose.chat','陪伴草稿保留');
  h.mount('chat','simple');
  assert.match(h.root.innerHTML,/data-prompt-text="simple"/);
  assert.doesNotMatch(h.root.innerHTML,/data-prompt-entry/);
  assert.deepEqual(h.modes,['companion']);
  h.event('click','[data-prompt-mode]',{dataset:{promptMode:'companion'}});await settle();
  assert.equal(h.requests.at(-1).body.changes.mode,'companion');
  h.reply(h.requests.at(-1),{task:task()});await settle();
  assert.match(h.root.innerHTML,/陪伴草稿保留/);
});

test('模拟前确认当前作用域的已保存模式，并保留未保存提示词草稿',async()=>{
  const h=harness(),simple=task();simple.prompt_assembly.current_mode='simple';
  await h.boot(simple);h.edit('simple','保留未保存的简洁草稿');
  const confirmed=h.page.confirmMode();await settle();
  const request=h.requests.at(-1);assert.equal(request.method,'GET');assert.equal(request.body.layer,'window');
  assert.equal(request.options.timeoutMs,30000);
  h.reply(request,{tasks:[task()]});assert.equal(await confirmed,'companion');
  assert.equal(h.page.state.currentMode,'companion');assert.equal(h.modes.at(-1),'companion');
  assert.equal(h.page.state.drafts.get('simple').text,'保留未保存的简洁草稿');
  assert.equal(h.requests.filter(item=>item.method==='POST').length,0);
});

test('模拟确认等待正在保存的模式完成，再读取对应模式',async()=>{
  const h=harness();await h.boot();
  h.event('click','[data-prompt-mode]',{dataset:{promptMode:'simple'}});await settle();
  const save=h.requests.at(-1),count=h.requests.length;
  const confirmed=h.page.confirmMode();await settle();assert.equal(h.requests.length,count);
  const simple=task();simple.prompt_assembly.current_mode='simple';
  h.reply(save,{task:simple});await settle();
  assert.equal(h.requests.at(-1).method,'GET');h.reply(h.requests.at(-1),{tasks:[simple]});
  assert.equal(await confirmed,'simple');assert.equal(h.requests.filter(item=>item.method==='POST').length,1);
});

test('模式保存结果未知时阻止模拟确认，保留原操作供核对',async()=>{
  const h=harness();await h.boot();
  h.event('click','[data-prompt-mode]',{dataset:{promptMode:'simple'}});await settle();
  h.requests.at(-1).reject(new Error('NetworkError'));await settle();
  const count=h.requests.length;
  await assert.rejects(h.page.confirmMode(),error=>error.code==='contact_prompt_mode_unconfirmed');
  assert.equal(h.requests.length,count);assert.ok(h.page.state.modePending);
});
