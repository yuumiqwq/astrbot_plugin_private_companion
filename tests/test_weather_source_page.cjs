"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.resolve(__dirname, "../pages/陪伴小屋/weather-source-page.js"), "utf8");
const flush = () => new Promise(resolve => setImmediate(resolve));

function harness(options = {}) {
  const listeners = {}, confirmations = [], posts = [];
  const status = {
    settings: {
      provider: "auto", api_host: "", refresh_minutes: 30,
      jwt_credential_id: "", jwt_project_id: "", jwt_developer_id: "", has_jwt_private_key: false,
      ...(options.settings || {}),
    },
    actual_provider: "open-meteo", edit_token: "a".repeat(64), has_credential: false,
    credential_kind: "", credential_source: "none", source_state: "ready",
    ...(options.status || {}),
  };
  const bridge = {
    apiGet: async () => status,
    apiPost: async (_endpoint, body) => {
      posts.push(body);
      if (options.postError) throw options.postError;
      return options.postStatus || status;
    },
  };
  const window = {
    CompanionProduct: {bridgeNow: () => bridge, bridgeFor: async () => bridge, scopeKey: () => "role-scope"},
    CompanionConfirm: request => new Promise(resolve => confirmations.push({request, resolve})),
    addEventListener() {},
  };
  const root = {
    isConnected: true, innerHTML: "",
    addEventListener(type, callback) { listeners[type] = callback; },
    removeEventListener(type) { delete listeners[type]; },
    dispatch(type, event) { return listeners[type]?.(event); },
  };
  vm.runInNewContext(source, {window, Promise, Object, String, Number, Error, AbortController, setTimeout, clearTimeout,
    document:{dispatchEvent(){}}, CustomEvent:class {constructor(type){this.type=type;}}});
  return {window, root, confirmations, posts, status, page: window.CompanionWeatherSourcePage};
}

const submit = root => root.dispatch("submit", {
  target: {matches: selector => selector === "[data-weather-source-form]"}, preventDefault() {},
});

test("weather draft confirmation is asynchronous and cancellation preserves the draft", async () => {
  const {page, root, confirmations} = harness();
  page.mount(root);
  await flush();
  root.dispatch("input", {target: {name: "credential", value: "draft-secret"}});

  const canceled = page.canLeave();
  assert.equal(typeof canceled.then, "function");
  assert.equal(confirmations[0].request.title, "天气数据源尚未保存");
  assert.equal(confirmations[0].request.danger, true);
  confirmations[0].resolve(false);
  assert.equal(await canceled, false);
  const accepted = page.canLeave();
  assert.equal(confirmations.length, 2);

  confirmations[1].resolve(true);
  assert.equal(await accepted, true);
  page.unmount();
});

test("an API Key configuration shows one credential field and hides the optional JWT group", async () => {
  const {page, root} = harness({
    settings: {provider: "qweather", api_host: "abc123.re.qweatherapi.com", refresh_minutes: 5},
    status: {actual_provider: "qweather", has_credential: true,
      credential_kind: "api_key", credential_source: "plugin"},
  });
  page.mount(root);
  await flush();

  assert.match(root.innerHTML, /value="credential" checked/);
  assert.match(root.innerHTML, /<div class="weather-source-auth-panel" >[\s\S]*?id="weather-source-credential"/);
  assert.match(root.innerHTML, /页面配置 · API Key 已配置/);
  assert.match(root.innerHTML, /<div class="weather-source-auth-panel" hidden>[\s\S]*?id="weather-source-jwt-kid"/);
  page.unmount();
});

test("switching authentication mode reveals only the fields for that mode", async () => {
  const {page, root} = harness();
  page.mount(root);
  await flush();

  root.dispatch("change", {target: {name: "auth_mode", value: "signed_jwt"}});
  assert.match(root.innerHTML, /name="auth_mode" value="signed_jwt" checked/);
  assert.match(root.innerHTML, /<div class="weather-source-auth-panel" >[\s\S]*?id="weather-source-jwt-kid"/);
  assert.match(root.innerHTML, /id="weather-source-jwt-key"/);
  assert.match(root.innerHTML, /<div class="weather-source-auth-panel" hidden>[\s\S]*?id="weather-source-credential"/);

  root.dispatch("change", {target: {name: "auth_mode", value: "credential"}});
  assert.match(root.innerHTML, /name="auth_mode" value="credential" checked/);
  assert.match(root.innerHTML, /<div class="weather-source-auth-panel" >[\s\S]*?id="weather-source-credential"/);
  assert.match(root.innerHTML, /<div class="weather-source-auth-panel" hidden>[\s\S]*?id="weather-source-jwt-kid"/);
  page.unmount();
});

test("switching from API Key to signed JWT requires a complete credential group", async () => {
  const {page, root, posts} = harness({
    settings: {provider: "qweather", api_host: "abc123.re.qweatherapi.com", refresh_minutes: 5},
    status: {actual_provider: "qweather", has_credential: true,
      credential_kind: "api_key", credential_source: "plugin"},
  });
  page.mount(root);
  await flush();

  root.dispatch("change", {target: {name: "auth_mode", value: "signed_jwt"}});
  submit(root);
  await flush();
  assert.equal(posts.length, 0);
  assert.match(root.innerHTML, /私钥自动签发需要同时填写 JWT 凭据 ID/);

  for (const [name, value] of [["jwt_credential_id", "kid-1"], ["jwt_project_id", "project-1"],
    ["jwt_private_key", "-----BEGIN PRIVATE KEY-----"]]) {
    root.dispatch("input", {target: {name, value}});
  }
  submit(root);
  await flush();
  assert.equal(posts.length, 1);
  assert.equal(posts[0].changes.credential, undefined);
  assert.equal(posts[0].changes.jwt_credential_id, "kid-1");
  assert.equal(posts[0].changes.jwt_project_id, "project-1");
  assert.equal(posts[0].changes.jwt_private_key, "-----BEGIN PRIVATE KEY-----");
  page.unmount();
});

