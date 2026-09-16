"use strict";

const content = document.querySelector("#content");
const titles = {dashboard:"Dashboard", sessions:"Sessions", models:"Models", logs:"Logs", mcp:"MCP", settings:"Settings", system:"System"};
let current = "", generation = 0, poll = null, draft = null, loadedYaml = "", revision = "", validated = false;
const escape = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const date = value => value ? new Date(value).toLocaleString([], {dateStyle:"medium", timeStyle:"short"}) : "—";
const el = id => document.getElementById(id);
const dirty = () => draft !== null && draft !== loadedYaml;

async function api(path, body) {
  const response = await fetch(`/api/${path}`, body === undefined ? {} : {
    method:"POST", headers:{"Content-Type":"application/json", "X-Suto-Request":"1"}, body:JSON.stringify(body),
  });
  if (!response.ok) {
    if (response.status === 401) throw new Error("Sign in using the private dashboard link printed in your terminal.");
    let message = "The request failed. Try again.";
    try { message = (await response.json()).error || message; } catch {}
    throw new Error(message);
  }
  el("connection").textContent = "Local connection";
  el("connection").className = "connection connected";
  return response.json();
}
function toast(message) { el("toast").textContent=message; el("toast").hidden=false; setTimeout(()=>el("toast").hidden=true,6000); }
function heading(name,description,action="") {
  return `<div class="heading"><div><p class="overline">WORKSPACE / ${escape(name.toUpperCase())}</p><h1>${escape(name)}</h1><p class="description">${escape(description)}</p></div>${action}</div>`;
}
function empty(title,description) {return `<div class="empty"><div class="empty-symbol">[ · ]</div><h3>${escape(title)}</h3><p>${escape(description)}</p></div>`;}
function facts(rows) {return `<dl class="facts">${rows.map(([key,value])=>`<div><dt>${escape(key)}</dt><dd>${escape(value)}</dd></div>`).join("")}</dl>`;}
function sessionRows(rows) {return rows.map(row=>`<a class="row" href="#sessions/${encodeURIComponent(row.id)}"><span class="row-icon">▤</span><div><span class="row-title">${escape(row.title || "Untitled session")}</span><div class="row-detail">${escape(row.display_name)} · CLI · ${escape(date(row.updated_at))}</div></div><span class="row-side">${row.message_count} msgs ↗</span></a>`).join("");}
function pager(offset, more, size) {return `<div class="pager"><button id="previous" ${offset===0?"disabled":""}>← Previous</button><span>Page ${Math.floor(offset/size)+1}</span><button id="next" ${!more?"disabled":""}>Next →</button></div>`;}
function pageError(error) {return `<div class="error" role="alert">${escape(error.message)}</div>`;}

async function dashboard(ticket) {
  const data=await api("overview"); if(ticket!==generation)return;
  content.innerHTML=heading("Dashboard","A clear view of your local workspace.",'<button id="refresh" class="subtle">↻ Refresh</button>')+
    (data.database==="not_created"?'<div class="warning">No local history yet. Start Suto in the CLI to create your workspace.</div>':"")+
    `<section class="stats" aria-label="Workspace summary">${[["Saved sessions",data.sessions,"Local CLI history","▤"],["Open tasks",data.tasks,"Personal task list","↗"],["Scheduled reminders",data.reminders,"Awaiting delivery","◷"]].map(([label,note,sub,icon])=>`<div class="stat"><div class="stat-label">${label}<span class="stat-symbol">${icon}</span></div><strong>${note}</strong><small>${sub}</small></div>`).join("")}</section>
    <div class="columns"><section class="panel"><div class="panel-head"><h2>Recent sessions</h2><a href="#sessions">View all ↗</a></div>${data.recent.length?sessionRows(data.recent):empty("No sessions yet","Your local CLI conversations will appear here.")}</section>
    <div><section class="panel"><div class="panel-head"><h2>Upcoming reminders</h2><span class="caption">LOCAL</span></div>${data.reminder_items.length?data.reminder_items.map(item=>`<div class="row"><div><span class="row-title">${escape(item.title)}</span><div class="row-detail">${escape(date(item.remind_at))} · browser time</div></div></div>`).join(""):empty("Nothing scheduled","Reminders created by your local assistant appear here.")}</section>
    <section class="panel"><div class="panel-head"><h2>Workspace</h2><a href="#settings">Settings ↗</a></div><div class="panel-body">${facts([["Database",data.database==="available"?"Available":"Not created"],["Session scope","Local CLI owner"],["Access","This machine"]])}</div></section></div></div>`;
  el("refresh").onclick=()=>navigate(true);
}

