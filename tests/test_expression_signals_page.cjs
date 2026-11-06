"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "..", "pages", "陪伴小屋", "expression-signals-page.js"), "utf8");
const HOST = "astrbot_plugin_private_companion";
const flush = () => new Promise(resolve => setTimeout(resolve, 0));

test("规则库导出与 V1 分享包预览/导入使用已选择角色路由", async () => {
  const gets = [], posts = [], listeners = new Map();
  const bridge = {
    apiGet: async (route, query) => {
      gets.push({route, query});
      const items = query?.state === "adopted" ? [{signal_id:"signal-1",run_id:"run-1",dimension:"diction",
        source_kind:"conversation_input",observation:"晚间问候时使用轻松短句",proposal:"用轻松短句问候",
        state:"adopted",created_at:"2026-10-08T10:00:00+00:00",actions:["retire"],injections:3,can_revise:true,
        expression:{kind:"style",situation:"彼此有默契",pattern:"懂的都懂",avoid:"认真求助时不用"},
        positive_feedback:1,negative_feedback:2,evidence:[],view_token:"token"}] : [];
      return {status:"ok", data:{items, capacity:{active:1,limit:60}}};
    },
    apiPost: async (route, body) => {
      posts.push({route, body});
      if (route.endsWith("/style/export")) return {status:"ok", data:{package:{schema:"private-companion-expression-pack",version:1,rule_groups:[]},rule_count:0,excluded_count:0}};
      if (route.endsWith("/style/import/preview")) return {status:"ok", data:{title:"测试分享",group_count:1,rule_count:1,
        importable_rule_count:1,duplicate_rule_count:0,capacity_blocked_count:0,rejected_count:0,
        preview_token:"a".repeat(64),candidates:[{dimension:"diction",observation:"短称呼",proposal:"使用简短称呼"}]}};
      return {status:"ok", data:{imported_count:1,state:"pending_review",replayed:false}};
    },
  };
  const root = {isConnected:true,innerHTML:"",addEventListener:(name, callback)=>listeners.set(name, callback),
    removeEventListener:name=>listeners.delete(name),querySelector:()=>null};
  const window = {CompanionProduct:{bridgeNow:()=>bridge,unwrap:value=>value},lucide:{createIcons(){}}};
  const context = vm.createContext({window,setTimeout,clearTimeout,TextEncoder,JSON,Date,Math,Number,Object,Array,String,URLSearchParams});
  vm.runInContext(source, context);
  await context.window.CompanionExpressionSignalsPage.mount(root, {
    replyApi:`/${HOST}/targets/profile-a/roles/role-a/reply`,
    target:{scope_kind:"group",group_id:"group-a"},scopeKey:"scope-a",
  });
  for(let index=0;index<20&&!gets.length;index++)await flush();
  for(let index=0;index<30&&context.window.CompanionExpressionSignalsPage.state.busy;index++)await flush();
  assert.ok(gets.length);
  assert.ok(gets.every(call=>call.route===`targets/profile-a/roles/role-a/reply/style`));
  assert.ok(root.innerHTML.includes("明确反馈 · 认可 1 / 不喜欢 2"));
  assert.match(root.innerHTML,/懂的都懂/);
  assert.match(root.innerHTML,/彼此有默契/);
  for(const dimension of ["表情符号","标点","直接程度","排版","其他"])assert.ok(root.innerHTML.includes(dimension));
  assert.match(root.innerHTML,/已拒绝/);

  const click = async selector => listeners.get("click")?.({target:{closest:value=>value===selector?{}:null}});
  const change = target => listeners.get("change")?.({target});
  const input = target => listeners.get("input")?.({target});
  listeners.get("click")({target:{closest:selector=>selector==="[data-signal-edit]"?{dataset:{signalEdit:"scope-a::signal-1"}}:null}});
  assert.match(root.innerHTML,/编辑表达规则/);
  const field={value:"原来如此呀",dataset:{ruleField:"pattern"},closest:selector=>selector==="[data-rule-field]"?field:null};
  input(field);
  await click("[data-rule-save]");
  for(let index=0;index<30&&!root.innerHTML.includes("表达规则已更新");index++)await flush();
  const edit=posts.find(call=>call.route.endsWith("/style/revise"));
  assert.equal(edit.route,`targets/profile-a/roles/role-a/reply/style/revise`);
  assert.equal(edit.body.view_token,"token");
  assert.equal(edit.body.changes.expression.pattern,"原来如此呀");
  assert.equal(edit.body.changes.expression.polarity,"positive");
  assert.equal(edit.body.changes.expression.avoid,"认真求助时不用");
  await click("[data-share-toggle]");
  await click("[data-share-export]");
  for(let index=0;index<30&&!root.innerHTML.includes("分享包已生成");index++)await flush();
  assert.ok(posts.some(call=>call.route===`targets/profile-a/roles/role-a/reply/style/export`));
  const json = JSON.stringify({schema:"private-companion-expression-pack",version:1,rule_groups:[{rules:[{kind:"style",label:"短称呼",pattern:"使用简短称呼"}]}]});
  input({value:json,closest:selector=>selector==="[data-share-import-json]"?{value:json}:null});
  await click("[data-share-preview]");
  for(let index=0;index<30&&!root.innerHTML.includes("预览已更新");index++)await flush();
  const preview = posts.find(call=>call.route.endsWith("/style/import/preview"));
  assert.equal(preview.body.package_json,json);
  await click("[data-share-apply]");
  for(let index=0;index<30&&!root.innerHTML.includes("已加入审阅中心");index++)await flush();
  assert.equal(posts.find(call=>call.route.endsWith("/style/import/apply")).body.preview_token,"a".repeat(64));
  assert.equal(root.innerHTML.includes("已加入审阅中心"),true);
  context.window.CompanionExpressionSignalsPage.unmount();
  assert.equal(listeners.size,0);
});

