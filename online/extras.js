// Online-only extras: "Ask ChartBridge" and the audit log viewer.
// Runs after web/app.js, so it uses its helpers ($, esc, cites, openSource, sourceLabels, selected).
/* global Backend, $, esc, cites, openSource, sourceLabels, selected */
(() => {
  const SUGGESTED = [
    "What changed since the last visit?",
    "Which medications did other providers change?",
    "Is anything still waiting on a result or follow-up?",
  ];
  const area = $("#askArea");
  area.innerHTML = `
    <div class="card ask">
      <h2>Ask ChartBridge <span class="muted">answers only from this patient’s records, every citation checked</span></h2>
      <form id="askForm" class="ask-row">
        <input id="askInput" type="text" maxlength="300" placeholder="Ask about this patient’s records…" autocomplete="off">
        <button class="primary" id="askBtn" type="submit">Ask</button>
      </form>
      <div class="ask-chips">${SUGGESTED.map((q) => `<button class="ghost small chip-q" type="button">${esc(q)}</button>`).join("")}</div>
      <div id="askOut"></div>
    </div>`;
  const out = $("#askOut");
  async function run(q) {
    q = q.trim();
    if (!q || !selected) return;
    const id = selected;
    $("#askBtn").disabled = true;
    out.innerHTML = `<div class="ask-answer"><div class="ask-q">${esc(q)}</div><div class="muted">Reading the records…</div></div>`;
    try {
      const r = await Backend.ask(id, q);
      if (selected !== id) return;
      const ok = r.citations.filter((c) => c.verified);
      const bad = r.citations.length - ok.length;
      out.innerHTML = `<div class="ask-answer"><div class="ask-q">${esc(q)}</div><p>${esc(r.answer)}</p>
        ${cites(ok)}
        <div class="ask-meta ${bad ? "warn" : ""}">${ok.length ? `✓ ${ok.length} citation${ok.length > 1 ? "s" : ""} verified against the records` : "No citations given"}${bad ? ` · ${bad} citation${bad > 1 ? "s" : ""} could not be verified and ${bad > 1 ? "were" : "was"} removed` : ""}</div></div>`;
      out.querySelectorAll("[data-src]").forEach((el) => el.addEventListener("click", () => openSource(el.dataset.src, el.dataset.loc)));
    } catch (e) {
      out.innerHTML = `<div class="error">${esc(e.message)}</div>`;
    } finally {
      $("#askBtn").disabled = false;
    }
  }
  $("#askForm").addEventListener("submit", (e) => { e.preventDefault(); run($("#askInput").value); });
  area.querySelectorAll(".chip-q").forEach((b) => b.addEventListener("click", () => { $("#askInput").value = b.textContent; run(b.textContent); }));
  // Clear the answer when another patient is picked.
  $("#appts").addEventListener("click", () => { out.innerHTML = ""; $("#askInput").value = ""; });

  // Audit log in the side drawer.
  const LABEL = {
    fetch_source: "Agent opened a record", submit_brief: "Agent submitted the brief", propose_action: "Agent proposed a follow-up",
    approve_action: "Physician approved", reject_action: "Physician dismissed", view_source: "Physician viewed a record",
    blocked_cross_patient_access: "Blocked: access to another patient’s record", suspicious_instructions_in_record: "Flagged: instructions aimed at the AI inside a record",
    ask: "Physician asked a question",
  };
  $("#auditBtn").addEventListener("click", () => {
    const rows = Backend.audit().slice().reverse();
    $("#dKind").textContent = "Every agent step, guardrail hit and physician decision";
    $("#dTitle").textContent = "Audit log";
    $("#dBody").innerHTML = rows.length ? `<ul class="audit">${rows.map((r) => {
      const d = r.detail || {};
      const what = d.source_id ? (sourceLabels[d.source_id] || d.source_id) : d.appointment_id || d.action_id || "";
      const guard = r.actor === "guardrail";
      return `<li class="${guard ? "guard" : ""}"><span class="t">${esc(r.t.replace("T", " "))}</span><span class="who">${esc(r.actor)}</span><span>${esc(LABEL[r.event] || r.event)}${what ? ` <span class="muted">· ${esc(what)}</span>` : ""}${d.question ? `<br><span class="muted">“${esc(d.question)}”</span>` : ""}</span></li>`;
    }).join("")}</ul>` : '<p class="muted">Nothing logged yet. Prepare a chart to see the agent’s steps here.</p>';
    $("#drawer").classList.add("open");
    $("#scrim").classList.add("open");
  });
})();
