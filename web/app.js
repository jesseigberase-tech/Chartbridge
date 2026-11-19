// ChartBridge dashboard. Plain JS, no build step.
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const api = async (path, opts = {}) => {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.detail || r.statusText);
  return body;
};

let selected = null;
let sourceLabels = {};
let status = {};
let liveSource = null;

function labelFor(src) {
  if (src.kind === "fhir") return "Clinic EMR";
  if (src.kind === "emr_export") return src.system.split(" (")[0];
  if (/fax/i.test(src.description)) return src.description.split(" ")[0] + " fax";
  return "Lab portal";
}

// ---------------------------------------------------------------- schedule
async function loadSchedule() {
  const appts = await api("/api/schedule");
  $("#appts").innerHTML = appts.map((a) => `
    <li class="appt ${a.appointment_id === selected ? "sel" : ""}" data-id="${a.appointment_id}">
      <div class="row1"><span class="time">${esc(a.time)}</span><span class="muted" style="font-size:12px">${a.length_min} min</span></div>
      <div class="name">${esc(a.patient_name)}</div>
      <div class="reason">${esc(a.reason)}</div>
      <div class="tags">
        <span class="tag">${a.sources} record sources</span>
        ${a.running ? '<span class="tag run">Preparing…</span>' : a.prepared ? '<span class="tag ready">Brief ready</span>' : ""}
        ${a.high_flags ? `<span class="tag high">${a.high_flags} high-priority</span>` : ""}
        ${a.pending_actions ? `<span class="tag pend">${a.pending_actions} to approve</span>` : ""}
      </div>
    </li>`).join("");
  document.querySelectorAll(".appt").forEach((el) => el.addEventListener("click", () => select(el.dataset.id)));
  const outbox = await api("/api/outbox");
  $("#outbox").innerHTML = outbox.length
    ? outbox.slice().reverse().map((m) => `<li><div class="to">To ${esc(m.to)}</div>${esc(m.message)}</li>`).join("")
    : '<li class="muted">Empty</li>';
  return appts;
}

async function select(id) {
  selected = id;
  $("#emptyState").hidden = true;
  $("#patientView").hidden = false;
  await loadSchedule();
  const data = await api(`/api/briefs/${id}`);
  const a = data.appointment;
  sourceLabels = Object.fromEntries(data.sources.map((s) => [s.source_id, labelFor(s)]));
  $("#pTime").textContent = `${a.time} · ${a.length_min} min`;
  $("#pName").textContent = a.patient_name;
  $("#pSub").textContent = `DOB ${a.dob} · ${a.reason}`;
  $("#prepBtn").textContent = data.brief ? "Re-run agent" : "Prepare chart";
  const me = (await api("/api/schedule")).find((x) => x.appointment_id === id);
  $("#replayBtn").hidden = !me?.has_recording;
  if (!liveSource) { $("#feed").innerHTML = ""; setActivity("idle", "Agent activity", ""); $("#activity").open = false; }
  renderBrief(data);
}

// ---------------------------------------------------------------- run agent
function setActivity(state, title, stats) {
  $("#actDot").className = "dot " + ({ live: "live", ok: "ok", bad: "bad" }[state] || "");
  $("#actTitle").textContent = title;
  $("#actStats").textContent = stats || "";
}

const TOOL_TEXT = {
  get_appointment: () => "Looked up the appointment",
  list_record_sources: (i) => `Searched every connected system for ${i.patient_id}'s records`,
  fetch_source: (i) => `Opening ${sourceLabels[i.source_id] || i.source_id}`,
  get_open_slots: (i) => `Checking open ${i.slot_type === "lab_draw" ? "lab" : "appointment"} slots`,
  submit_brief: (i) => `Submitting brief (${i.flags} flags, ${i.medications} meds) to the checker`,
  propose_action: (i) => `Proposing: ${i.title}`,
};

function addFeed(html, cls = "") {
  const li = document.createElement("li");
  li.className = cls;
  li.innerHTML = html;
  $("#feed").appendChild(li);
  $("#feed").scrollTop = $("#feed").scrollHeight;
}

