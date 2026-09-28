"use strict";
const root = document.querySelector("#content");
const esc = v => String(v ?? "").replace(/[&<>\"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const labels = {display_name:"ชื่อที่แสดง",locale:"ภาษา",timezone:"เขตเวลา"};
let page = "overview", info = null, saved = null, draft = null, revision = "", reviewed = false;

async function api(path, body) {
  const response = await fetch(`/api/${path}`, body === undefined ? {} : {method:"POST",headers:{"Content-Type":"application/json","X-Suto-Request":"1"},body:JSON.stringify(body)});
  if (!response.ok) { let message = "ไม่สามารถโหลดข้อมูลได้"; try { message = (await response.json()).error || message; } catch {} throw new Error(message); }
  return response.json();
}
function message(value, error=false) { const el = document.querySelector("#message"); el.textContent = value; el.className = error ? "notice error" : "notice"; el.hidden = !value; }
function date(value) { const d = new Date(value); return value && !Number.isNaN(d.getTime()) ? d.toLocaleString("th-TH",{dateStyle:"medium",timeStyle:"short"}) : "—"; }
function heading(kicker,title,extra="") { return `<div class="section-heading"><div><p class="eyebrow">${kicker}</p><h2>${title}</h2></div>${extra}</div>`; }

function overview() {
  if (!info) return `<section class="card empty">กำลังโหลดข้อมูล...</section>`;
  if (!info.available) return heading("SYSTEM OVERVIEW","ภาพรวมการทำงาน") + (info.error ? `<section class="card empty"><h3>ไม่สามารถอ่านข้อมูลการทำงาน</h3><p>ตรวจสอบฐานข้อมูลของ Suto แล้วโหลดหน้านี้อีกครั้ง</p></section>` : `<section class="card empty"><h3>ยังไม่มีข้อมูลการทำงาน</h3><p>ข้อมูลจะปรากฏเมื่อ Suto สร้างฐานข้อมูลแล้ว</p></section>`);
  const jobs = info.jobs;
  const running = (jobs.queued||0)+(jobs.running||0)+(jobs.waiting_approval||0)+(jobs.waiting_children||0);
  const names = {queued:"รอคิว",running:"กำลังทำงาน",completed:"สำเร็จ",failed:"ล้มเหลว",blocked:"ถูกบล็อก",cancelled:"ยกเลิก",interrupted:"หยุดชะงัก",waiting_approval:"รออนุมัติ",waiting_children:"รองานย่อย"};
  const rows = info.recent_jobs.map(j => `<tr><td><code>${esc(j.id)}</code></td><td>${esc(names[j.status]||j.status)}</td><td>${esc(date(j.created_at))}</td></tr>`).join("") || `<tr><td colspan="3" class="muted">ยังไม่มีงาน</td></tr>`;
  return heading("SYSTEM OVERVIEW","ภาพรวมการทำงาน",`<span class="pill">Worker ${info.worker === "running" ? "กำลังทำงาน" : "ไม่ทำงาน"}</span>`)
    + `<div class="stats"><article><span>งานที่รอหรือกำลังทำ</span><strong>${running}</strong></article><article><span>งานสำเร็จ</span><strong>${jobs.completed||0}</strong></article><article><span>งานล้มเหลว</span><strong>${jobs.failed||0}</strong></article><article><span>Skills</span><strong>${info.skill_count}</strong></article></div>`
    + `<section class="card"><div class="card-heading"><h3>งานล่าสุด</h3><span>8 รายการล่าสุด</span></div><div class="table-wrap"><table><thead><tr><th>รหัสงาน</th><th>สถานะ</th><th>สร้างเมื่อ</th></tr></thead><tbody>${rows}</tbody></table></div></section>`;
}
function profile() {
  return heading("LOCAL PROFILE","โปรไฟล์") + (!saved ? `<section class="card empty">กำลังโหลดข้อมูล...</section>` : `<section class="card form-card"><p class="muted">เริ่ม Suto ใหม่หลังบันทึกเพื่อใช้ค่าที่เปลี่ยน</p><div class="fields"><label>ชื่อที่แสดง<input name="display_name" maxlength="100" required></label><label>ภาษา<input name="locale" maxlength="16" required><small>ตัวอย่าง: th หรือ en</small></label><label>เขตเวลา<input name="timezone" maxlength="100" required><small>ตัวอย่าง: Asia/Bangkok หรือ UTC</small></label></div><div class="actions"><button id="reload" class="secondary">โหลดจากไฟล์ใหม่</button><button id="validate">ตรวจสอบและทบทวน</button><button id="save" class="primary" disabled>บันทึก</button></div><div id="review" class="review" hidden></div></section>`);
}
function skills() {
  if (!info) return `<section class="card empty">กำลังโหลดข้อมูล...</section>`;
  const rows = info.skills.map(s => `<tr><td>${esc(s.name)}</td><td>v${esc(s.current_version)}</td><td>${esc(date(s.updated_at))}</td></tr>`).join("");
  return heading("AUTOMATION LIBRARY","Skills",`<span class="pill">อ่านอย่างเดียว</span>`) + `<section class="card"><div class="card-heading"><h3>Skills ของ Suto</h3><span>${info.available ? info.skill_count+" รายการ" : "ยังไม่มีข้อมูล"}</span></div>${rows ? `<div class="table-wrap"><table><thead><tr><th>ชื่อ</th><th>เวอร์ชัน</th><th>แก้ไขล่าสุด</th></tr></thead><tbody>${rows}</tbody></table></div>` : `<p class="empty-message">${info.available ? "ยังไม่มี Skills" : info.error ? "ไม่สามารถอ่านฐานข้อมูล" : "ยังไม่มีข้อมูลการทำงาน"}</p>`}${info.skill_count>info.skills.length ? `<p class="muted footnote">แสดง 100 รายการแรก</p>` : ""}</section>`;
}
function mcp() { return heading("INTEGRATIONS","MCP",`<span class="pill">อ่านอย่างเดียว</span>`) + `<section class="card empty"><h3>ยังไม่รองรับ MCP</h3><p>Suto ยังไม่มีการเชื่อมต่อ MCP ในระบบปัจจุบัน</p></section>`; }

function render() {
  document.querySelectorAll("[data-page]").forEach(b => { const selected = b.dataset.page === page; b.classList.toggle("selected",selected); b.setAttribute("aria-current",selected ? "page" : "false"); });
  document.querySelector("#view").innerHTML = ({overview,profile,skills,mcp})[page]();
  message("");
  if (page === "profile" && saved) bindProfile();
}
function bindProfile() {
  document.querySelectorAll(".fields input").forEach(input => { input.value = draft[input.name]; input.oninput = () => { draft[input.name]=input.value; reviewed=false; document.querySelector("#save").disabled=true; document.querySelector("#review").hidden=true; message(""); }; });
  document.querySelector("#reload").onclick = async () => { if (JSON.stringify(draft)!==JSON.stringify(saved) && !confirm("ทิ้งข้อมูลที่แก้ไขและโหลดจากไฟล์ใหม่?")) return; try { await loadProfile(); render(); } catch(e) { message(e.message,true); } };
  document.querySelector("#validate").onclick = async () => { const checked={...draft}; try { const result = await api("profile/validate",{profile:checked,revision}); if (page!=="profile" || Object.keys(labels).some(k=>draft[k]!==checked[k])) return; reviewed=true; draft=result.profile; document.querySelectorAll(".fields input").forEach(input=>{input.value=draft[input.name];}); const changed=Object.keys(labels).filter(k=>saved[k]!==draft[k]); const review=document.querySelector("#review"); review.innerHTML=`<h3>ทบทวนการเปลี่ยนแปลง</h3>${changed.length ? `<ul>${changed.map(k=>`<li><strong>${labels[k]}</strong>: ${esc(saved[k])} → ${esc(draft[k])}</li>`).join("")}</ul>` : `<p>ไม่มีค่าที่เปลี่ยนแปลง</p>`}`; review.hidden=false; document.querySelector("#save").disabled=!changed.length; message(""); } catch(e) { reviewed=false; message(e.message,true); } };
  document.querySelector("#save").onclick = async () => { if (!reviewed) return; const submission={...draft}; document.querySelectorAll(".fields input, .actions button").forEach(el=>{el.disabled=true;}); try { const result=await api("profile/save",{profile:submission,revision}); saved=result.profile; draft={...saved}; revision=result.revision; reviewed=false; render(); message("บันทึกแล้ว เริ่ม Suto ใหม่เพื่อใช้ค่าที่เปลี่ยน"); } catch(e) { document.querySelectorAll(".fields input, .actions button").forEach(el=>{el.disabled=false;}); message(e.message,true); } };
}
async function loadProfile() { const result=await api("profile"); saved=result.profile; draft={...saved}; revision=result.revision; reviewed=false; }
async function loadOverview() { try { info=await api("overview"); } catch { info={available:false,error:true,worker:"unknown",jobs:{},recent_jobs:[],skills:[],skill_count:0}; } if (page==="overview"||page==="skills") render(); }
async function start() {
  const token=new URLSearchParams(location.hash.slice(1)).get("token");
  if (token) { history.replaceState(null,"","/"); await api("auth",{token}); }
  root.innerHTML=`<div class="shell"><aside class="sidebar"><div class="brand"><span class="mark">S</span><div><strong>SUTO</strong><small>CONTROL CENTER</small></div></div><nav aria-label="เมนูหลัก"><button data-page="overview">ภาพรวม</button><button data-page="profile">โปรไฟล์</button><button data-page="skills">Skills</button><button data-page="mcp">MCP</button></nav><p class="sidebar-foot">LOCAL SETTINGS · 127.0.0.1</p></aside><main class="main"><header class="topbar"><div><p class="eyebrow">SUTO / SETTINGS</p><h1>ศูนย์จัดการ Suto</h1></div><span class="local-badge">LOCAL ONLY</span></header><div id="message" role="status" aria-live="polite" hidden></div><div id="view"></div></main></div>`;
  document.querySelectorAll("[data-page]").forEach(b=>b.onclick=()=>{page=b.dataset.page;render();});
  render();
  try { await loadProfile(); if (page==="profile") render(); } catch(e) { message(e.message,true); }
  await loadOverview();
}
start().catch(e=>{ root.textContent=e.message; });