test("多来源部分读取失败时保留失败来源与错误码", async () => {
  const bridge = {
    apiGet: async route => {
      if (route === "targets/profile-b/roles/role-b/reply/style") {
        /* 真实宿主桥的形态：status=error 只抛 message，code 已经丢失。 */
        throw new Error("expression_group_context_required");
      }
      if (route === "targets/profile-c/roles/role-c/reply/style") {
        throw Object.assign(new Error("future_source_failure"), {code: "future_source_failure"});
      }
      return {status: "ok", data: {items: [{signal_id: "signal-a", run_id: "run-a", dimension: "diction",
        source_kind: "conversation_input", observation: "来源甲可读", proposal: "继续使用", state: "adopted",
        created_at: "2026-10-08T10:00:00+00:00", actions: ["retire"], injections: 1, can_revise: true,
        expression: {kind: "style", situation: "日常", pattern: "来源甲规则", avoid: ""},
        evidence: [], view_token: "token"}], capacity: {active: 1, limit: 50}, counts: {adopted: 1}}};
    },
    apiPost: async () => ({status: "ok", data: {}}),
  };
  const root = {isConnected: true, innerHTML: "", addEventListener() {}, removeEventListener() {},
    querySelector: () => null};
  const window = {CompanionProduct: {bridgeNow: () => bridge, unwrap: value => value},
    lucide: {createIcons() {}}};
  const context = vm.createContext({window, setTimeout, clearTimeout, TextEncoder, JSON, Date, Math,
    Number, Object, Array, String, URLSearchParams});
  vm.runInContext(source, context);
  const page = context.window.CompanionExpressionSignalsPage;
  await page.mount(root, {sources: [
    {key: "source-a", scopeKey: "scope-a", label: "来源甲",
      replyApi: `/${HOST}/targets/profile-a/roles/role-a/reply`},
    {key: "source-b", scopeKey: "scope-b", label: "来源乙",
      replyApi: `/${HOST}/targets/profile-b/roles/role-b/reply`},
    {key: "source-c", scopeKey: "scope-c", label: "来源丙",
      replyApi: `/${HOST}/targets/profile-c/roles/role-c/reply`},
  ]});
  for (let index = 0; index < 40 && page.state.busy; index++) await flush();
  assert.equal(page.state.notice, "3 个来源中 2 个读取失败，已显示可用记录。");
  assert.match(root.innerHTML, /来源甲规则/);
  assert.match(root.innerHTML, /3 个来源中 2 个读取失败/);
  assert.match(root.innerHTML, /来源乙/);
  assert.match(root.innerHTML, /该群还没有可用于表达学习的对话/);
  assert.match(root.innerHTML, /来源丙/);
  assert.match(root.innerHTML, /原始依据已不可读取/);
  assert.match(root.innerHTML, /role="status"/);
  assert.match(root.innerHTML, /data-code="expression_group_context_required"/);
  assert.match(root.innerHTML, /data-code="future_source_failure"/);
  assert.doesNotMatch(root.innerHTML, /部分规则暂时无法读取，已显示可用记录。/);
  page.unmount();
});

test("空页的 unavailable_reason 作为中性说明而不是读取失败", async () => {
  const bridge = {
    apiGet: async route => route === "targets/profile-b/roles/role-b/reply/style"
      ? {status: "ok", data: {items: [], counts: {}, next_cursor: null,
        unavailable_reason: "role_binding_missing"}}
      : {status: "ok", data: {items: [{signal_id: "signal-a", run_id: "run-a", dimension: "diction",
        source_kind: "conversation_input", observation: "私聊规则可读", proposal: "继续使用", state: "adopted",
        created_at: "2026-10-08T10:00:00+00:00", actions: ["retire"], injections: 0, can_revise: true,
        expression: {kind: "style", situation: "日常", pattern: "私聊规则", avoid: ""},
        evidence: [], view_token: "token"}], capacity: {active: 1, limit: 50}, counts: {adopted: 1}}},
    apiPost: async () => ({status: "ok", data: {}}),
  };
  const root = {isConnected: true, innerHTML: "", addEventListener() {}, removeEventListener() {},
    querySelector: () => null};
  const window = {CompanionProduct: {bridgeNow: () => bridge, unwrap: value => value},
    lucide: {createIcons() {}}};
  const context = vm.createContext({window, setTimeout, clearTimeout, TextEncoder, JSON, Date, Math,
    Number, Object, Array, String, URLSearchParams});
  vm.runInContext(source, context);
  const page = context.window.CompanionExpressionSignalsPage;
  await page.mount(root, {sources: [
    {key: "source-a", scopeKey: "scope-a", label: "私聊来源",
      replyApi: `/${HOST}/targets/profile-a/roles/role-a/reply`},
    {key: "source-b", scopeKey: "scope-b", label: "群聊来源",
      replyApi: `/${HOST}/targets/profile-b/roles/role-b/reply`},
  ]});
  for (let index = 0; index < 40 && page.state.busy; index++) await flush();
  assert.equal(page.state.notice, "1 个来源还没有可读取的表达学习记录，已显示其他来源。");
  assert.match(root.innerHTML, /私聊规则/);
  assert.match(root.innerHTML, /群聊来源/);
  assert.match(root.innerHTML, /这个来源还没有绑定角色/);
  assert.match(root.innerHTML, /data-code="role_binding_missing"/);
  assert.doesNotMatch(root.innerHTML, /lh-inline-status is-error/);
  page.unmount();
});

test("旧作用域规则只作为可展开诊断，不冒充读取失败", async () => {
  const bridge = {
    apiGet: async route => route === "targets/profile-b/roles/role-b/reply/style"
      ? {status: "ok", data: {items: [], counts: {}, next_cursor: null, legacy_orphan_count: 4}}
      : {status: "ok", data: {items: [{signal_id: "signal-a", run_id: "run-a", dimension: "diction",
        source_kind: "conversation_input", observation: "私聊规则可读", proposal: "继续使用", state: "adopted",
        created_at: "2026-10-08T10:00:00+00:00", actions: ["retire"], injections: 0, can_revise: true,
        expression: {kind: "style", situation: "日常", pattern: "私聊规则", avoid: ""},
        evidence: [], view_token: "token"}], capacity: {active: 1, limit: 50}, counts: {adopted: 1},
        legacy_orphan_count: 0}},
    apiPost: async () => ({status: "ok", data: {}}),
  };
  const root = {isConnected: true, innerHTML: "", addEventListener() {}, removeEventListener() {},
    querySelector: () => null};
  const window = {CompanionProduct: {bridgeNow: () => bridge, unwrap: value => value},
    lucide: {createIcons() {}}};
  const context = vm.createContext({window, setTimeout, clearTimeout, TextEncoder, JSON, Date, Math,
    Number, Object, Array, String, URLSearchParams});
  vm.runInContext(source, context);
  const page = context.window.CompanionExpressionSignalsPage;
  await page.mount(root, {sources: [
    {key: "source-a", scopeKey: "scope-a", label: "私聊来源",
      replyApi: `/${HOST}/targets/profile-a/roles/role-a/reply`},
    {key: "source-b", scopeKey: "scope-b", label: "群聊来源",
      replyApi: `/${HOST}/targets/profile-b/roles/role-b/reply`},
  ]});
  for (let index = 0; index < 40 && page.state.busy; index++) await flush();
  assert.equal(page.state.notice, "4 条旧版群规则保存在旧作用域，暂未生效。");
  assert.match(root.innerHTML, /群聊来源/);
  assert.match(root.innerHTML, /4 条旧版群规则保存在旧作用域，暂未生效；需要单独迁移后才能读取和生效。/);
  assert.match(root.innerHTML, /data-code="legacy_expression_scope"/);
  assert.doesNotMatch(root.innerHTML, /lh-inline-status is-error/);
  page.unmount();
});

