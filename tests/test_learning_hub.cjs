"use strict";
const test=require("node:test"),assert=require("node:assert/strict"),fs=require("node:fs"),path=require("node:path"),vm=require("node:vm");
const PAGE=path.join(__dirname,"../pages/陪伴小屋"),source=fs.readFileSync(path.join(PAGE,"app.js"),"utf8").replace(/\r\n/g,"\n");
const definition=name=>{const start=source.indexOf("function "+name+"(");assert.ok(start>=0,name);return source.slice(start,source.indexOf("\n}\n",start)+3);};
function harness(groups=false){
 const sandbox={learningTab:"expression",manageTabs:[["learning"],["memory"],["host"],["contact"],["expression"]],lifeTabs:[["home"],["diary"],["state"]],
  learningChoices:key=>key==="slang"?(groups?[{role:{role_id:"group-role"}}]:[]):[{role:{role_id:"private-role"}}],
  learningExpressionSources:()=>[{}],learningReviewSources:()=>[{}],learningScopeControls:()=>"",learningRoleControls:()=>"",
  configEditors:id=>`<section id="${id}"></section>`,consoleLoading:()=>"",escapeHTML:value=>String(value),navHref:(...parts)=>"#"+parts.join("/")};
 vm.createContext(sandbox);vm.runInContext(definition("navigationDestination")+definition("renderLearningPage"),sandbox);return sandbox;
}
test("private learning offers only expression rules and growth, with progress review inside growth",()=>{
 const h=harness();let html=h.renderLearningPage();
 assert.match(html,/expressionSignalsWorkspace/);assert.match(html,/表达学习/);assert.match(html,/成长链路/);
 assert.doesNotMatch(html,/总览|审阅中心|每日复盘|行为模仿|群聊黑话|观察素材|expressionWorkspace/);
 h.learningTab="chain";html=h.renderLearningPage();assert.match(html,/id="learningWorkspace"/);assert.match(html,/growthConfigWorkspace/);
 const growth=fs.readFileSync(path.join(PAGE,"learning-page.js"),"utf8");assert.match(growth,/\["reviews","待审阅"/);assert.match(growth,/async function review\(/);
});
test("retired learning links resolve to a live destination and slang requires group sources",()=>{
 const h=harness();for(const subtab of ["overview","review"])assert.equal(h.navigationDestination("manage","learning",subtab).subtab,"expression");
 for(const subtab of ["strategy","reflection","behavior","skills"])assert.equal(h.navigationDestination("manage","learning",subtab).subtab,"chain");
 assert.deepEqual(JSON.parse(JSON.stringify(h.navigationDestination("manage","learning","semantic"))),{next:"manage",tab:"memory",subtab:"settings"});
 h.learningTab="slang";assert.doesNotMatch(h.renderLearningPage(),/semanticSlangWorkspace/);
 const group=harness(true);group.learningTab="slang";assert.match(group.renderLearningPage(),/semanticSlangWorkspace/);
});
test("retired learning assets are no longer loaded or retained",()=>{
 const html=fs.readFileSync(path.join(PAGE,"index.html"),"utf8");
 for(const name of ["learning-overview.js","learning-review-page.js","self-review-page.js","self-review-page.css","behavior-page.js","behavior-page.css","expression-page.js","expression-samples-page.js"]){
  assert.ok(!fs.existsSync(path.join(PAGE,name)),name);assert.ok(!html.includes(name),name);
 }
 assert.match(html,/expression-signals-page\.js/);assert.match(source,/semanticMemoryWorkspace/);assert.doesNotMatch(source,/CompanionExpressionSamplesPage|CompanionLearningOverviewPage|CompanionSelfReviewPage|CompanionLearningReviewPage/);
});