async function sessions(ticket,id) {
  if(id) {
    let offset=0;
    async function load() {
      const data=await api(`sessions/${encodeURIComponent(id)}/messages?offset=${offset}`);if(ticket!==generation)return;
      content.innerHTML=heading("Conversation",id,'<a class="button subtle" href="#sessions">← All sessions</a>')+
        `<div class="messages">${data.items.length?[...data.items].reverse().map(m=>`<article class="message ${m.role=== "assistant"?"assistant":"user"}"><div class="message-head"><span>${m.role==="assistant"?"SUTO":"YOU"}</span><time>${escape(date(m.created_at))}</time></div><div class="message-body">${escape(m.content)}${m.truncated?"\n[Long message truncated]":""}</div></article>`).join(""):empty("No messages","This session has no saved messages.")}${pager(offset,data.more,30)}<p class="caption">Latest messages first by page. Timestamps use your browser timezone.</p></div>`;
      el("previous").onclick=()=>{offset-=30;load().catch(showError);};
      el("next").onclick=()=>{offset+=30;load().catch(showError);};
    }
    await load();return;
  }
  content.innerHTML=heading("Sessions","Browse saved conversations belonging to the local CLI user.")+
    `<form id="search-form" class="toolbar"><input type="search" id="search" maxlength="200" aria-label="Search sessions" placeholder="Search conversations…"><button>Search</button><span class="caption">CLI history only</span></form><div id="results" aria-live="polite"></div>`;
  let offset=0, query="", sequence=0;
  async function load() {
    const seq=++sequence;
    const data=await api(`sessions?q=${encodeURIComponent(query)}&offset=${offset}`);if(ticket!==generation || seq!==sequence)return;
    el("results").innerHTML=`<section class="panel">${data.items.length?`<div class="table-scroll"><table><thead><tr><th>Conversation</th><th>Owner / channel</th><th>Messages</th><th>Last activity</th></tr></thead><tbody>${data.items.map(r=>`<tr><td><a class="session-title" href="#sessions/${encodeURIComponent(r.id)}">${escape(r.title||"Untitled session")}</a><span class="caption mono">${escape(r.id)}</span></td><td>${escape(r.display_name)}<div class="row-detail">CLI</div></td><td class="mono">${r.message_count}</td><td class="muted">${escape(date(r.updated_at))}</td></tr>`).join("")}</tbody></table></div>`:empty("No conversations found",query?"Try a different search.":"Start a conversation in the CLI to see it here.")}</section>${pager(offset,data.more,30)}`;
    el("previous").onclick=()=>{offset-=30;load().catch(showError);};el("next").onclick=()=>{offset+=30;load().catch(showError);};
  }
  el("search-form").onsubmit=event=>{event.preventDefault();query=el("search").value;offset=0;load().catch(showError);};
  await load();
}

async function models(ticket) {
  const data=await api("models");if(ticket!==generation)return;
  content.innerHTML=heading("Models","The model configuration loaded by this dashboard.")+
    `<section class="panel"><div class="panel-head"><h2>Configured model</h2><span class="badge">READ ONLY</span></div><div class="panel-body">${facts([["Provider",data.provider],["Model",data.model],["API credential",data.credential_configured?"Configured":"Not configured"],["Connection","Not checked"]])}</div></section><p class="note">Model settings currently come from the host environment and .env. Restart the relevant Suto process after changing them. Model discovery and switching are not available yet.</p>`;
}

