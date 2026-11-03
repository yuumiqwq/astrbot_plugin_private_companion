/* Live module controls. Each write goes to the existing owner and changes one field. */
"use strict";
window.CompanionModuleSwitches = (() => {
  const PREFIX = "/astrbot_plugin_private_companion/";
  const entries = [];
  const add = (id,label,pages,source,field,extra={}) => entries.push({id,label,pages:pages.split(" "),source,field,...extra});
  const cfg = (id,label,pages,section,field="enabled",extra={}) => add(id,label,pages,"config",field,{section,...extra});
  add("split","小屋分段","expression","reply","enabled",{locked:"由 AstrBot 按换行发送",selector:'#replyWorkspace [data-wb-field="enabled"]'});
  add("humanized","表达调整","expression","reply","enable_humanized_expression",{locked:"已停用",selector:'#replyWorkspace [data-wb-field="enable_humanized_expression"]'});
  add("voice","语音回复","expression","reply","voice_enabled",{locked:"已停用",selector:'#replyWorkspace [data-wb-field="voice_enabled"]'});
  add("reactions","表情图片","expression","semantic","reactions_enabled",{locked:"已停用",selector:'#semanticExpressionWorkspace [name="reactions_enabled"]'});
  cfg("learning","表达学习","learning","expression.learning","enabled");
  cfg("style-review","表达复核","learning expression","expression.learning","style_review_enabled",{locked:"已停用"});
  cfg("skills","技能成长","learning","role.growth.skills");
  cfg("profile","小屋角色档案","governance","role_profile","enabled",{locked:"沿用 AstrBot 人格"});
  cfg("knowledge","知识资料注入","governance","knowledge","inject_enabled");
  cfg("knowledge-import","知识资料导入","governance","knowledge","import_enabled");
  cfg("acquaintance","熟人档案","identity identity-people identity-feelings","person","acquaintance_enabled");
  cfg("cross-window-awareness","跨会话感知","identity identity-people","person","cross_window_awareness_enabled");
  cfg("cross-window-continuity","跨会话连续性","identity identity-people","person","cross_window_continuity_enabled");
  cfg("contact","主动联系","contact","proactive.contact","enabled",{global:true});
  cfg("automatic","自动主动联系","contact","proactive.contact","automatic_enabled",{global:true,parent:"contact"});
  cfg("quiet","免打扰时段","contact","proactive.contact","quiet_enabled",{global:true});
  cfg("morning","晨间流程","contact","proactive.contact","morning_chain_enabled",{global:true,parent:"contact"});
  cfg("contact-voice","主动语音","contact expression","proactive.contact","voice_action_enabled",{global:true,locked:"已停用",selector:'[data-field="voice_action_enabled"]'});
  cfg("news-read","每日热榜读取","perception contact","proactive.contact","news_daily_hot_read_enabled",{global:true});
  cfg("news-share","新闻分享源","perception contact","proactive.contact","news_share_enabled",{global:true,parent:"contact"});
  add("state","角色状态","state decision","state","enabled");
  add("mood","心情模拟","state","state","mood_enabled",{parent:"state"});
  add("calendar","角色日历","state","state","calendar_enabled");
  add("planning","自动日程规划","state","state","planning_enabled",{parent:"calendar"});
  cfg("life","生活例程","diary","life");
  cfg("life-derive","生活事件整理","diary","life","derive_enabled",{parent:"life"});
  cfg("diary","日记","diary","life","diary_enabled",{parent:"life"});
  cfg("sleep","睡眠与梦境","diary","life","sleep_enabled",{parent:"life"});
  cfg("reading","阅读活动","diary","life","reading_enabled",{parent:"life"});
  cfg("library","书架","diary","library");
  add("memory","长期记忆","memory","memory","enabled");
  add("reflection","每日记忆整理","memory","memory","reflection_enabled",{parent:"memory"});
  add("semantic-memory","记忆向量检索","memory","semantic","memory_enabled");
  cfg("writing","作品创作","output","content.authoring");
  cfg("wardrobe","衣柜","output","wardrobe");
  cfg("reference","参考图","output","photo.reference");
  cfg("busy","忙碌回复处理","decision","busy_gate","busy_reply_gate_enabled");
  cfg("web","网页理解","understanding","web_understanding");
  add("file","文件理解","understanding","reply","file_understanding_enabled");
  add("image","私聊图片理解","understanding","reply","image_understanding_private_enabled");
  add("video","视频理解","understanding","reply","video_understanding_enabled");
  add("debounce","连续消息合并","decision","reply","private_debounce_enabled");
  cfg("relay","消息转达","action","relay");
  cfg("qzone","QQ 空间","action","qzone");
  add("screen","屏幕读取","perception","access","role_enabled",{api:"screen_access_api"});
  add("phone","手机读取","perception mobile","access","role_enabled",{api:"reality_access_api"});
  cfg("screen-interpretation","屏幕后台解读","perception","screen","interpretation_enabled");
  const aliases={calendar:"state",garden:"learning",desk:"diary",bed:"diary",sleep:"diary",screen:"perception",reality:"perception",content:"output",image:"output",wardrobe:"output"};
  const escape = value => String(value??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const instances=new Map();
  function endpoint(def,role) {
    return def.source==="config" ? (def.global?PREFIX+"config":role?.config_api)
      : def.source==="semantic"?PREFIX+"semantic"
      : def.source==="memory"?role?.memory_api&&role.memory_api+"/settings"
      : def.source==="access"?role?.[def.api]:role?.[def.source+"_api"];
  }
  const query=def=>def.source==="config"?{sections:def.section}:def.source==="semantic"?{global:true}:undefined;
  function relative(path) {
    if(typeof path!=="string"||!path.startsWith(PREFIX))throw new Error("module_not_connected");
    return path.slice(PREFIX.length);
  }
  function valueOf(def,data) {
    const value=def.source==="config"?data?.sections?.[def.section]:data;
    const values=def.source==="memory"?value?.values:def.source==="access"?value:value?.settings;
    return {value:values?.[def.field],token:value?.edit_token,revision:value?.revision,
      roleToken:value?.role_edit_token,roleRevision:value?.role_revision,raw:value};
  }
  function commandFor(def,snapshot,enabled,operation) {
    const changes={[def.field]:enabled};
    switch(def.source) {
      case "config":return {suffix:"/configure",body:{section:def.section,operation_key:operation,edit_token:snapshot.token,changes}};
      case "reply":return {suffix:"/configure",body:{operation_key:operation,edit_token:snapshot.token,changes}};
      case "state":return {suffix:"/command",body:{action:"configure_role",operation_key:operation,role_edit_token:snapshot.roleToken,changes}};
      case "memory":return {suffix:"/command",body:{operation_key:operation,expected_edit_token:snapshot.token,changes:{["memory."+def.field]:enabled},reset_to_inherit:[]}};
      case "semantic":return {suffix:"",body:{global:true,action:"configure",revision:snapshot.raw.settings.revision,changes}};
      case "access":return {suffix:"/role-selection",body:{operation_key:operation,expected_revision:snapshot.roleRevision,enabled}};
      default:throw new Error("module_source_invalid");
    }
  }
  function active(s) {return s.root.isConnected&&window.CompanionProduct?.scopeKey()===s.scope;}
  function render(s) {
    if(!active(s))return;
    s.root.innerHTML=`<section class="module-switch-panel" aria-label="模块开关"><header><h3>模块开关</h3><button type="button" data-module-refresh${s.loading||s.busy?' disabled':''}>刷新</button></header><div class="module-switch-grid">${s.defs.map(def=>{
      const item=s.values.get(def.id),known=typeof item?.value==="boolean",on=item?.value===true;
      const parent=def.parent&&s.values.get(def.parent),paused=on&&parent?.value===false;
      const label=known?(on?"已开启":"已关闭"):(s.loading?"读取中":"读取失败");
      return `<label class="module-switch-item${def.locked?' is-locked':''}" data-module-id="${escape(def.id)}"><span class="module-switch-name">${escape(def.label)}</span><span class="module-switch-state">${label}${def.locked?' · 已锁定':paused?' · 总开关已关闭':''}</span><input type="checkbox" role="switch" aria-label="${escape(def.label)}" data-module-toggle="${escape(def.id)}"${on?' checked':''}${!known||s.loading||s.busy||def.locked?' disabled':''}><span class="module-switch-track" aria-hidden="true"></span>${def.locked?`<small>${escape(def.locked)}</small>`:''}${s.errors.get(def.id)?`<small class="module-switch-error" role="alert">${escape(s.errors.get(def.id))}</small>`:''}</label>`;
    }).join("")}</div></section>`;
  }
  async function read(s) {
    if(!active(s)||s.loading)return;
    s.loading=true;render(s);
    const calls=new Map();
    try {
      await Promise.all(s.defs.map(async def=>{
        const path=endpoint(def,s.role),params=query(def),key=JSON.stringify([path,params]);
        try {
          if(!path)throw new Error("module_not_connected");
          if(!calls.has(key))calls.set(key,window.CompanionProduct.request("GET",relative(path),params));
          const data=await calls.get(key),value=valueOf(def,data);
          if(typeof value.value!=="boolean")throw new Error("module_status_unavailable");
          s.values.set(def.id,value);s.errors.delete(def.id);
        }catch(error){s.values.delete(def.id);s.errors.set(def.id,"未能读取状态");}
      }));
    }finally{s.loading=false;render(s);applyLocks(s);}
  }
  async function save(s,id,enabled) {
    const def=s.defs.find(item=>item.id===id),snapshot=s.values.get(id);
    if(!active(s)||!def||def.locked||s.loading||s.busy||typeof snapshot?.value!=="boolean")return;
    const scope=s.scope,path=endpoint(def,s.role);
    const request=commandFor(def,snapshot,enabled,"module:"+crypto.randomUUID());
    s.busy=id;s.errors.delete(id);render(s);
    try {
      await window.CompanionProduct.request("POST",relative(path+request.suffix),request.body);
      if(!active(s)||s.scope!==scope)return;
      s.values.set(id,{...snapshot,value:enabled});
      window.dispatchEvent(new CustomEvent("companion:module-changed",{detail:{id,enabled,scope}}));
    }catch(error){
      if(active(s))s.errors.set(id,/conflict|revision|changed/.test(String(error?.code||error?.message))?"配置已更新，请刷新后重试":"保存失败，请刷新核对");
    }finally{
      s.busy="";
      const error=s.errors.get(id);
      if(active(s)){await read(s);if(error){s.errors.set(id,error);render(s);}}
    }
  }
  function applyLocks(s) {
    if(!active(s))return;
    const host=s.root.parentElement;
    for(const def of s.defs.filter(x=>x.locked)) {
      const selector=def.selector||(def.source==="config"?`[data-config-host="${def.section}"] [data-wb-field="${def.field}"]`:null);
      if(selector)for(const input of host.querySelectorAll(selector)){if(!input.disabled)input.disabled=true;input.title=def.locked;}
    }
  }
  function unmount(root) {
    for(const [element,s] of instances)if(!root||element===root){
      s.abort.abort();s.observer?.disconnect();instances.delete(element);
    }
  }
  function mount(root,page=root?.dataset.moduleSwitches) {
    if(!root)return;
    unmount(root);
    const selected=aliases[page]||page,defs=entries.filter(x=>x.pages.includes(selected));
    if(!defs.length){root.hidden=true;return;}
    const product=window.CompanionProduct;
    const s={root,defs,role:product?.role(),scope:product?.scopeKey(),values:new Map(),errors:new Map(),abort:new AbortController(),loading:false,busy:""};
    instances.set(root,s);root.hidden=false;
    root.addEventListener("change",event=>{const input=event.target.closest("[data-module-toggle]");if(input)save(s,input.dataset.moduleToggle,input.checked);},{signal:s.abort.signal});
    root.addEventListener("click",event=>{if(event.target.closest("[data-module-refresh]"))read(s);},{signal:s.abort.signal});
    s.observer=new MutationObserver(()=>applyLocks(s));
    s.observer.observe(root.parentElement,{childList:true,subtree:true});
    read(s);
  }
  return {mount,unmount,entries,commandFor};
})();