test("saving a non-credential setting does not resend or clear the saved API Key", async () => {
  const {page, root, posts} = harness({
    settings: {provider: "qweather", api_host: "abc123.re.qweatherapi.com", refresh_minutes: 5},
    status: {actual_provider: "qweather", has_credential: true,
      credential_kind: "api_key", credential_source: "plugin"},
  });
  page.mount(root);
  await flush();

  root.dispatch("input", {target: {name: "refresh_minutes", value: "10"}});
  submit(root);
  await flush();
  assert.equal(posts.length, 1);
  assert.deepEqual(Object.keys(posts[0].changes), ["refresh_minutes"]);
  assert.equal(posts[0].changes.refresh_minutes, 10);
  assert.equal(posts[0].changes.credential, undefined);
  page.unmount();
});
test("switching from signed JWT to API Key requires a replacement credential", async () => {
  const {page, root, posts} = harness({
    settings: {provider: "qweather", api_host: "abc123.re.qweatherapi.com", refresh_minutes: 5,
      jwt_credential_id: "kid-1", jwt_project_id: "project-1", has_jwt_private_key: true},
    status: {actual_provider: "qweather", has_credential: true,
      credential_kind: "signed_jwt", credential_source: "plugin"},
  });
  page.mount(root);
  await flush();

  root.dispatch("change", {target: {name: "auth_mode", value: "credential"}});
  submit(root);
  await flush();
  assert.equal(posts.length, 0);
  assert.match(root.innerHTML, /请填写新的和风凭据后再保存/);

  root.dispatch("input", {target: {name: "credential", value: "new-api-key"}});
  submit(root);
  await flush();
  assert.equal(posts.length, 1);
  assert.deepEqual(Object.keys(posts[0].changes), ["credential"]);
  assert.equal(posts[0].changes.credential, "new-api-key");
  assert.equal(posts[0].changes.jwt_credential_id, undefined);
  page.unmount();
});
test("a saved signed JWT cannot be saved with a cleared required field", async () => {
  const {page, root, posts} = harness({
    settings: {provider: "qweather", api_host: "abc123.re.qweatherapi.com", refresh_minutes: 5,
      jwt_credential_id: "kid-1", jwt_project_id: "project-1", has_jwt_private_key: true},
    status: {actual_provider: "qweather", has_credential: true,
      credential_kind: "signed_jwt", credential_source: "plugin"},
  });
  page.mount(root);
  await flush();

  root.dispatch("input", {target: {name: "jwt_credential_id", value: ""}});
  submit(root);
  await flush();
  assert.equal(posts.length, 0);
  assert.match(root.innerHTML, /私钥自动签发需要同时填写 JWT 凭据 ID/);
  page.unmount();
});

test("a server rejection passed through the AstrBot bridge retains editable credentials", async () => {
  const {page, root, posts} = harness({postError:new Error("weather_source_invalid")});
  page.mount(root);
  await flush();
  root.dispatch("input", {target:{name:"api_host",value:"abc123.re.qweatherapi.com"}});
  root.dispatch("input", {target:{name:"credential",value:"draft-weather-credential"}});
  submit(root);
  await flush();
  assert.equal(posts.length,1);
  assert.match(root.innerHTML,/请按当前认证方式核对/);
  assert.doesNotMatch(root.innerHTML,/保存结果尚未确认/);
  assert.match(root.innerHTML,/value="draft-weather-credential"/);
  assert.doesNotMatch(root.innerHTML,/<button[^>]*type="submit"[^>]*disabled/);
  submit(root);
  await flush();
  assert.equal(posts.length,2);
  page.unmount();
});

test("a bridge conflict is confirmed but still requires a fresh edit token", async () => {
  const {page, root} = harness({postError:new Error("host_options_conflict")});
  page.mount(root);
  await flush();
  root.dispatch("input", {target:{name:"refresh_minutes",value:"10"}});
  submit(root);
  await flush();
  assert.match(root.innerHTML,/数据源已被其他页面修改/);
  assert.doesNotMatch(root.innerHTML,/保存结果尚未确认/);
  assert.match(root.innerHTML,/<button[^>]*type="submit"[^>]*disabled/);
  page.unmount();
});

test("a transport failure remains an unconfirmed write", async () => {
  const {page, root} = harness({postError:new Error("bridge_timeout")});
  page.mount(root);
  await flush();
  root.dispatch("input", {target:{name:"credential",value:"draft-weather-credential"}});
  submit(root);
  await flush();
  assert.match(root.innerHTML,/保存结果尚未确认/);
  assert.doesNotMatch(root.innerHTML,/value="draft-weather-credential"/);
  assert.match(root.innerHTML,/<button[^>]*type="submit"[^>]*disabled/);
  page.unmount();
});
