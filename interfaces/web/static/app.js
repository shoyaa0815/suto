"use strict";
const root = document.querySelector("#content");
const fields = ["display_name", "locale", "timezone"];
let saved = null, draft = null, revision = "", saving = false;
let activeView = "settings", selectedConversation = null;
let conversations = [], conversationOffset = null, conversationsLoaded = false, conversationsLoading = false, conversationsError = "";
let messages = [], messageAfter = null, messagesLoaded = false, messagesLoading = false, messagesError = "", messageRequest = 0;

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
      <label><span>Display name</span><input name="display_name" maxlength="100" required autocomplete="name"></label>
      <label><span>Language</span><input name="locale" minlength="2" maxlength="16" required></label>
      <label><span>Time zone</span><input name="timezone" maxlength="100" required></label>
    </div>
    <div class="actions"><button id="reload" type="button">Reload</button><button id="save" class="primary" type="button" disabled>Save</button></div>`}
  </section>`;
}
function hasChanges() {
  return saved && fields.some(key => draft[key] !== saved[key]);
}
function updateSaveState() {
  const button = document.querySelector("#save");
  if (button) button.disabled = saving || !hasChanges();
}
function render() {
  document.querySelectorAll(".sidebar nav button").forEach(button => {
    if (button.dataset.view === activeView) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  if (activeView === "settings") {
    document.querySelector("#view").innerHTML = settingsView();
    if (saved) bindSettings();
  } else {
    renderDashboard();
  }
  notice("");
}
function formatDate(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}
function renderDashboard() {
  const view = document.querySelector("#view");
  view.innerHTML = `<section class="settings dashboard" aria-labelledby="dashboard-heading">
    <h1 id="dashboard-heading">Dashboard</h1>
    <div class="section-heading"><h2>Conversations</h2></div>
    <div id="conversations-content"></div>
  </section>`;
  const content = document.querySelector("#conversations-content");
  if (selectedConversation) {
    const back = document.createElement("button");
    back.type = "button";
    back.className = "back-button";
    back.textContent = "← All conversations";
    back.onclick = () => { selectedConversation = null; render(); };
    content.append(back);
    const title = document.createElement("h3");
    title.className = "conversation-title";
    title.textContent = selectedConversation.preview || "Conversation";
    content.append(title);
    const date = document.createElement("p");
    date.className = "conversation-date";
    date.textContent = formatDate(selectedConversation.created_at);
    content.append(date);
    const thread = document.createElement("div");
    thread.className = "message-list";
    for (const message of messages) {
      const article = document.createElement("article");
      article.className = `message message-${message.role}`;
      const label = document.createElement("strong");
      label.textContent = message.role === "user" ? "You" : "Suto";
      const body = document.createElement("div");
      body.className = "message-content";
      body.textContent = message.content;
      article.append(label, body);
      thread.append(article);
    }
    content.append(thread);
    if (!messagesLoaded && messagesError) content.append(document.createTextNode(messagesError));
    else if (!messagesLoaded) content.append(document.createTextNode("Loading messages…"));
    else if (!messages.length) content.append(document.createTextNode("No chat messages saved."));
    if (messageAfter !== null) {
      const more = document.createElement("button");
      more.type = "button";
      more.className = "load-more";
      more.textContent = messagesLoading ? "Loading…" : "Load more messages";
      more.disabled = messagesLoading;
      more.onclick = () => loadMessages(messageAfter);
      content.append(more);
    }
    return;
  }
  const refresh = document.createElement("button");
  refresh.type = "button";
  refresh.className = "back-button";
  refresh.textContent = "Refresh";
  refresh.disabled = conversationsLoading;
  refresh.onclick = () => loadConversations(0);
  content.append(refresh);
  const list = document.createElement("div");
  list.className = "conversation-list";
  for (const conversation of conversations) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "conversation-item";
    const title = document.createElement("strong");
    title.textContent = conversation.preview || "Conversation";
    const date = document.createElement("span");
    date.textContent = formatDate(conversation.updated_at);
    button.append(title, date);
    button.onclick = () => {
      selectedConversation = conversation;
      messages = []; messageAfter = null; messagesLoaded = false; messagesLoading = false; messagesError = ""; messageRequest++;
      render(); loadMessages(0);
    };
    list.append(button);
  }
  content.append(list);
  if (!conversationsLoaded && conversationsError) content.append(document.createTextNode(conversationsError));
  else if (!conversationsLoaded) content.append(document.createTextNode("Loading conversations…"));
  else if (!conversations.length) content.append(document.createTextNode("No saved conversations yet."));
  if (conversationOffset !== null) {
    const more = document.createElement("button");
    more.type = "button";
    more.className = "load-more";
    more.textContent = conversationsLoading ? "Loading…" : "Load more conversations";
    more.disabled = conversationsLoading;
    more.onclick = () => loadConversations(conversationOffset);
    content.append(more);
  }
}
async function loadConversations(offset=0) {
  if (conversationsLoading) return;
  conversationsLoading = true;
  conversationsError = "";
  try {
    const result = await api(`conversations?offset=${offset}`);
    conversations = offset === 0 ? result.conversations : conversations.concat(result.conversations);
    conversationOffset = result.next_offset;
    conversationsLoaded = true;
    conversationsLoading = false;
    if (activeView === "dashboard" && !selectedConversation) render();
  } catch (error) {
    conversationsError = error.message;
    conversationsLoading = false;
    if (activeView === "dashboard" && !selectedConversation) { render(); notice(error.message, true); }
  } finally {
    conversationsLoading = false;
  }
}
async function loadMessages(after=0) {
  if (messagesLoading || !selectedConversation) return;
  const id = selectedConversation.id;
  const requestId = ++messageRequest;
  messagesLoading = true;
  messagesError = "";
  try {
    const result = await api(`conversations/${encodeURIComponent(id)}/messages?after=${after}`);
    if (!selectedConversation || selectedConversation.id !== id || requestId !== messageRequest) return;
    const scrollTop = after ? document.querySelector(".message-list")?.scrollTop || 0 : 0;
    messages = after === 0 ? result.messages : messages.concat(result.messages);
    messageAfter = result.next_after;
    messagesLoaded = true;
    messagesLoading = false;
    if (activeView === "dashboard") {
      render();
      document.querySelector(".message-list").scrollTop = scrollTop;
    }
  } catch (error) {
    if (activeView === "dashboard" && selectedConversation?.id === id && requestId === messageRequest) {
      messagesError = error.message;
      messagesLoading = false;
      render(); notice(error.message, true);
    }
  } finally {
    if (requestId === messageRequest) messagesLoading = false;
  }
}
function bindSettings() {
  document.querySelectorAll(".fields input").forEach(input => {
    input.value = draft[input.name];
    input.oninput = () => {
      draft[input.name] = input.value;
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
    document.querySelectorAll(".fields input, .actions button").forEach(el => el.disabled = true);
    try {
      const result = await api("profile/save", {profile:{...draft},revision});
      saved = result.profile; draft = {...saved}; revision = result.revision;
      render(); notice("Saved. Restart Suto to use the new settings.");
    } catch (error) {
      document.querySelectorAll(".fields input, .actions button").forEach(el => el.disabled = false);
      notice(error.message, true);
    } finally {
      saving = false;
      updateSaveState();
    }
  };
  updateSaveState();
}
async function loadSettings() {
  const result = await api("profile");
  saved = result.profile; draft = {...saved}; revision = result.revision;
}
async function start() {
  const token = new URLSearchParams(location.hash.slice(1)).get("token");
  if (token) { history.replaceState(null, "", "/"); await api("auth", {token}); }
  root.innerHTML = `<div class="site"><aside class="sidebar"><div class="brand">SUTO</div>
    <nav aria-label="Main navigation"><button type="button" data-view="dashboard">Dashboard</button><button type="button" data-view="settings">Settings</button></nav></aside>
    <main class="main"><div id="message" class="notice" role="status" aria-live="polite" hidden></div><div id="view"></div></main></div>`;
  document.querySelectorAll(".sidebar nav button").forEach(button => {
    button.onclick = async () => {
      activeView = button.dataset.view;
      render();
      if (activeView === "dashboard" && !conversationsLoaded && !conversationsLoading) loadConversations();
      if (activeView === "settings" && !saved) {
        try { await loadSettings(); if (activeView === "settings") render(); }
        catch (error) { if (activeView === "settings") notice(error.message, true); }
      }
    };
  });
  render();
  try { await loadSettings(); if (activeView === "settings") render(); } catch (error) {
    if (activeView === "settings") notice(error.message, true);
  }
}
start().catch(error => { root.textContent = error.message; });
