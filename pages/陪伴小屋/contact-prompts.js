/* Editable proactive prompt sources, scoped to the selected role or window. */
"use strict";
window.CompanionContactPrompts = (() => {
  const PREFIX = "/astrbot_plugin_private_companion/";
  const KIND = "proactive.compose";
  const TASK = "task_prompt";
  const scopes = new Map();
  const messages = {
    configuration_revision_conflict:"配置已更新，草稿已保留。请核对当前内容。",
    configuration_value_invalid:"提示词未通过校验，请核对长度与格式变量。",
    model_configuration_invalid:"提示词未通过校验，请核对长度与格式变量。",
    model_prompt_template_invalid:"提示词格式变量无效。",
    model_prompt_section_readonly:"当前范围不支持编辑这个提示片段。",
    model_task_unavailable:"主动联系模型尚未就绪。",
    model_request_invalid:"请求未通过校验，请重新读取。",
    idempotency_key_conflict:"原保存请求已变化，请重新读取核对。",
    core_access_required:"当前账号没有编辑权限。",
    dashboard_identity_required:"请从已登录的 AstrBot 管理后台打开。",
    staging_not_initialized:"当前角色尚未就绪。",
    staging_profile_conflict:"当前角色映射已变化，请重新读取。",
    staging_host_terminated:"插件已停止，请重新读取。",
  };
  const e = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const sourceLabel = source => ({default:"插件默认",installation:"全局设置",role:"角色默认",window:"当前窗口",relationship:"联系偏好",presentation:"表达设置"}[source] || "上级设置");
  let root = null, state = null, options = null, version = 0;
  function current(ticket, s) { return state === s && root?.isConnected; }
  function sectionOf(s, key) {
    if (key === TASK) return s.task && {key:TASK,label:"模型任务提示",condition:"每次主动联系",editable:true,
      prompt:s.task.fields?.prompt?.value ?? "", default_prompt:s.task.default_prompt ?? "", variables:[],
      field:s.task.fields?.prompt,edit_token:s.task.edit_token,configuration_layer:s.layer};
    return s.task?.prompt_sections?.find(item => item.key === key);
  }
  function baseline(section) { return JSON.stringify([section.prompt ?? "", section.field?.explicit === true, section.field?.source || "default"]); }
  function draftOf(s, section) {
    if (!s.drafts.has(section.key)) s.drafts.set(section.key, {text:section.prompt ?? "",base:baseline(section),
      token:section.edit_token,dirty:false,reset:false,conflict:false,pending:null,error:"",saved:false});
    return s.drafts.get(section.key);
  }
  function acceptTask(s, task) {
    s.task = task;
    s.currentMode = task.prompt_assembly?.current_mode === "simple" ? "simple" : "companion";
    for (const [key, draft] of s.drafts) {
      const section = sectionOf(s, key);
      if (!section || draft.pending) continue;
      const changed = draft.base !== baseline(section);
      const matches = draft.reset ? section.field?.explicit !== true : draft.text === section.prompt;
      if (draft.dirty && changed && !matches) draft.conflict = true;
      if (!draft.dirty) {
        draft.text = section.prompt ?? ""; draft.base = baseline(section); draft.reset = false;
      }
      if (!draft.conflict) { draft.token = section.edit_token; draft.base = baseline(section); }
    }
    if (state === s) options?.onModeChange?.(s.currentMode);
  }
  async function api(method, path, body) {
    let timer;
    try {
      const product = window.CompanionProduct;
      const bridge = product.bridgeNow?.() || await product.bridgeFor();
      const response = await Promise.race([method === "GET" ? bridge.apiGet(path,body) : bridge.apiPost(path,body),
        new Promise((_,reject) => { timer = setTimeout(() => reject(new Error("bridge_timeout")),30000); })]);
      const wrapped = ["ok","error"].includes(response?.status) && Object.hasOwn(response,"data") ? response
        : ["ok","error"].includes(response?.data?.status) && Object.hasOwn(response.data,"data") ? response.data : null;
      if (wrapped?.status === "error") {
        const code = wrapped.data?.code || wrapped.message || "invalid_response";
        throw Object.assign(new Error(code),{code,confirmed:true});
      }
      const value = wrapped ? wrapped.data : response;
      if (!value || typeof value !== "object") throw new Error("invalid_response");
      return value;
    } catch (error) {
      const code = error?.code || error?.message;
      if (Object.hasOwn(messages,code)) error.confirmed = true;
      throw error;
    } finally { clearTimeout(timer); }
  }
  async function load(s = state) {
    if (!s || s.loading || !s.path) return;
    const ticket = version;
    s.loading = true; s.error = "";
    render();
    try {
      const result = await api("GET",s.path,{layer:s.layer});
      const task = result.tasks?.find(item => item.kind === KIND);
      if (!task || !Array.isArray(task.prompt_sections)) throw new Error("invalid_response");
      acceptTask(s,task);
    } catch (error) {
      s.error = messages[error.code || error.message] || "提示词读取失败，请重试。";
    } finally { s.loading = false; if (current(ticket,s)) render(); }
  }
  function operationKey() {
    return "contact-prompt:" + (window.crypto?.randomUUID?.() || Date.now().toString(36) + ":" + Math.random().toString(36).slice(2));
  }
  async function save(key) {
    const s = state, section = sectionOf(s,key);
    if (!section?.editable || s.busy || s.loading || s.modePending) return;
    const draft = draftOf(s,section);
    if (!draft.pending && (!draft.dirty || draft.conflict)) return;
    const payload = draft.pending || {kind:KIND,...(key === TASK ? {} : {section:key}),layer:s.layer,
      operation_key:operationKey(),edit_token:draft.token,
      changes:draft.reset ? {} : {prompt:draft.text},reset:draft.reset ? ["prompt"] : []};
    const ticket = version;
    s.busy = key; draft.error = ""; draft.saved = false; draft.pending = payload;
    render();
    try {
      const result = await api("POST",s.path + "/configure",payload);
      if (!result.task || result.task.kind !== KIND) throw new Error("invalid_response");
      draft.pending = null; draft.dirty = false; draft.conflict = false; draft.saved = true;
      acceptTask(s,result.task);
    } catch (error) {
      const code = error.code || error.message;
      if (error.confirmed) {
        draft.pending = null;
        draft.error = messages[code] || "保存失败，草稿已保留。";
        if (code === "configuration_revision_conflict") {
          draft.conflict = true;
          await load(s);
        }
      } else draft.error = "保存结果尚未确认，请核对原保存。";
    } finally { s.busy = ""; if (current(ticket,s)) render(); }
  }
  function promptMode(s = state) {
    return (s?.currentMode || s?.task?.prompt_assembly?.current_mode) === "simple" ? "simple" : "companion";
  }
  async function changeMode(mode) {
    const s = state, field = s?.task?.prompt_assembly?.mode;
    if (!s || s.busy || s.loading || !field?.edit_token || !["companion","simple"].includes(mode)) return;
    if (!s.modePending && mode === promptMode(s)) return;
    const payload = s.modePending || {kind:KIND,section:"mode",layer:s.layer,
      operation_key:operationKey(),edit_token:field.edit_token,changes:{mode},reset:[]};
    const ticket = version;
    s.busy = "@mode"; s.modePending = payload; s.modeError = "";
    render();
    try {
      s.modeRequest = api("POST",s.path + "/configure",payload);
      const result = await s.modeRequest;
      if (!result.task || !["companion","simple"].includes(result.task.prompt_assembly?.current_mode)) throw new Error("invalid_response");
      s.modePending = null;
      acceptTask(s,result.task);
    } catch (error) {
      const code = error.code || error.message;
      if (error.confirmed) {
        s.modePending = null;
        s.modeError = messages[code] || "模式切换失败，提示词草稿已保留。";
        if (code === "configuration_revision_conflict") await load(s);
      } else s.modeError = "模式切换结果尚未确认，请核对原操作。";
    } finally { s.modeRequest = null; s.busy = ""; if (current(ticket,s)) render(); }
  }
  async function confirmMode() {
    const s = state;
    if (!s?.path) return null;
    if (s.modeRequest) await s.modeRequest.catch(() => {});
    if (s.modePending || s.busy === "@mode") throw Object.assign(new Error("contact_prompt_mode_unconfirmed"),
      {code:"contact_prompt_mode_unconfirmed",confirmed:true});
    const result = await api("GET",s.path,{layer:s.layer});
    const task = result.tasks?.find(item => item.kind === KIND);
    if (!task || !["simple","companion"].includes(task.prompt_assembly?.current_mode)) throw new Error("invalid_response");
    if (state !== s) throw Object.assign(new Error("contact_recipient_changed"),{code:"contact_recipient_changed",confirmed:true});
    acceptTask(s,task);
    render();
    return promptMode(s);
  }
  function selectedPurpose() {
    const variant = state.task?.prompt_assembly?.diagnostic_variants?.[state.selected];
    if (variant?.purpose) return variant.purpose;
    return state.selected === "mobile_location" ? "environment" : state.selected === "reaction_image" ? "chat" : state.selected;
  }
  function primaryKeys() {
    const assembly = state.task?.prompt_assembly;
    const purpose = selectedPurpose();
    const result = [assembly?.purpose_sections?.[purpose] || "purpose." + purpose];
    const variant = assembly?.diagnostic_variants?.[state.selected];
    if (variant?.sections) result.push(...variant.sections);
    else if (state.selected === "reaction_image") result.push("diagnostic_image");
    else if (state.selected === "mobile_location") result.push("origin.reality_location_event");
    return [...new Set(result)];
  }
  function editor(key) {
    const section = sectionOf(state,key);
    if (!section) return "";
    const draft = draftOf(state,section);
    const locked = !!state.busy || !!state.modePending || !!draft.pending || state.loading || !section.editable;
    const source = section.field?.source;
    return `<section class="ct-prompt-field${draft.dirty ? " is-dirty" : ""}" data-prompt-editor="${e(key)}">
      <header><label for="ct-prompt-${e(key)}">${e(section.label)}</label><span data-prompt-state>${draft.pending ? "待核对保存" : draft.conflict ? "版本冲突" : draft.dirty ? "未保存" : draft.saved ? "已保存" : sourceLabel(source)}</span></header>
      ${section.condition ? `<p class="ct-prompt-condition">${e(section.condition)}</p>` : ""}
      <textarea id="ct-prompt-${e(key)}" data-prompt-text="${e(key)}" rows="${key.startsWith("purpose.") ? 7 : 5}" maxlength="8000" spellcheck="false"${locked ? " disabled" : ""}>${e(draft.text)}</textarea>
      ${section.variables?.length ? `<div class="ct-prompt-variables">格式变量 ${section.variables.map(name => `<code>${e(key === "simple" ? "{{" + name + "}}" : "{" + name + "}")}</code>`).join(" ")}</div>` : ""}
      ${draft.error ? `<p class="ct-prompt-error" role="alert">${e(draft.error)}</p>` : ""}
      ${draft.conflict ? `<details class="ct-prompt-current"><summary>当前保存内容</summary><pre>${e(section.prompt)}</pre></details>` : ""}
      <footer>
        <button type="button" class="ct-btn" data-prompt-action="default" data-prompt-key="${e(key)}"${locked ? " disabled" : ""}>恢复默认</button>
        <button type="button" class="ct-btn" data-prompt-action="inherit" data-prompt-key="${e(key)}"${locked ? " disabled" : ""}>跟随上级</button>
        <button type="button" class="ct-btn" data-prompt-action="discard" data-prompt-key="${e(key)}"${locked || !draft.dirty ? " disabled" : ""}>放弃修改</button>
        ${draft.conflict ? `<button type="button" class="ct-btn" data-prompt-action="rebase" data-prompt-key="${e(key)}"${locked ? " disabled" : ""}>保留草稿继续编辑</button>` : ""}
        <button type="button" class="ct-btn is-primary" data-prompt-action="save" data-prompt-key="${e(key)}"${state.busy || state.modePending || state.loading || !section.editable || (!draft.pending && (!draft.dirty || draft.conflict)) ? " disabled" : ""}>${state.busy === key ? "保存中…" : draft.pending ? "核对保存" : "保存"}</button>
      </footer>
    </section>`;
  }
  function group(id,label,keys) {
    const existing = [...new Set(keys)].filter(key => sectionOf(state,key));
    if (!existing.length) return "";
    if (id === "conditional" && !existing.includes(state.conditionKey)) state.conditionKey = existing[0];
    const content = id === "conditional"
      ? `<label class="ct-prompt-section-selector">条件片段<select data-prompt-section aria-label="选择条件提示">${existing.map(key => `<option value="${e(key)}"${state.conditionKey === key ? " selected" : ""}>${e(sectionOf(state,key).label)}${state.drafts.get(key)?.dirty ? " · 未保存" : ""}</option>`).join("")}</select></label>${editor(state.conditionKey)}`
      : existing.map(editor).join("");
    return `<details class="ct-prompt-group" data-prompt-group="${id}"${state.open.has(id) ? " open" : ""}><summary>${label}<span>${existing.length}</span></summary><div>${content}</div></details>`;
  }
  function render() {
    if (!root || !state) return;
    const assembly = state.task?.prompt_assembly;
    const mode = promptMode();
    const primary = primaryKeys();
    const common = [...(assembly?.common_sections || ["business","expression","history","output","terminal"]),TASK];
    const excluded = new Set([...primary,...common]);
    const conditional = (assembly?.conditional_sections || state.task?.prompt_sections?.map(item => item.key) || [])
      .filter(key => !excluded.has(key) && !key.startsWith("purpose.") && !["simple","simple_output","simple_terminal"].includes(key));
    const order = mode === "simple" ? (assembly?.simple_order || [])
      .filter(item => !["output_contract","terminal_prompt_guard"].includes(item.key)) : (assembly?.companion_order || assembly?.order || []);
    root.innerHTML = `<section class="ct-prompt-panel" aria-label="主动消息提示词">
      <header class="ct-prompt-header"><h3>主动消息提示词</h3><span class="ct-prompt-draft-count">${[...state.drafts.values()].filter(d => d.dirty).length} 项未保存</span></header>
      <div class="ct-prompt-modes" role="group" aria-label="主动提示模式">${[["companion","陪伴插件"],["simple","简洁"]].map(([key,label]) => `<button type="button" data-prompt-mode="${key}" aria-pressed="${mode === key}"${state.busy || state.modePending || state.loading || !assembly?.mode?.edit_token ? " disabled" : ""}>${label}</button>`).join("")}</div>
      ${state.modeError ? `<div class="ct-prompt-mode-error" role="alert"><p class="ct-prompt-error">${e(state.modeError)}</p>${state.modePending ? '<button type="button" class="ct-btn" data-prompt-action="retry-mode">核对切换</button>' : ""}</div>` : ""}
      <div class="ct-prompt-controls">
        ${mode === "companion" ? `<label>测试入口<select data-prompt-entry aria-label="选择提示词入口">${(options.items || []).map(item => `<option value="${e(item.id)}"${state.selected === item.id ? " selected" : ""}>${e(item.label)}</option>`).join("")}</select></label>` : ""}
        <label>生效范围<select data-prompt-layer aria-label="提示词生效范围"${state.busy ? " disabled" : ""}><option value="window"${state.layer === "window" ? " selected" : ""}>当前窗口</option><option value="role"${state.layer === "role" ? " selected" : ""}>角色默认</option></select></label>
        <button type="button" class="ct-btn" data-prompt-action="refresh"${state.loading || state.busy ? " disabled" : ""}>${state.loading ? "读取中…" : "重新读取"}</button>
      </div>
      ${state.error ? `<p class="ct-prompt-error" role="alert">${e(state.error)}</p>` : ""}
      ${state.task ? `${mode === "simple" ? `<div class="ct-prompt-primary ct-prompt-simple">${editor("simple")}</div>` : `<div class="ct-prompt-primary">${primary.map(editor).join("")}</div>
        ${group("common","共用提示",common)}${group("conditional","条件提示",conditional)}`}
        <details class="ct-prompt-group" data-prompt-group="assembly"${state.open.has("assembly") ? " open" : ""}><summary>组装顺序</summary>
          <ol class="ct-prompt-order">${order.map(item => `<li><strong>${e(item.label)}</strong>${item.sections?.length ? `<span>${item.sections.filter(key => !key.startsWith("purpose.") || key === primary[0]).map(key => e(sectionOf(state,key)?.label || key)).join(" · ")}</span>` : ""}</li>`).join("")}</ol>
        </details>` : ""}
    </section>`;
  }
  function updateStatus(key) {
    const draft = state.drafts.get(key);
    const container = root.querySelector(`[data-prompt-editor="${key}"]`);
    container?.classList.toggle("is-dirty",draft.dirty);
    const badge = container?.querySelector("[data-prompt-state]");
    if (badge) badge.textContent = draft.conflict ? "版本冲突" : draft.dirty ? "未保存" : "已保存";
    for (const action of ["save","discard"]) {
      const button = container?.querySelector(`[data-prompt-action="${action}"]`);
      if (button) button.disabled = !!state.busy || !!state.modePending || state.loading || !draft.dirty || (action === "save" && draft.conflict);
    }
    const count = root.querySelector(".ct-prompt-draft-count");
    if (count) count.textContent = [...state.drafts.values()].filter(d => d.dirty).length + " 项未保存";
  }
  function input(event) {
    const field = event.target.closest("[data-prompt-text]");
    if (!field || !state) return;
    const section = sectionOf(state,field.dataset.promptText);
    if (!section?.editable || state.busy || state.modePending) return;
    const draft = draftOf(state,section);
    if (draft.pending) return;
    draft.text = field.value; draft.reset = false; draft.dirty = draft.text !== section.prompt;
    draft.error = ""; draft.saved = false;
    updateStatus(section.key);
  }
  function select(id) {
    if (!state || !(options.items || []).some(item => item.id === id)) return;
    state.selected = id;
    options.onSelect?.(id);
    render();
  }
  function changed(event) {
    const section = event.target.closest("[data-prompt-section]");
    if (section && sectionOf(state,section.value)) { state.conditionKey = section.value; render(); return; }
    const entry = event.target.closest("[data-prompt-entry]");
    if (entry) { select(entry.value); return; }
    const layer = event.target.closest("[data-prompt-layer]");
    if (layer && ["role","window"].includes(layer.value) && !state.busy) {
      const previous = state;
      const newState = scopeState(previous.scope,previous.path,layer.value);
      newState.selected = previous.selected;
      state = newState; render(); void load();
    }
  }
  function clicked(event) {
    const mode = event.target.closest("[data-prompt-mode]");
    if (mode) { void changeMode(mode.dataset.promptMode); return; }
    const button = event.target.closest("[data-prompt-action]");
    if (!button || !state) return;
    const action = button.dataset.promptAction;
    if (action === "retry-mode") { void changeMode(state.modePending?.changes?.mode); return; }
    if (action === "refresh") { void load(); return; }
    const key = button.dataset.promptKey, section = sectionOf(state,key);
    if (!section) return;
    const draft = draftOf(state,section);
    if (action === "save") { void save(key); return; }
    if (state.busy || state.modePending || draft.pending) return;
    if (action === "discard") state.drafts.delete(key);
    if (action === "default") { draft.text = section.default_prompt ?? ""; draft.reset = false; draft.dirty = true; draft.saved = false; }
    if (action === "inherit") { draft.text = section.field?.inherited_value ?? section.default_prompt ?? ""; draft.reset = true; draft.dirty = true; draft.saved = false; }
    if (action === "rebase") { draft.token = section.edit_token; draft.base = baseline(section); draft.conflict = false; draft.error = ""; }
    render();
  }
  function toggled(event) {
    const id = event.target.dataset?.promptGroup;
    if (!id || !state) return;
    if (event.target.open) state.open.add(id); else state.open.delete(id);
  }
  function scopeState(scope,path,layer) {
    const key = JSON.stringify([scope,path,layer]);
    if (!scopes.has(key)) scopes.set(key,{scope,path,layer,task:null,drafts:new Map(),open:new Set(),
      selected:"chat",conditionKey:"",modePending:null,modeError:"",loading:false,busy:"",error:""});
    return scopes.get(key);
  }
  function unmount() {
    ++version;
    if (root) {
      root.removeEventListener("click",clicked); root.removeEventListener("input",input);
      root.removeEventListener("change",changed); root.removeEventListener("toggle",toggled,true);
    }
    root = null; state = null; options = null;
  }
  function mount(element,config = {}) {
    const previousLayer = state?.layer || "window";
    unmount(); root = element; options = config;
    if (!root) return;
    const product = window.CompanionProduct, endpoint = product?.role?.()?.model_api;
    if (typeof endpoint !== "string" || !endpoint.startsWith(PREFIX) || /[?#%\\:]/.test(endpoint)
        || endpoint.split("/").slice(1).some(part => !part || part === "." || part === "..")) {
      root.innerHTML = '<p class="ct-prompt-error">请选择已关联角色的私聊窗口。</p>'; return;
    }
    const identity = product.identity?.() || {};
    const scope = JSON.stringify([identity.installation_id,identity.operator_id]);
    state = scopeState(scope,endpoint.slice(PREFIX.length),previousLayer);
    if (["simple","companion"].includes(config.mode)) state.currentMode = config.mode;
    if (config.selected && (config.items || []).some(item => item.id === config.selected)) state.selected = config.selected;
    root.addEventListener("click",clicked); root.addEventListener("input",input);
    root.addEventListener("change",changed); root.addEventListener("toggle",toggled,true);
    render(); if (!state.task && !state.loading) void load();
  }
  return {mount,unmount,select,confirmMode,get state(){return state;}};
})();