test("manual comparison and pending rules share one workspace without native sample calls", async () => {
  const calls=[],listeners=new Map();
  const bridge={apiGet:async(route,query)=>{calls.push({method:'GET',route,query});return {status:'ok',data:{items:[],counts:{pending:0,adopted:0}}};},
    apiPost:async(route,body,options)=>{calls.push({method:'POST',route,body,options});return {status:'ok',data:{simulation:false,state:'complete',receipts:{empty:true}}};}};
  const root={isConnected:true,innerHTML:'',addEventListener:(name,fn)=>listeners.set(name,fn),removeEventListener:name=>listeners.delete(name)};
  const window={CompanionProduct:{bridgeNow:()=>bridge,unwrap:value=>value},lucide:{createIcons(){}}};
  const sandbox=vm.createContext({window,setTimeout,clearTimeout,Date,Math,Number,Object,Array,String});
  vm.runInContext(source,sandbox);
  const page=window.CompanionExpressionSignalsPage;
  page.mount(root,{replyApi:'/astrbot_plugin_private_companion/targets/private/roles/persona/reply',scopeKey:'one',manual:true,stateFilter:'pending'});
  for(let n=0;n<20&&page.state.busy;n++)await flush();
  assert.match(root.innerHTML,/立即对照学习/);assert.match(root.innerHTML,/待审/);assert.match(root.innerHTML,/已启用/);
  assert.doesNotMatch(root.innerHTML,/学习设置|每日对照学习|观察素材|允许.*学习|模仿对象/);
  listeners.get('click')({target:{closest:selector=>selector==='[data-expression-learn]'?{}:null}});
  for(let n=0;n<30&&!root.innerHTML.includes('暂无新增回复');n++)await flush();
  const writes=calls.filter(c=>c.method==='POST');assert.equal(writes.length,1);
  assert.equal(writes[0].route,'setup/daily-diagnostic');assert.equal(writes[0].body.mode,'expression');
  assert.equal(writes[0].body.simulation,false);assert.equal(writes[0].body.confirm_delivery,false);assert.equal(writes[0].options.timeoutMs,360000);
  assert.ok(calls.filter(c=>c.method==='GET').every(c=>c.route.endsWith('/style')));
  assert.match(root.innerHTML,/暂无新增回复/);page.unmount();
});

async function manualComparisonHarness(response, problem, options={}) {
  const calls=[],listeners=new Map();let finish;
  const settled=new Promise(resolve=>{finish=resolve;});
  const bridge={
    apiGet:async(route,query)=>{const items=calls.some(call=>call.method==="POST")?options.items || []:[];
      calls.push({method:"GET",route,query});return {status:"ok",data:{items,counts:{pending:items.length,adopted:0}}};},
    apiPost:async(route,body)=>{calls.push({method:"POST",route,body});await settled;if(problem)throw problem;return response;},
  };
  const root={isConnected:true,innerHTML:"",addEventListener:(name,fn)=>listeners.set(name,fn),removeEventListener:name=>listeners.delete(name)};
  const window={CompanionProduct:{bridgeNow:()=>bridge,unwrap:value=>value},lucide:{createIcons(){}}};
  vm.runInContext(source,vm.createContext({window,setTimeout,clearTimeout,Date,Math,Number,Object,Array,String}));
  const page=window.CompanionExpressionSignalsPage;
  page.mount(root,{replyApi:`/${HOST}/targets/private/roles/persona/reply`,manual:true,stateFilter:options.initialState || "pending"});
  for(let n=0;n<20&&page.state.busy;n++)await flush();
  const click=()=>listeners.get("click")({target:{closest:selector=>selector==="[data-expression-learn]"?{}:null}});
  click();click();
  for(let n=0;n<20&&!calls.some(call=>call.method==="POST");n++)await flush();
  assert.equal(calls.filter(call=>call.method==="POST").length,1);
  assert.match(root.innerHTML,/学习中/);
  finish();
  for(let n=0;n<30&&page.state.busy;n++)await flush();
  assert.equal(page.state.busy,false);
  assert.equal(calls.filter(call=>call.method==="POST").length,1);
  assert.equal(calls.filter(call=>call.method==="GET").length,2);
  assert.ok(calls.filter(call=>call.method==="GET").every(call=>call.route.endsWith("/style")));
  assert.ok(calls.filter(call=>call.method==="POST").every(call=>call.route==="setup/daily-diagnostic"));
  return {page,root,calls,click:selector=>listeners.get("click")({target:{closest:value=>value===selector?{}:null}})};
}

test("manual comparison shows insufficient DeepSeek balance from result or receipt codes without retries",async()=>{
  for(const result of [
    {simulation:false,state:"failed",code:"model_insufficient_balance",stage:"model_generation",error:"redacted"},
    {simulation:false,state:"failed",receipts:{code:"model_insufficient_balance",stage:"model_generation"}},
  ]){
    const h=await manualComparisonHarness({status:"ok",data:result});
    assert.match(h.root.innerHTML,/对照学习失败/);
    assert.match(h.root.innerHTML,/DeepSeek API 余额不足，请充值后重试。/);
    assert.match(h.root.innerHTML,/阶段：模型生成/);
    assert.match(h.root.innerHTML,/lh-inline-status is-error/);
    assert.doesNotMatch(h.root.innerHTML,/对照学习完成|对照学习未完成/);
    h.page.unmount();
  }
});

test("manual comparison shows approved-evidence validation failures without reporting completion",async()=>{
  for(const [code,text] of [
    ["expression_approved_quote_required","模型引用的原文与认可示例不匹配"],
    ["expression_unapproved_pattern","模型生成的说法缺少认可原文支持"],
    ["expression_approved_evidence_required","模型生成的规则缺少认可示例依据"],
  ]){
    const h=await manualComparisonHarness({status:"ok",data:{simulation:false,state:"failed",
      code,stage:"output_validation",receipts:{code,expression:{pending_rules:0,rejected_rules:1,
        cursor_advanced:false,rejections:[{index:1,code}]}}}});
    assert.match(h.root.innerHTML,/对照学习失败/);
    assert.match(h.root.innerHTML,/阶段：输出校验/);
    assert.ok(h.root.innerHTML.includes(text));
    assert.match(h.root.innerHTML,/新增待审 0 条/);
    assert.match(h.root.innerHTML,/未通过 1 条/);
    assert.match(h.root.innerHTML,/整理位置已保留/);
    assert.match(h.root.innerHTML,/查看未通过项/);
    assert.doesNotMatch(h.root.innerHTML,/对照学习完成/);
    h.page.unmount();
  }
});

test("manual comparison identifies local preflight failures before provider dispatch",async()=>{
  for(const [code,text] of [
    ["model_estimation_failed","本机请求用量预估失败"],
    ["host_model_limit","本次请求超出模型配置的输入或输出上限"],
  ]){
    const h=await manualComparisonHarness({status:"ok",data:{simulation:false,state:"failed",
      code,stage:"model_generation",receipts:{code}}});
    assert.match(h.root.innerHTML,/对照学习失败/);
    assert.match(h.root.innerHTML,/阶段：模型生成/);
    assert.ok(h.root.innerHTML.includes(text));
    assert.match(h.root.innerHTML,/尚未发送模型请求，整理位置保持不变/);
    assert.doesNotMatch(h.root.innerHTML,/对照学习完成/);
    h.page.unmount();
  }
});