async function startRun(kind) {
  const id = selected;
  $("#prepBtn").disabled = $("#replayBtn").disabled = true;
  $("#feed").innerHTML = "";
  $("#activity").open = true;
  $("#briefArea").innerHTML = '<div class="placeholder">The agent is working. The brief appears here when every claim has passed the citation check.</div>';
  setActivity("live", kind === "replay" ? "Replaying recorded run" : "Agent working", "");
  let run;
  try {
    run = await api(`/api/${kind === "replay" ? "replay" : "prep"}/${id}`, { method: "POST" });
  } catch (e) {
    setActivity("bad", "Could not start", "");
    $("#briefArea").innerHTML = `<div class="error">${esc(e.message)}</div>`;
    $("#prepBtn").disabled = $("#replayBtn").disabled = false;
    return;
  }
  loadSchedule();
  let steps = 0, checks = 0;
  const t0 = Date.now();
  const tick = setInterval(() => $("#actStats").textContent = `${steps} tool calls · ${((Date.now() - t0) / 1000).toFixed(0)}s`, 500);
  liveSource = new EventSource(`/api/runs/${run.run_id}/stream`);
  liveSource.onmessage = async (msg) => {
    const ev = JSON.parse(msg.data);
    if (ev.type === "start") addFeed(`<span class="ic">▶</span><span class="txt">Started on ${esc(ev.model)}${ev.replay ? " · <b>replay of a recorded run</b>" : ""}</span>`);
    if (ev.type === "thought") addFeed(`<span class="ic">…</span><span class="txt">${esc(ev.text)}</span>`, "thought");
    if (ev.type === "tool_call") { steps++; const f = TOOL_TEXT[ev.tool]; addFeed(`<span class="ic">${steps}</span><span><span class="txt">${esc(f ? f(ev.input) : ev.tool)}</span><div class="res" data-step="${ev.step}-${ev.tool}"></div></span>`); }
    if (ev.type === "tool_result") {
      const cells = document.querySelectorAll(`.res[data-step="${ev.step}-${ev.tool}"]`);
      const cell = [...cells].find((c) => !c.textContent);
      if (cell) { cell.textContent = ev.summary; if (!ev.ok) cell.closest("li").classList.add("bad"); }
    }
    if (ev.type === "validation") {
      checks++;
      if (ev.ok) addFeed(`<span class="ic">✓</span><span class="txt"><b>Checks passed.</b> Every claim traced to an exact location in a source, and nothing in the structured records left out.</span>`, "check-ok");
      else addFeed(`<span class="ic">!</span><span class="txt"><b>Checker caught ${ev.errors.length} problem(s).</b> Sent back to the agent to fix:<ul>${ev.errors.slice(0, 5).map((e) => `<li>${esc(Object.entries(sourceLabels).reduce((t, [k, v]) => t.replaceAll(k, v), e))}</li>`).join("")}</ul></span>`, "check-bad");
    }
    if (ev.type === "injection") addFeed(`<span class="ic">⚠</span><span class="txt"><b>Security:</b> ${esc(sourceLabels[ev.source_id] || ev.source_id)} contains text trying to instruct the AI. Treated as data, not followed, and reported to the physician.</span>`, "inject");
    if (ev.type === "action") loadSchedule();
    if (ev.type === "error") addFeed(`<span class="ic">✕</span><span class="txt">${esc(ev.message)}</span>`, "bad");
    if (ev.type === "done") {
      liveSource.close(); liveSource = null; clearInterval(tick);
      const tok = ev.usage ? ` · ${(ev.usage.input_tokens + ev.usage.output_tokens).toLocaleString()} tokens` : "";
      setActivity(ev.ok ? "ok" : "bad", ev.ok ? "Brief ready" : "Run did not finish", `${steps} tool calls · ${checks} checks${ev.seconds ? ` · ${ev.seconds}s` : ""}${tok}`);
      $("#prepBtn").disabled = $("#replayBtn").disabled = false;
      if (selected === id) { const data = await api(`/api/briefs/${id}`); renderBrief(data); $("#prepBtn").textContent = "Re-run agent"; }
      loadSchedule();
    }
  };
  liveSource.onerror = () => {};
}

