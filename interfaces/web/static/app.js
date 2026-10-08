"use strict";
const root = document.querySelector("#content");
let saved = null, draft = "", revision = "", saving = false;
async function api(path, body) {
  const response = await fetch(`/api/${path}`, body === undefined ? {} : {
    method:"POST",headers:{"Content-Type":"application/json","X-Suto-Request":"1"},body:JSON.stringify(body)
  });
  if (!response.ok) {
    let error = "Request failed.";
    try { error = (await response.json()).error || error; } catch {}
    throw new Error(error);
  }
  return response.json();
}
function notice(value, error=false) {
  const el = document.querySelector("#message");
  el.textContent = value;
  el.className = error ? "notice error" : "notice";
  el.hidden = !value;
}
function settingsView() {
  return `<section class="settings" aria-labelledby="settings-heading">
    <h1 id="settings-heading">Settings</h1>
    ${!saved ? '<p>Loading…</p>' : `<div class="fields">
      <label><span>Runtime configuration (YAML)</span><textarea name="yaml" rows="18" spellcheck="false"></textarea></label>
      <p>Set provider, model, timezone, workspace and execution limits in the runtime section. Keep secrets in .env. Restart Suto processes after saving.</p>
    </div>
    <div class="actions"><button id="reload" type="button">Reload</button><button id="save" class="primary" type="button" disabled>Save</button></div>`}
  </section>`;
}
function hasChanges() {
  return saved !== null && draft !== saved;
}
function updateSaveState() {
  const button = document.querySelector("#save");
  if (button) button.disabled = saving || !hasChanges();
}
function render() {
  document.querySelector("#view").innerHTML = settingsView();
  if (saved !== null) bindSettings();
  notice("");
}
function bindSettings() {
  document.querySelectorAll(".fields textarea").forEach(input => {
    input.value = draft;
    input.oninput = () => {
      draft = input.value;
      updateSaveState();
      notice("");
    };
  });
  document.querySelector("#reload").onclick = async () => {
    if (hasChanges() && !confirm("Discard your changes and reload the file?")) return;
    try { await loadSettings(); render(); } catch (error) { notice(error.message, true); }
  };
  document.querySelector("#save").onclick = async () => {
    if (saving || !hasChanges()) return;
    saving = true;
    document.querySelectorAll(".fields textarea, .actions button").forEach(el => el.disabled = true);
    try {
      const result = await api("settings/save", {yaml:draft,revision});
      saved = result.yaml; draft = saved; revision = result.revision;
      render(); notice("Saved. Restart Suto to use the new settings.");
    } catch (error) {
      document.querySelectorAll(".fields textarea, .actions button").forEach(el => el.disabled = false);
      notice(error.message, true);
    } finally {
      saving = false;
      updateSaveState();
    }
  };
  updateSaveState();
}
async function loadSettings() {
  const result = await api("settings");
  saved = result.yaml; draft = saved; revision = result.revision;
}
async function start() {
  const token = new URLSearchParams(location.hash.slice(1)).get("token");
  if (token) { history.replaceState(null, "", "/"); await api("auth", {token}); }
  root.innerHTML = `<div class="site"><aside class="sidebar"><div class="brand">SUTO</div>
    <nav aria-label="Main navigation"><button type="button" aria-current="page">Settings</button></nav></aside>
    <main class="main"><div id="message" class="notice" role="status" aria-live="polite" hidden></div><div id="view"></div></main></div>`;
  render();
  try { await loadSettings(); render(); } catch (error) {
    notice(error.message, true);
  }
}
start().catch(error => { root.textContent = error.message; });
