"use strict";

const content = document.querySelector("#content");
const escape = value => String(value ?? "").replace(/[&<>\"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
let draft = "", loadedYaml = "", revision = "", validated = false;

async function api(path, body) {
  const response = await fetch(`/api/${path}`, body === undefined ? {} : {
    method: "POST", headers: {"Content-Type":"application/json", "X-Suto-Request":"1"}, body: JSON.stringify(body),
  });
  if (!response.ok) {
    let message = "The request failed. Try again.";
    try { message = (await response.json()).error || message; } catch {}
    throw new Error(message);
  }
  return response.json();
}

function renderError(error) {
  document.querySelector("#settings-error").innerHTML = `<p class="error" role="alert">${escape(error.message)}</p>`;
}

function render(data) {
  loadedYaml = data.yaml;
  draft = data.yaml;
  revision = data.revision;
  content.innerHTML = `<section class="shell"><header><span class="mark">s.</span><div><p class="eyebrow">LOCAL SETTINGS</p><h1>Suto Settings</h1><p>Edit your local profile. Restart Suto after saving.</p></div></header>
    <div id="settings-error"></div><section class="panel"><div class="panel-head"><code>config.yaml</code><span id="status">Saved</span></div><textarea id="yaml" aria-label="Configuration YAML" spellcheck="false" wrap="off"></textarea></section>
    <div class="actions"><button id="reload" class="secondary">Reload file</button><button id="validate">Validate & review</button><button id="save" class="primary" disabled>Save changes</button></div>
    <section class="panel diff" id="diff-panel" hidden><h2>Review changes</h2><pre id="diff"></pre></section>
    <aside><h2>Profile fields</h2><p><code>display_name</code>, <code>timezone</code> (for example <code>Asia/Bangkok</code>), and <code>locale</code> (such as <code>th</code> or <code>en</code>).</p><p>Secrets and model credentials stay in <code>.env</code>.</p></aside></section>`;
  const yaml = document.querySelector("#yaml");
  const changed = () => {
    draft = yaml.value;
    validated = false;
    document.querySelector("#save").disabled = true;
    document.querySelector("#diff-panel").hidden = true;
    document.querySelector("#status").textContent = draft === loadedYaml ? "Saved" : "Unsaved changes";
  };
  yaml.value = draft;
  yaml.oninput = changed;
  yaml.onkeydown = event => {
    if (event.key === "Tab") { event.preventDefault(); yaml.setRangeText("  ", yaml.selectionStart, yaml.selectionEnd, "end"); changed(); }
  };
  document.querySelector("#reload").onclick = () => {
    if (draft === loadedYaml || confirm("Discard your draft and reload config.yaml?")) load();
  };
  async function update(save) {
    document.querySelector("#settings-error").innerHTML = "";
    try {
      const result = await api(`settings/${save ? "save" : "validate"}`, {yaml: draft, revision});
      if (save) { render(result); document.querySelector("#toast").textContent = "Settings saved. Restart Suto to apply changes."; document.querySelector("#toast").hidden = false; }
      else { validated = true; document.querySelector("#diff").textContent = result.diff || "No changes to saved values."; document.querySelector("#diff-panel").hidden = false; document.querySelector("#save").disabled = draft === loadedYaml; document.querySelector("#status").textContent = "Validated"; }
    } catch (error) { renderError(error); }
  }
  document.querySelector("#validate").onclick = () => update(false);
  document.querySelector("#save").onclick = () => { if (validated) update(true); };
}

async function load() {
  try { render(await api("settings")); } catch (error) { content.innerHTML = `<p class="error">${escape(error.message)}</p>`; }
}

(async () => {
  const token = new URLSearchParams(location.hash.slice(1)).get("token");
  if (token) { history.replaceState(null, "", "/"); await api("auth", {token}); }
  await load();
})();