// ---------------------------------------------------------------- brief
const cites = (cs) => `<span class="cites">${(cs || []).map((c) =>
  `<button class="cite" data-src="${esc(c.source_id)}" data-loc="${esc(c.locator)}" title="${esc(c.quote)}">${esc(sourceLabels[c.source_id] || c.source_id)} · ${esc(c.locator.replace(/^(MedicationStatement|AllergyIntolerance|Observation|Condition|ServiceRequest|Patient|Encounter)\//, ""))}</button>`).join("")}</span>`;

function renderBrief({ brief, actions }) {
  if (!brief) {
    $("#printBtn").hidden = true;
    $("#briefArea").innerHTML = `<div class="placeholder">No brief yet. Press <b>Prepare chart</b> and the agent will gather and reconcile ${Object.keys(sourceLabels).length} record sources.</div>`;
    return;
  }
  const order = { high: 0, medium: 1, low: 2 };
  const flags = [...brief.flags].sort((a, b) => order[a.severity] - order[b.severity]);
  const meds = brief.medications.map((m) => `<li><div class="item-top"><b>${esc(m.name)}</b><span class="status ${esc(m.status)}">${esc(m.status)}</span></div><div class="item-sub">${esc(m.dose || "")} ${cites(m.citations)}</div></li>`).join("");
  const alg = brief.allergies.map((a) => `<li><div class="item-top"><b>${esc(a.agent)}</b><span class="item-sub">${esc(a.reaction || "")}</span></div>${cites(a.citations)}</li>`).join("") || '<li class="muted">None recorded</li>';
  const cond = brief.conditions.map((c) => `<li><div class="item-top"><b>${esc(c.name)}</b><span class="item-sub">${esc(c.status || "")}</span></div>${cites(c.citations)}</li>`).join("");
  const res = brief.recent_results.map((r) => `<li><div class="item-top"><b>${esc(r.test)}</b><span>${esc(r.value)}</span><span class="item-sub">${esc(r.date || "")}</span></div>${r.trend ? `<div class="item-sub">${esc(r.trend)}</div>` : ""}${cites(r.citations)}</li>`).join("");

  const warnings = Object.entries(brief.security_warnings || {});
  const security = warnings.length ? `<div class="security"><span class="shield">⚠</span><div><b>Suspicious instructions found in ${warnings.map(([sid]) => esc(sourceLabels[sid] || sid)).join(", ")}.</b>
    Someone wrote text aimed at the AI inside a record. ChartBridge did not follow it and still flagged everything it found.
    ${warnings.flatMap(([sid, hits]) => hits.map((h) => `<div class="q">${esc(h.text)}</div>`)).join("")}</div></div>` : "";
  $("#printBtn").hidden = false;
  $("#briefArea").innerHTML = security + `
    <div class="summary">${esc(brief.one_line_summary)}<span class="verified">✓ ${countCites(brief)} citations verified against source records · generated ${esc(brief.generated?.replace("T", " "))}</span></div>
    <div class="grid">
      <div class="card span2"><h2>Review before you walk in <span class="muted">${flags.length}</span></h2>
        ${flags.map((f) => `<div class="flag"><span class="sev ${f.severity}">${f.severity}</span><div><div class="cat">${esc(f.category)}</div><div class="title">${esc(f.title)}</div><div class="detail">${esc(f.detail)}</div>${cites(f.citations)}</div></div>`).join("") || '<div class="muted">Nothing flagged.</div>'}
      </div>
      <div class="card span2"><h2>Follow-up the agent prepared <span class="muted">needs your approval</span></h2><div class="actions">${renderActions(actions)}</div></div>
      <div class="card"><h2>Medications (reconciled)</h2><ul class="items">${meds}</ul></div>
      <div class="card"><h2>Allergies &amp; reactions</h2><ul class="items">${alg}</ul></div>
      <div class="card"><h2>Recent results</h2><ul class="items">${res}</ul></div>
      <div class="card"><h2>Conditions</h2><ul class="items">${cond}</ul></div>
      ${brief.questions_for_visit?.length ? `<div class="card"><h2>Worth asking</h2><ul class="qs">${brief.questions_for_visit.map((q) => `<li>${esc(q)}</li>`).join("")}</ul></div>` : ""}
      <div class="card"><h2>Sources reviewed</h2><ul class="srcs">${brief.sources_reviewed.map((s) => `<li><span class="idm ${s.identity_match}">${s.identity_match}</span><span><a data-src="${esc(s.source_id)}">${esc(sourceLabels[s.source_id] || s.source_id)}</a>${s.note ? ` <span class="muted">· ${esc(s.note)}</span>` : ""}</span></li>`).join("")}</ul></div>
    </div>`;
  bindBrief();
}

function countCites(b) {
  return ["medications", "allergies", "conditions", "recent_results", "flags"].reduce((n, k) => n + b[k].reduce((m, x) => m + (x.citations?.length || 0), 0), 0);
}

function renderActions(actions) {
  if (!actions.length) return '<div class="muted">No follow-up proposed.</div>';
  return actions.map((a) => {
    const editable = a.status === "pending" && a.message;
    const slotNote = a.slot_start ? ` · ${esc(a.slot_start.replace("T", " "))}` : "";
    return `<div class="action ${a.status}" data-id="${a.action_id}">
      <div class="a-top"><div><div class="a-type">${esc(a.action_type.replaceAll("_", " "))}${a.recipient ? ` → ${esc(a.recipient)}` : ""}${slotNote}</div><div class="a-title">${esc(a.title)}</div></div>${cites(a.citations)}</div>
      <div class="a-reason">${esc(a.reason)}</div>
      ${editable ? `<textarea>${esc(a.message)}</textarea>` : a.message ? `<div class="item-sub" style="margin-bottom:6px">“${esc(a.message)}”</div>` : ""}
      ${a.status === "pending" ? `<div class="btns"><button class="approve">Approve${a.message ? " & send" : ""}</button><button class="reject">Dismiss</button></div>`
        : a.status === "approved" ? `<div class="result">✓ ${esc(a.result || "Approved")}</div>` : `<div class="muted">${esc(a.status)}</div>`}
    </div>`;
  }).join("");
}

function bindBrief() {
  document.querySelectorAll("[data-src]").forEach((el) => el.addEventListener("click", () => openSource(el.dataset.src, el.dataset.loc)));
  document.querySelectorAll(".action").forEach((el) => {
    const id = el.dataset.id;
    el.querySelector(".approve")?.addEventListener("click", () => decide(id, true, el.querySelector("textarea")?.value));
    el.querySelector(".reject")?.addEventListener("click", () => decide(id, false));
  });
}

async function decide(id, approve, message) {
  try { await api(`/api/actions/${id}`, { method: "POST", body: JSON.stringify({ approve, message: message ?? null }) }); }
  catch (e) { alert(e.message); }
  const data = await api(`/api/briefs/${selected}`);
  renderBrief(data);
  loadSchedule();
}

// ---------------------------------------------------------------- source drawer
async function openSource(id, loc) {
  const v = await api(`/api/sources/${id}`);
  $("#dKind").textContent = `${v.system} · updated ${v.last_updated}`;
  $("#dTitle").textContent = sourceLabels[id] || id;
  let html = "";
  if (v.kind === "document") {
    let [a, b] = [null, null];
    const m = (loc || "").match(/^L(\d+)(?:\s*-\s*L?(\d+))?$/);
    if (m) { a = +m[1]; b = +(m[2] || m[1]); }
    html = `<div class="doc">${v.content.text.split("\n").map((line) => {
      const [, n, rest] = line.match(/^L(\d+): ?(.*)$/) || [];
      return `<div class="ln ${a && +n >= a && +n <= b ? "hl" : ""}" ${+n === a ? 'id="hl"' : ""}><span>${n}</span><span>${esc(rest)}</span></div>`;
    }).join("")}</div>`;
  } else if (v.kind === "fhir") {
    html = v.content.resources.map((r) => `<div class="res-block ${r.locator === loc ? "hl" : ""}" ${r.locator === loc ? 'id="hl"' : ""}><span class="loc">${esc(r.locator)}</span>\n${esc(JSON.stringify(r.resource, null, 2))}</div>`).join("");
  } else {
    const top = loc ? loc.split(/[.[]/)[0] : null;
    html = Object.entries(v.content.export).map(([k, val]) => `<div class="res-block ${k === top ? "hl" : ""}" ${k === top ? 'id="hl"' : ""}><span class="loc">${esc(k)}</span>${loc && k === top && loc !== k ? ` <span class="muted">(cited: ${esc(loc)})</span>` : ""}\n${esc(JSON.stringify(val, null, 2))}</div>`).join("");
  }
  $("#dBody").innerHTML = html;
  $("#drawer").classList.add("open");
  $("#scrim").classList.add("open");
  setTimeout(() => document.getElementById("hl")?.scrollIntoView({ block: "center", behavior: "smooth" }), 230);
}
const closeDrawer = () => { $("#drawer").classList.remove("open"); $("#scrim").classList.remove("open"); };

// ---------------------------------------------------------------- init
$("#prepBtn").addEventListener("click", () => startRun("live"));
$("#replayBtn").addEventListener("click", () => startRun("replay"));
$("#printBtn").addEventListener("click", () => window.print());
$("#prepAllBtn").addEventListener("click", async () => {
  try { await api("/api/prep-all", { method: "POST" }); } catch (e) { alert(e.message); return; }
  pollBatch();
});
async function pollBatch() {
  const b = await api("/api/prep-all");
  const btn = $("#prepAllBtn");
  btn.disabled = b.running;
  btn.textContent = b.running ? `Preparing ${Math.min(b.done + 1, b.total)} of ${b.total}…` : "Prepare all briefs";
  await loadSchedule();
  if (b.running) setTimeout(pollBatch, 2000);
  else if (selected && !liveSource) select(selected);
}
$("#dClose").addEventListener("click", closeDrawer);
$("#scrim").addEventListener("click", closeDrawer);
document.addEventListener("keydown", (e) => e.key === "Escape" && closeDrawer());
$("#resetBtn").addEventListener("click", async () => { if (confirm("Clear all briefs, actions and outbox?")) { await api("/api/reset", { method: "POST" }); selected ? select(selected) : loadSchedule(); } });

(async () => {
  status = await api("/api/status");
  $("#meta").textContent = `${status.clinic} · ${status.physician} · ${status.date}`;
  const p = $("#modelPill");
  p.textContent = status.has_api_key ? status.model : "No API key: replay only";
  if (!status.has_api_key) p.classList.add("warn");
  await loadSchedule();
  pollBatch();
})();