test("manual comparison preserves diagnostic codes and stages from API and thrown exceptions",async()=>{
  for(const [response,problem] of [
    [{status:"error",data:{code:"model_insufficient_balance",stage:"model_generation"}},null],
    [null,Object.assign(new Error("model_insufficient_balance"),{stage:"model_generation"})],
    [{status:"ok",data:{status:"error",data:{receipts:{code:"future_provider_error",stage:"model_generation"}}}},null],
  ]){
    const h=await manualComparisonHarness(response,problem);
    assert.match(h.root.innerHTML,/对照学习失败/);
    assert.match(h.root.innerHTML,/阶段：模型生成/);
    assert.match(h.root.innerHTML,/DeepSeek API 余额不足|错误码：future_provider_error/);
    assert.doesNotMatch(h.root.innerHTML,/对照学习完成/);
    h.page.unmount();
  }
});

test("manual comparison distinguishes unknown and ongoing results from completion",async()=>{
  for(const state of ["unknown","running","prepared","received","in_progress"]){
    const h=await manualComparisonHarness({status:"ok",data:{simulation:false,state,stage:"model_generation"}});
    assert.match(h.root.innerHTML,state==="unknown"?/对照学习结果待确认/:/对照学习处理中/);
    assert.match(h.root.innerHTML,/阶段：模型生成/);
    assert.doesNotMatch(h.root.innerHTML,/对照学习完成|对照学习失败/);
    assert.equal(/lh-inline-status is-error/.test(h.root.innerHTML),state==="unknown");
    h.page.unmount();
  }
  const h=await manualComparisonHarness(null,Object.assign(new Error("timeout"),{code:"bridge_timeout"}));
  assert.match(h.root.innerHTML,/对照学习结果待确认/);
  assert.match(h.root.innerHTML,/页面连接超时/);
  assert.doesNotMatch(h.root.innerHTML,/对照学习完成/);
  h.page.unmount();
});

test("manual comparison accepts complete results with no candidates and validates diagnostic text",async()=>{
  const h=await manualComparisonHarness({status:"ok",data:{simulation:false,state:"complete",receipts:{expression:{pending_rules:0}}}});
  assert.match(h.root.innerHTML,/对照学习完成，未生成新候选。/);
  assert.doesNotMatch(h.root.innerHTML,/lh-inline-status is-error|对照学习失败/);
  h.page.unmount();
  const unsafe=await manualComparisonHarness({status:"ok",data:{simulation:false,state:"failed",code:'<script>alert(1)</script>',stage:'secret key with spaces',error:'sk-sensitive-token'}});
  assert.match(unsafe.root.innerHTML,/对照学习失败/);
  assert.doesNotMatch(unsafe.root.innerHTML,/script|sensitive-token|secret key/);
  unsafe.page.unmount();
});

const candidateRejections=[
  {index:1,code:"expression_unapproved_pattern",pattern:"干嘛/干嘛呀",unsupported_examples:["怎么又叫我宝宝"]},
  {index:2,code:"expression_platform_marker",pattern:"[回复]",unsupported_examples:["[回复]"]},
];

test("partial comparison shows saved and rejected counts, keeps the checkpoint and opens pending rules",async()=>{
  const item={signal_id:"approved-rule",state:"pending",dimension:"diction",proposal:"仅保留已认可的说法",
    expression:{kind:"style",pattern:"合格的说法"},actions:["adopt","reject"],view_token:"token"};
  const h=await manualComparisonHarness({status:"ok",data:{simulation:false,state:"partial",
    code:"expression_unapproved_pattern",stage:"output_validation",receipts:{expression:{
      pending_rules:1,rejected_rules:2,cursor_advanced:false,rejections:candidateRejections}}}},null,{initialState:"active",items:[item]});
  assert.match(h.root.innerHTML,/对照学习部分完成/);
  assert.match(h.root.innerHTML,/新增待审 1 条/);assert.match(h.root.innerHTML,/未通过 2 条/);
  assert.match(h.root.innerHTML,/整理位置已保留/);
  assert.doesNotMatch(h.root.innerHTML,/未写入规则|对照学习失败|lh-inline-status is-error/);
  assert.match(h.root.innerHTML,/data-signal-state="pending" aria-pressed="true"/);
  assert.equal(h.calls.filter(call=>call.method==="GET").at(-1).query.state,"pending");
  assert.match(h.root.innerHTML,/lh-signal-card is-pending/);assert.match(h.root.innerHTML,/合格的说法/);
  assert.match(h.root.innerHTML,/data-learning-rejections/);assert.match(h.root.innerHTML,/查看未通过项 · 2 条/);
  assert.match(h.root.innerHTML,/第 1 条 · 模型生成的说法缺少认可原文支持/);
  assert.match(h.root.innerHTML,/第 2 条 · 候选包含平台标记/);
  assert.match(h.root.innerHTML,/候选说法：干嘛\/干嘛呀/);
  assert.match(h.root.innerHTML,/未获支持片段：<span>怎么又叫我宝宝<\/span>/);
  assert.match(h.root.innerHTML,/候选说法：\[回复\]/);
  h.click("[data-signal-refresh]");
  for(let n=0;n<20&&h.page.state.busy;n++)await flush();
  assert.doesNotMatch(h.root.innerHTML,/data-learning-rejections|对照学习部分完成/);
  assert.equal(h.calls.filter(call=>call.method==="POST").length,1);
  h.page.unmount();
});

test("fully rejected comparison retains per-candidate reasons and reports zero pending additions",async()=>{
  const h=await manualComparisonHarness({status:"ok",data:{simulation:false,state:"failed",
    code:"expression_unapproved_pattern",stage:"output_validation",receipts:{code:"expression_unapproved_pattern",
      expression:{pending_rules:0,rejected_rules:2,cursor_advanced:false,rejections:candidateRejections}}}});
  assert.match(h.root.innerHTML,/对照学习失败/);assert.match(h.root.innerHTML,/lh-inline-status is-error/);
  assert.match(h.root.innerHTML,/新增待审 0 条/);assert.match(h.root.innerHTML,/未通过 2 条/);
  assert.match(h.root.innerHTML,/整理位置已保留/);assert.match(h.root.innerHTML,/查看未通过项 · 2 条/);
  assert.match(h.root.innerHTML,/expression_platform_marker/);assert.match(h.root.innerHTML,/怎么又叫我宝宝/);
  assert.doesNotMatch(h.root.innerHTML,/对照学习完成|对照学习部分完成/);
  h.page.unmount();
});

test("candidate rejection diagnostics escape fragments and preserve valid source sequence numbers",async()=>{
  const h=await manualComparisonHarness({status:"ok",data:{simulation:false,state:"failed",
    code:"expression_candidate_invalid",receipts:{expression:{pending_rules:0,rejected_rules:2,cursor_advanced:false,
      rejections:[{index:3,code:"expression_candidate_invalid",pattern:'<img src=x onerror="attack()">',
        unsupported_examples:['<script>attack()</script>',null,{}]},
        {index:4,code:"future_candidate_error",pattern:"普通片段",unsupported_examples:[]}]}}}});
  assert.match(h.root.innerHTML,/第 3 条 · 候选字段或结构不符合要求/);
  assert.match(h.root.innerHTML,/第 4 条 · 错误码：future_candidate_error/);
  assert.match(h.root.innerHTML,/&lt;img src=x onerror=&quot;attack\(\)&quot;&gt;/);
  assert.match(h.root.innerHTML,/&lt;script&gt;attack\(\)&lt;\/script&gt;/);
  assert.doesNotMatch(h.root.innerHTML,/<img src=x|<script>|\[object Object\]/);
  h.page.unmount();
});