async function logs(ticket) {
  content.innerHTML=heading("Logs","Stored host job events. Details are redacted before display.")+
    `<form class="toolbar" id="log-filter"><input type="search" id="search" maxlength="200" aria-label="Search logs" placeholder="Search detail or job ID…"><label for="event">Event</label><select id="event"><option value="">All events</option>${["queued","running","completed","failed","blocked","cancelled","interrupted","waiting_approval"].map(x=>`<option>${x}</option>`).join("")}</select><button>Filter</button><button type="button" id="live">Auto-refresh: off</button></form><div id="log-error"></div><div id="results"></div><p class="note">Historical host events, including parked developer jobs. Newest first; timestamps use your browser timezone. This view does not run jobs.</p>`;
  let offset=0, query="", event="", live=false, busy=false;
  async function load() {
    if(busy)return;busy=true;
    try {
      const data=await api(`logs?q=${encodeURIComponent(query)}&event=${encodeURIComponent(event)}&offset=${offset}`);if(ticket!==generation)return;
      el("log-error").innerHTML="";
      el("results").innerHTML=`<section class="panel">${data.items.length?`<div class="table-scroll"><table><thead><tr><th>Time</th><th>Event</th><th>Source</th><th>Detail</th></tr></thead><tbody>${data.items.map(r=>`<tr><td class="mono muted">${escape(date(r.created_at))}</td><td><span class="badge">${escape(r.event_type)}</span></td><td class="mono muted">${escape(r.job_id||"Host")}</td><td class="log-detail">${escape(r.detail||"—")}</td></tr>`).join("")}</tbody></table></div>`:empty("No log entries",query||event?"Try another filter.":"Stored job events will appear here when available.")}</section>${pager(offset,data.more,50)}`;
      el("previous").onclick=()=>{offset-=50;load();};el("next").onclick=()=>{offset+=50;load();};
    } catch(error) {if(ticket===generation)el("log-error").innerHTML=pageError(error);} finally {busy=false;}
  }
  el("log-filter").onsubmit=e=>{e.preventDefault();offset=0;query=el("search").value;event=el("event").value;load();};
  el("live").onclick=()=>{live=!live;el("live").textContent=`Auto-refresh: ${live?"on":"off"}`;clearInterval(poll);if(live)poll=setInterval(()=>{if(offset===0)load();},5000);};
  await load();
}

async function settings(ticket) {
  const data=await api("settings");if(ticket!==generation)return;
  loadedYaml=data.yaml;revision=data.revision;draft=data.yaml;validated=false;
  content.innerHTML=heading("Settings","Edit the workspace configuration. Changes apply after restarting Suto.",'<span class="badge">HOST SETTINGS</span>')+
    `<div class="settings-layout"><div><div id="settings-error"></div><section class="panel"><div class="editor-header"><span class="mono">config.yaml</span><span class="caption" id="edit-status">Saved</span></div><div class="editor"><pre class="line-numbers" id="line-numbers" aria-hidden="true"></pre><textarea id="yaml" aria-label="Configuration YAML" spellcheck="false" wrap="off"></textarea></div><div class="editor-footer"><span>YAML · UTF-8</span><span>Profile configuration · version 1</span></div></section><div class="actions"><button id="reload" class="subtle">Reload file</button><button id="validate">Validate & review</button><button id="save" class="primary" disabled>Save changes</button></div><section class="panel diff" id="diff-panel" hidden><div class="panel-head"><h2>Review changes</h2><span class="caption">SAVED → DRAFT</span></div><pre id="diff"></pre></section></div>
    <aside class="settings-help"><div><h3>Profile</h3><p><code>display_name</code><br>Your local display name.<br><code>timezone</code><br>An IANA timezone, such as Asia/Bangkok.<br><code>locale</code><br>Your reply language, such as th or en.</p></div><div><h3>Saving</h3><p>Review the changes before saving. Unsupported fields are rejected. Formatting is normalized; comments are not retained.</p><h3>Models & credentials</h3><p>Managed in your host environment. This file stores profile settings only.</p></div></aside></div>`;
  el("yaml").value=draft;
  function lineNumbers(){el("line-numbers").textContent=draft.split("\n").map((_,i)=>i+1).join("\n");}
  function changed(){draft=el("yaml").value;validated=false;el("save").disabled=true;el("diff-panel").hidden=true;el("edit-status").textContent=dirty()?"Unsaved changes":"Saved";lineNumbers();}
  lineNumbers();el("yaml").oninput=changed;
  el("yaml").onkeydown=e=>{if(e.key==="Tab"){e.preventDefault();const area=e.target;area.setRangeText("  ",area.selectionStart,area.selectionEnd,"end");changed();}};
  el("reload").onclick=()=>{if(!dirty()||confirm("Discard your draft and reload config.yaml?")){draft=null;navigate(true);}};
  async function update(save) {
    const source=draft;
    el("settings-error").innerHTML="";
    for(const id of ["validate","save","reload"])el(id).disabled=true;
    try {
      const result=await api(`settings/${save?"save":"validate"}`,{yaml:source,revision});if(ticket!==generation)return;
      if(save){loadedYaml=result.yaml;revision=result.revision;validated=false;if(draft===source){draft=result.yaml;el("yaml").value=draft;lineNumbers();}el("edit-status").textContent=dirty()?"Unsaved changes":"Saved";el("diff-panel").hidden=true;toast("Settings saved. Restart Suto to apply changes.");}
      else if(draft===source){validated=true;el("diff").textContent=result.diff||"No changes to saved values.";el("diff-panel").hidden=false;el("edit-status").textContent="Validated";}
    }catch(error){if(ticket===generation){el("settings-error").innerHTML=pageError(error);validated=false;}}
    finally{if(ticket===generation){el("validate").disabled=false;el("reload").disabled=false;el("save").disabled=!validated||!dirty();}}
  }
  el("validate").onclick=()=>update(false);el("save").onclick=()=>{if(validated)update(true);};
}

async function system(ticket) {
  const data=await api("system");if(ticket!==generation)return;
  content.innerHTML=heading("System","Installation details and available maintenance controls.")+
    `<div class="columns"><section class="panel"><div class="panel-head"><h2>Suto installation</h2></div><div class="panel-body">${facts([["Version",data.version],["Access",data.transport],["Update status","Not checked"]])}</div></section><section class="panel"><div class="panel-head"><h2>Updates</h2><span class="badge unavailable">UNAVAILABLE</span></div><div class="panel-body"><p class="note">Automatic updates are not configured for this installation.</p><button disabled>Update Suto</button></div></section></div>`;
}

function showError(error){content.querySelector(".page-error")?.remove();const box=document.createElement("div");box.className="page-error";box.innerHTML=pageError(error);content.prepend(box);el("connection").textContent="Request failed";el("connection").className="connection";}
async function navigate(force=false) {
  const route=location.hash.slice(1)||"dashboard";
  if(route===current&&!force)return;
  if(dirty() && !confirm("Discard unsaved settings and leave this page?")){history.replaceState(null,"",`#${current}`);return;}
  draft=null;current=route;clearInterval(poll);
  const [page,id]=route.split("/");const title=titles[page]||"Dashboard";const ticket=++generation;
  document.title=`${title} · Suto`;el("breadcrumb").textContent=title;
  document.querySelectorAll("[data-page]").forEach(a=>{if(a.dataset.page===(titles[page]?page:"dashboard"))a.setAttribute("aria-current","page");else a.removeAttribute("aria-current");});
  content.innerHTML='<div class="loading" role="status">Loading workspace…</div>';
  try {
    if(page==="sessions")await sessions(ticket,id);
    else if(page==="models")await models(ticket);
    else if(page==="logs")await logs(ticket);
    else if(page==="settings")await settings(ticket);
    else if(page==="system")await system(ticket);
    else if(page==="mcp")content.innerHTML=heading("MCP","Connections to external tools and services.")+`<section class="panel"><div class="panel-head"><h2>MCP servers</h2><span class="badge unavailable">NOT AVAILABLE YET</span></div>${empty("No MCP integration","Server connections and tool discovery will appear here when supported.")}</section>`;
    else await dashboard(ticket);
  } catch(error){if(ticket===generation){content.innerHTML=heading(title,"Workspace data could not be loaded.");showError(error);}}
}
window.addEventListener("beforeunload",e=>{if(dirty()){e.preventDefault();e.returnValue="";}});
window.addEventListener("hashchange",()=>navigate());
(async()=>{
  const token=new URLSearchParams(location.hash.slice(1)).get("token");
  if(token){history.replaceState(null,"","#dashboard");try{await api("auth",{token});}catch(error){showError(error);return;}}
  await navigate(true);
})();