test("API failures retain rejected-candidate receipts alongside the original failure status",async()=>{
  const h=await manualComparisonHarness({status:"error",data:{code:"expression_platform_marker",stage:"output_validation",
    receipts:{expression:{pending_rules:0,rejected_rules:1,cursor_advanced:false,rejections:[candidateRejections[1]]}}}});
  assert.match(h.root.innerHTML,/对照学习失败/);assert.match(h.root.innerHTML,/阶段：输出校验/);
  assert.match(h.root.innerHTML,/新增待审 0 条/);assert.match(h.root.innerHTML,/候选包含平台标记/);
  assert.match(h.root.innerHTML,/第 2 条/);assert.match(h.root.innerHTML,/候选说法：\[回复\]/);
  assert.match(h.root.innerHTML,/整理位置已保留/);
  h.page.unmount();
});


function polarityRule(id,polarity) {
  return {signal_id:id,run_id:"run-"+id,dimension:"diction",source_kind:"conversation_input",
    observation:"普通私聊",proposal:polarity==="negative"?"避免反复加入动作旁白":"参考认可示例中的简短说法",
    state:"pending",created_at:"2026-11-06T00:00:00Z",actions:["adopt","reject"],can_revise:true,
    expression:{kind:polarity==="negative"?"grammar":"style",situation:"普通私聊",pattern:id,
      avoid:polarity==="negative"?"用户明确要求角色扮演时":"严肃求助时",...(polarity?{polarity}:{})},
    evidence:polarity==="negative"?["认可示例：嗯，明天见","对照反例：<动作旁白>"]:["认可示例：嗯，明天见"],
    view_token:"view-"+id};
}
async function polarityHarness(rows,{preview=null}={}) {
  const savedRows=JSON.parse(JSON.stringify(rows)),calls=[],listeners=new Map();
  const success=data=>({status:"ok",data});
  const bridge={apiGet:async(route,query)=>{
      calls.push({method:"GET",route,query});
      const items=savedRows.filter(row=>row.state===query.state);
      return success({items,counts:{pending:savedRows.filter(row=>row.state==="pending").length,
        adopted:savedRows.filter(row=>row.state==="adopted").length}});
    },apiPost:async(route,body)=>{
      calls.push({method:"POST",route,body:JSON.parse(JSON.stringify(body))});
      if(route.endsWith("/style/import/preview"))return success(preview);
      const item=savedRows.find(row=>row.signal_id===body.signal_id);
      if(route.endsWith("/style/revise"))Object.assign(item,JSON.parse(JSON.stringify(body.changes)));
      if(route.endsWith("/style/command")){
        item.state=body.action==="adopt"?"adopted":"rejected";
        item.actions=item.state==="adopted"?["retire"]:["reconsider"];
      }
      return success({});
    }};
  const root={isConnected:true,innerHTML:"",addEventListener:(name,fn)=>listeners.set(name,fn),
    removeEventListener:name=>listeners.delete(name),querySelector:()=>null,querySelectorAll:()=>[]};
  const window={CompanionProduct:{bridgeNow:()=>bridge,unwrap:value=>value},lucide:{createIcons(){}}};
  vm.runInContext(source,vm.createContext({window,setTimeout,clearTimeout,TextEncoder,Date,Math,Number,Object,Array,String,URLSearchParams}));
  const page=window.CompanionExpressionSignalsPage;
  const drain=async()=>{for(let n=0;n<5;n++)await flush();};
  page.mount(root,{replyApi:`/${HOST}/targets/private/roles/persona/reply`,scopeKey:"polarity",stateFilter:"pending"});
  await drain();
  const click=async(selector,dataset={})=>{listeners.get("click")({target:{closest:value=>value===selector?{dataset}:null}});await drain();};
  const change=async(selector,value)=>{const target={value,closest:current=>current===selector?target:null};
    listeners.get("change")({target});await drain();};
  const input=(name,value)=>{const target={value,dataset:{ruleField:name},closest:selector=>selector==="[data-rule-field]"?target:null};listeners.get("input")({target});};
  const importText=async(value)=>{const target={value,closest:selector=>selector==="[data-share-import-json]"?target:null};
    listeners.get("input")({target});await drain();};
  const condition=async(name,value,checked)=>{const target={value,checked,dataset:{ruleCondition:name},
    closest:selector=>selector==="[data-rule-condition]"?target:null};listeners.get("change")({target});await drain();};
  return {page,root,calls,click,change,input,condition,importText,drain};
}

test("rule polarity labels and filters include legacy positive rules without writing changes",async()=>{
  const h=await polarityHarness([polarityRule("传统正向示例"),polarityRule("已标正向示例","positive"),polarityRule("反复动作旁白","negative")]);
  assert.match(h.root.innerHTML,/data-rule-polarity="positive">正向表达/);
  assert.match(h.root.innerHTML,/data-rule-polarity="negative">负面约束/);
  assert.match(h.root.innerHTML,/约束建议/);assert.match(h.root.innerHTML,/例外情境/);
  assert.match(h.root.innerHTML,/认可示例：嗯，明天见/);
  assert.match(h.root.innerHTML,/对照反例：&lt;动作旁白&gt;/);assert.doesNotMatch(h.root.innerHTML,/<动作旁白>/);
  await h.change("[data-rule-polarity-filter]","positive");
  assert.match(h.root.innerHTML,/<h3>传统正向示例<\/h3>/);assert.match(h.root.innerHTML,/<h3>已标正向示例<\/h3>/);
  assert.doesNotMatch(h.root.innerHTML,/<h3>反复动作旁白<\/h3>/);
  await h.change("[data-rule-polarity-filter]","negative");
  assert.match(h.root.innerHTML,/<h3>反复动作旁白<\/h3>/);assert.doesNotMatch(h.root.innerHTML,/<h3>传统正向示例<\/h3>|<h3>已标正向示例<\/h3>/);
  await h.change("[data-rule-kind-filter]","style");assert.match(h.root.innerHTML,/当前筛选下没有表达规则/);
  await h.change("[data-rule-kind-filter]","all");assert.match(h.root.innerHTML,/<h3>反复动作旁白<\/h3>/);
  await h.change("[data-rule-polarity-filter]","all");assert.match(h.root.innerHTML,/<h3>传统正向示例<\/h3>/);
  assert.equal(h.calls.filter(call=>call.method==="POST").length,0);
  assert.equal(h.calls.filter(call=>call.method==="GET").length,1);
  h.page.unmount();
});

test("negative rule edits keep read-only polarity while kind and constraint fields remain editable",async()=>{
  const h=await polarityHarness([polarityRule("动作旁白","negative")]);
  await h.click("[data-signal-edit]",{signalEdit:"polarity::动作旁白"});
  const editor=h.root.innerHTML.slice(h.root.innerHTML.indexOf('<section class="lh-rule-editor"'));
  assert.match(editor,/data-rule-polarity="negative">负面约束/);
  assert.match(editor,/应避免的说法或句式/);assert.match(editor,/约束建议/);assert.match(editor,/例外情境/);
  assert.doesNotMatch(editor,/data-rule-field="polarity"/);
  h.input("polarity","positive");h.input("kind","style");h.input("pattern","反复使用括号动作");
  h.input("proposal","省略没有必要的动作旁白");h.input("avoid","用户明确要求描写动作时");
  await h.click("[data-rule-save]");
  const edit=h.calls.find(call=>call.route.endsWith("/style/revise"));
  assert.equal(edit.body.changes.expression.polarity,"negative");assert.equal(edit.body.changes.expression.kind,"style");
  assert.equal(edit.body.changes.expression.pattern,"反复使用括号动作");
  assert.equal(edit.body.changes.expression.avoid,"用户明确要求描写动作时");
  assert.equal(edit.body.changes.proposal,"省略没有必要的动作旁白");
  assert.match(h.root.innerHTML,/data-rule-polarity="negative">负面约束/);
  assert.match(h.root.innerHTML,/表达规则已更新/);h.page.unmount();
});

test("positive and negative rules share the same pending adoption workflow",async()=>{
  const h=await polarityHarness([polarityRule("正向候选","positive"),polarityRule("负面候选","negative")]);
  await h.click("[data-signal-action]",{signalAction:"adopt",id:"polarity::正向候选"});
  assert.doesNotMatch(h.root.innerHTML,/<h3>正向候选<\/h3>/);assert.match(h.root.innerHTML,/<h3>负面候选<\/h3>/);
  await h.click("[data-signal-action]",{signalAction:"adopt",id:"polarity::负面候选"});
  const writes=h.calls.filter(call=>call.method==="POST");assert.equal(writes.length,2);
  assert.ok(writes.every(call=>call.route.endsWith("/style/command")&&call.body.action==="adopt"));
  await h.click("[data-signal-state]",{signalState:"active"});
  assert.match(h.root.innerHTML,/<h3>正向候选<\/h3>/);assert.match(h.root.innerHTML,/<h3>负面候选<\/h3>/);
  assert.match(h.root.innerHTML,/data-rule-polarity="negative">负面约束/);
  h.page.unmount();
});

test("sharing previews show polarity for nested, top-level and legacy candidate forms",async()=>{
  const h=await polarityHarness([],{preview:{title:"规则预览",importable_rule_count:3,duplicate_rule_count:0,
    capacity_blocked_count:0,rejected_count:0,preview_token:"b".repeat(64),candidates:[
      {dimension:"diction",observation:"对照情境",proposal:"避免反复旁白",expression:{polarity:"negative"}},
      {dimension:"diction",observation:"认可情境",proposal:"采用认可说法",polarity:"positive"},
      {dimension:"formatting",observation:"旧规则",proposal:"保留自然分段"}]}});
  await h.click("[data-share-toggle]");
  const json=JSON.stringify({schema:"private-companion-expression-pack",version:1,rule_groups:[]});
  await h.importText(json);await h.click("[data-share-preview]");
  assert.match(h.root.innerHTML,/data-rule-polarity="negative">负面约束 · 措辞/);
  assert.equal((h.root.innerHTML.match(/<b data-rule-polarity="positive">正向表达/g) || []).length,2);
  assert.match(h.root.innerHTML,/避免反复旁白/);assert.match(h.root.innerHTML,/保留自然分段/);
  const writes=h.calls.filter(call=>call.method==="POST");assert.equal(writes.length,1);
  assert.ok(writes[0].route.endsWith("/style/import/preview"));assert.equal(writes[0].body.package_json,json);
  h.page.unmount();
});


test("negative-rule counterexample errors show their Chinese reasons without reporting success",async()=>{
  for(const [code,message] of [
    ["expression_counterexample_required","负面约束缺少未认可回复中的对照反例。"],
    ["expression_counterexample_invalid","负面约束引用的对照反例无效。"],
  ]){
    const h=await manualComparisonHarness({status:"ok",data:{simulation:false,state:"failed",code,stage:"output_validation",
      receipts:{code,expression:{pending_rules:0,rejected_rules:1,cursor_advanced:false,rejections:[{index:1,code,pattern:"重复动作旁白"}]}}}});
    assert.ok(h.root.innerHTML.includes(message));assert.match(h.root.innerHTML,/对照学习失败/);
    assert.match(h.root.innerHTML,/新增待审 0 条/);assert.match(h.root.innerHTML,/整理位置已保留/);
    assert.match(h.root.innerHTML,/查看未通过项/);assert.doesNotMatch(h.root.innerHTML,/对照学习完成/);
    h.page.unmount();
  }
});


function repeatSourceFixture(key,roleId=key,scope="private") {
  return {key,scopeKey:key,label:"来源 "+key,replyApi:`/${HOST}/targets/${key}/roles/${roleId}/reply`,
    target:{scope_kind:scope,bot_id:"bot-one"},role:{role_id:roleId,name:roleId,
      config_api:`/${HOST}/targets/${key}/roles/${roleId}/config`}};
}
async function repeatControlHarness(sources,options={}) {
  const calls=[],listeners=new Map(),states=new Map(sources.map((source,index)=>[source.role.config_api.slice(HOST.length+2),
    {value:options.values?.[source.key]??true,token:String(index+1).repeat(64)}]));
  let readFailure=options.readFailure || false,saveFailure=options.saveFailure || null;
  let finishSave,finishLearning,finishRead;
  let saveGate=options.holdSave?new Promise(resolve=>{finishSave=resolve;}):null;
  let learningGate=options.holdLearning?new Promise(resolve=>{finishLearning=resolve;}):null;
  let readGate=options.holdRead?new Promise(resolve=>{finishRead=resolve;}):null;
  const ok=data=>({status:"ok",data});
  const bridge={apiGet:async(route,query)=>{
      calls.push({method:"GET",route,query});
      if(route.endsWith("/style"))return ok({items:[],counts:{pending:0,adopted:0}});
      if(readGate)await readGate;
      if(readFailure)throw Object.assign(new Error("configuration_section_read_failed"),{code:"configuration_section_read_failed"});
      const state=states.get(route);assert.ok(state,route);
      return ok({sections:{"expression.learning":{settings:{skip_processed_replies:state.value},edit_token:state.token,
        fields:{skip_processed_replies:{kind:"boolean"}}}}});
    },apiPost:async(route,body)=>{
      calls.push({method:"POST",route,body:JSON.parse(JSON.stringify(body))});
      if(route==="setup/daily-diagnostic"){
        if(learningGate)await learningGate;
        return ok({simulation:false,state:"complete",receipts:{empty:true}});
      }
      if(saveGate)await saveGate;
      if(saveFailure)throw Object.assign(new Error(saveFailure),{code:saveFailure});
      const state=states.get(route.replace(/\/configure$/,""));assert.ok(state,route);
      state.value=body.changes.skip_processed_replies;state.token="z".repeat(64);
      if(options.failReadAfterSave)readFailure=true;
      return ok({result:{operation_id:"repeat-save",replayed:false}});
    }};
  const root={isConnected:true,innerHTML:"",addEventListener:(name,fn)=>listeners.set(name,fn),
    removeEventListener:name=>listeners.delete(name),querySelector:()=>null};
  const window={CompanionProduct:{bridgeNow:()=>bridge,unwrap:value=>value,
      role:()=>({config_api:options.currentConfig || sources[0]?.role.config_api})},lucide:{createIcons(){}}};
  vm.runInContext(source,vm.createContext({window,setTimeout,clearTimeout,Date,Math,Number,Object,Array,String,Set,URLSearchParams}));
  const page=window.CompanionExpressionSignalsPage;
  const drain=async()=>{for(let n=0;n<8;n++)await flush();};
  const mount=()=>page.mount(root,{sources,manual:true,stateFilter:"pending"});
  mount();await drain();
  const change=async(selector,value)=>{const target={value,checked:value===true,closest:current=>current===selector?target:null};
    listeners.get("change")({target});await drain();};
  const click=async(selector)=>{listeners.get("click")({target:{closest:current=>current===selector?{}:null}});await drain();};
  return {page,root,calls,states,change,click,drain,
    remount:async()=>{mount();await drain();},
    finishRead:async()=>{finishRead?.();readGate=null;await drain();},
    finishSave:async()=>{finishSave?.();saveGate=null;await drain();},
    finishLearning:async()=>{finishLearning?.();learningGate=null;await drain();},
    setReadFailure:value=>{readFailure=value;},setSaveFailure:value=>{saveFailure=value;}};
}
const repeatInput=html=>html.match(/<input[^>]*data-skip-processed-replies[^>]*>/)?.[0] || "";

test("skip-processed control reads role defaults, saves one field and confirms it with a reread",async()=>{
  const source=repeatSourceFixture("one","OvO"),h=await repeatControlHarness([source]);
  assert.match(repeatInput(h.root.innerHTML),/checked/);assert.doesNotMatch(repeatInput(h.root.innerHTML),/disabled/);
  const gets=h.calls.filter(call=>call.method==="GET"&&!call.route.endsWith("/style"));
  assert.equal(gets.length,1);assert.equal(gets[0].query.sections,"expression.learning");
  await h.change("[data-skip-processed-replies]",false);
  const writes=h.calls.filter(call=>call.method==="POST");assert.equal(writes.length,1);
  assert.equal(writes[0].route,"targets/one/roles/OvO/config/configure");
  assert.equal(writes[0].body.section,"expression.learning");assert.equal(writes[0].body.edit_token,"1".repeat(64));
  assert.deepEqual(writes[0].body.changes,{skip_processed_replies:false});assert.match(writes[0].body.operation_key,/^expression-repeat-/);
  assert.equal(Object.keys(writes[0].body).sort().join(","),"changes,edit_token,operation_key,section");
  assert.doesNotMatch(repeatInput(h.root.innerHTML),/checked/);assert.match(h.root.innerHTML,/已保存。/);
  assert.equal(h.calls.filter(call=>call.method==="GET"&&!call.route.endsWith("/style")).length,2);
  await h.remount();assert.doesNotMatch(repeatInput(h.root.innerHTML),/checked/);
  assert.equal(h.calls.filter(call=>call.method==="POST").length,1);h.page.unmount();
});

test("same-role private sources share one setting and group sources are excluded",async()=>{
  const one=repeatSourceFixture("one","OvO"),two=repeatSourceFixture("two","OvO"),group=repeatSourceFixture("group","Other","group");
  const h=await repeatControlHarness([one,two,group]);
  assert.doesNotMatch(h.root.innerHTML,/data-expression-repeat-source/);
  await h.change("[data-skip-processed-replies]",false);
  const configCalls=h.calls.filter(call=>call.route.includes("/config"));
  assert.ok(configCalls.every(call=>call.route.startsWith("targets/one/roles/OvO/config")));
  assert.equal(configCalls.filter(call=>call.method==="POST").length,1);h.page.unmount();
});

test("different role settings select one role and use that role edit token",async()=>{
  const one=repeatSourceFixture("one","OvO"),two=repeatSourceFixture("two","Another");
  const h=await repeatControlHarness([one,two],{values:{one:true,two:false}});
  assert.match(h.root.innerHTML,/设置角色/);
  await h.change("[data-expression-repeat-source]","two");
  assert.doesNotMatch(repeatInput(h.root.innerHTML),/checked/);
  await h.change("[data-skip-processed-replies]",true);
  const writes=h.calls.filter(call=>call.method==="POST");assert.equal(writes.length,1);
  assert.equal(writes[0].route,"targets/two/roles/Another/config/configure");assert.equal(writes[0].body.edit_token,"2".repeat(64));
  assert.equal(h.states.get("targets/one/roles/OvO/config").value,true);h.page.unmount();
});

test("settings stay disabled until read and a failed read exposes a retry without model requests",async()=>{
  const h=await repeatControlHarness([repeatSourceFixture("one","OvO")],{holdRead:true});
  assert.match(repeatInput(h.root.innerHTML),/disabled/);assert.match(h.root.innerHTML,/读取中/);
  await h.change("[data-rule-polarity-filter]","negative");assert.match(h.root.innerHTML,/当前筛选下没有表达规则/);
  await h.change("[data-skip-processed-replies]",false);
  assert.equal(h.calls.filter(call=>call.method==="POST").length,0);
  h.setReadFailure(true);await h.finishRead();assert.match(repeatInput(h.root.innerHTML),/disabled/);
  assert.match(h.root.innerHTML,/设置读取失败/);assert.match(h.root.innerHTML,/data-expression-repeat-refresh/);
  h.setReadFailure(false);await h.click("[data-expression-repeat-refresh]");
  assert.match(repeatInput(h.root.innerHTML),/checked/);assert.doesNotMatch(repeatInput(h.root.innerHTML),/disabled/);
  assert.equal(h.calls.filter(call=>call.method==="POST").length,0);h.page.unmount();
});

test("a pending setting save keeps the confirmed value and blocks duplicate saves and learning",async()=>{
  const h=await repeatControlHarness([repeatSourceFixture("one","OvO")],{holdSave:true});
  await h.change("[data-skip-processed-replies]",false);await h.change("[data-skip-processed-replies]",false);
  assert.match(repeatInput(h.root.innerHTML),/checked/);assert.match(repeatInput(h.root.innerHTML),/disabled/);
  assert.match(h.root.innerHTML,/保存中/);assert.doesNotMatch(h.root.innerHTML,/已保存。/);
  await h.click("[data-expression-learn]");assert.equal(h.calls.filter(call=>call.method==="POST").length,1);
  await h.finishSave();assert.doesNotMatch(repeatInput(h.root.innerHTML),/checked/);assert.match(h.root.innerHTML,/已保存。/);
  h.page.unmount();
});

test("learning disables the repeat setting until the single model task returns",async()=>{
  const h=await repeatControlHarness([repeatSourceFixture("one","OvO")],{holdLearning:true});
  await h.click("[data-expression-learn]");assert.match(repeatInput(h.root.innerHTML),/disabled/);
  await h.change("[data-skip-processed-replies]",false);
  assert.equal(h.calls.filter(call=>call.method==="POST").length,1);
  assert.equal(h.calls.find(call=>call.method==="POST").route,"setup/daily-diagnostic");
  await h.finishLearning();assert.doesNotMatch(repeatInput(h.root.innerHTML),/disabled/);h.page.unmount();
});

test("conflicted saves and unconfirmed rereads show actual settings without false success",async()=>{
  const source=repeatSourceFixture("one","OvO"),conflict=await repeatControlHarness([source],{saveFailure:"configuration_revision_conflict"});
  await conflict.change("[data-skip-processed-replies]",false);
  assert.match(repeatInput(conflict.root.innerHTML),/checked/);assert.match(conflict.root.innerHTML,/配置已变化/);
  assert.doesNotMatch(conflict.root.innerHTML,/已保存。/);assert.equal(conflict.calls.filter(call=>call.method==="POST").length,1);
  conflict.page.unmount();
  const unconfirmed=await repeatControlHarness([source],{failReadAfterSave:true});
  await unconfirmed.change("[data-skip-processed-replies]",false);
  assert.match(repeatInput(unconfirmed.root.innerHTML),/disabled/);assert.match(unconfirmed.root.innerHTML,/保存结果待确认/);
  assert.doesNotMatch(unconfirmed.root.innerHTML,/已保存。/);
  unconfirmed.setReadFailure(false);await unconfirmed.click("[data-expression-repeat-refresh]");
  assert.doesNotMatch(repeatInput(unconfirmed.root.innerHTML),/checked|disabled/);
  assert.equal(unconfirmed.calls.filter(call=>call.method==="POST").length,1);unconfirmed.page.unmount();
});

test("the shared positive and negative candidate limit has a specific failure status",async()=>{
  const h=await manualComparisonHarness({status:"ok",data:{simulation:false,state:"failed",code:"expression_rule_limit",stage:"output_validation"}});
  assert.match(h.root.innerHTML,/每次最多返回 3 条候选规则，正向与负面合计。/);
  assert.doesNotMatch(h.root.innerHTML,/对照学习完成/);h.page.unmount();
});


function intentRule(id,intent) {
  return {...polarityRule(id,"positive"),applicability:{channels:["private"],relationship_stages:["any"],
    emotion_gates:["any"],intent,keywords:[]}};
}
const checkedIntents=html=>[...html.matchAll(/<input[^>]*data-rule-condition="intent"[^>]*>/g)]
  .map(match=>match[0]).filter(input=>/\schecked(?:\s|>)/.test(input)).map(input=>input.match(/value="([^"]+)"/)[1]).sort();

test("multiple intent labels use Chinese and editing other fields preserves every matching intent",async()=>{
  const h=await polarityHarness([intentRule("多意图规则",["help","comfort"]),intentRule("旧单意图规则","question")]);
  assert.match(h.root.innerHTML,/<b>意图<\/b>求助、安慰/);assert.match(h.root.innerHTML,/<b>意图<\/b>提问/);
  await h.click("[data-signal-edit]",{signalEdit:"polarity::多意图规则"});
  assert.deepEqual(checkedIntents(h.root.innerHTML),["comfort","help"]);
  assert.match(h.root.innerHTML,/<legend>回复意图<\/legend>/);
  assert.doesNotMatch(h.root.innerHTML,/<select data-rule-condition="intent"/);
  h.input("pattern","修改后的说法");await h.click("[data-rule-save]");
  const edit=h.calls.find(call=>call.route.endsWith("/style/revise"));
  assert.deepEqual(edit.body.changes.applicability.intent,["comfort","help"]);
  assert.match(h.root.innerHTML,/<b>意图<\/b>安慰、求助/);h.page.unmount();
});

test("legacy single intent can become multiple intents and any stays exclusive with specific choices",async()=>{
  const h=await polarityHarness([intentRule("单意图规则","comfort")]);
  await h.click("[data-signal-edit]",{signalEdit:"polarity::单意图规则"});
  assert.deepEqual(checkedIntents(h.root.innerHTML),["comfort"]);
  await h.condition("intent","question",true);await h.change("[data-rule-kind-filter]","all");
  assert.deepEqual(checkedIntents(h.root.innerHTML),["comfort","question"]);
  await h.click("[data-rule-save]");
  assert.deepEqual(h.calls.filter(call=>call.route.endsWith("/style/revise")).at(-1).body.changes.applicability.intent,["comfort","question"]);
  await h.click("[data-signal-edit]",{signalEdit:"polarity::单意图规则"});
  await h.condition("intent","any",true);await h.change("[data-rule-kind-filter]","all");
  assert.deepEqual(checkedIntents(h.root.innerHTML),["any"]);await h.click("[data-rule-save]");
  assert.equal(h.calls.filter(call=>call.route.endsWith("/style/revise")).at(-1).body.changes.applicability.intent,"any");
  await h.click("[data-signal-edit]",{signalEdit:"polarity::单意图规则"});
  await h.condition("intent","help",true);await h.change("[data-rule-kind-filter]","all");
  assert.deepEqual(checkedIntents(h.root.innerHTML),["help"]);await h.click("[data-rule-save]");
  assert.equal(h.calls.filter(call=>call.route.endsWith("/style/revise")).at(-1).body.changes.applicability.intent,"help");
  h.page.unmount();
});

test("removing intent choices saves one remaining intent as a string and keeps empty selection valid",async()=>{
  const h=await polarityHarness([intentRule("可减少意图规则",["comfort","help"])]);
  await h.click("[data-signal-edit]",{signalEdit:"polarity::可减少意图规则"});
  await h.condition("intent","help",false);await h.click("[data-rule-save]");
  assert.equal(h.calls.filter(call=>call.route.endsWith("/style/revise")).at(-1).body.changes.applicability.intent,"comfort");
  await h.click("[data-signal-edit]",{signalEdit:"polarity::可减少意图规则"});
  await h.condition("intent","comfort",false);await h.change("[data-rule-kind-filter]","all");
  assert.deepEqual(checkedIntents(h.root.innerHTML),["any"]);await h.click("[data-rule-save]");
  assert.equal(h.calls.filter(call=>call.route.endsWith("/style/revise")).at(-1).body.changes.applicability.intent,"any");
  h.page.unmount();
});

test("applicability and expression form errors have explicit field-level status messages",async()=>{
  for(const [code,message] of [
    ["expression_applicability_invalid","候选适用条件不符合要求。"],
    ["expression_form_invalid","候选表达形式不符合要求。"],
  ]){
    const h=await manualComparisonHarness({status:"ok",data:{simulation:false,state:"failed",code,stage:"output_validation",
      receipts:{expression:{pending_rules:0,rejected_rules:1,cursor_advanced:false,rejections:[{index:1,code}]}}}});
    assert.ok(h.root.innerHTML.includes(message));assert.match(h.root.innerHTML,/对照学习失败/);
    assert.match(h.root.innerHTML,/查看未通过项/);assert.doesNotMatch(h.root.innerHTML,/对照学习完成/);
    h.page.unmount();
  }
});
