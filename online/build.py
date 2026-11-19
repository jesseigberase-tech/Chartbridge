"""Bundle the online version of ChartBridge into one self-contained HTML page.

The online page reuses web/ (dashboard), data/ (synthetic records) and the
agent prompt and brief schema from app/, and swaps the Python server for
online/backend.js, which runs the same tools and guardrails in the browser
and reaches Claude through the viewer's claude.ai account.

Run from the project root:  python online/build.py  ->  online/chartbridge-online.html
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.agent import SYSTEM_PROMPT  # noqa: E402
from app.tools import TOOLS  # noqa: E402


def must_replace(text, old, new):
    assert old in text, f"build: expected text not found: {old[:70]!r}"
    return text.replace(old, new, 1)


# ---- data
data_dir = ROOT / "data"
index = json.loads((data_dir / "sources/index.json").read_text())
files = {s["file"]: (data_dir / "sources" / s["file"]).read_text() for srcs in index.values() for s in srcs}
prompt = must_replace(
    SYSTEM_PROMPT,
    "1. Look up the appointment, then list and open EVERY record source for that patient.",
    "1. Open EVERY record source for that patient (the appointment and its sources are listed below).",
)
brief_schema = json.loads(json.dumps(next(t["input_schema"] for t in TOOLS if t["name"] == "submit_brief")))
# The online runtime allows tool schemas at most 8 levels deep. Describe each citation's
# fields in words instead of a nested schema; the checker still enforces them in code.
SHALLOW_CITATIONS = {
    "type": "array",
    "description": "At least one. Each item: {\"source_id\": string, \"locator\": string in that source's citation_format, \"quote\": short exact text copied from that location}.",
    "items": {"type": "object"},
}
for prop in brief_schema["properties"].values():
    item_props = prop.get("items", {}).get("properties", {})
    if "citations" in item_props:
        item_props["citations"] = SHALLOW_CITATIONS


def depth(node):
    if isinstance(node, dict):
        return 1 + max((depth(v) for v in node.values()), default=0)
    if isinstance(node, list):
        return max((depth(v) for v in node), default=0)
    return 0


assert depth(brief_schema) <= 8, f"brief schema depth {depth(brief_schema)} > 8"
assert len(json.dumps(brief_schema)) < 4000, "brief schema over the 4 KB tool-schema limit"
DATA = {
    "appointments": json.loads((data_dir / "appointments.json").read_text()),
    "slots": json.loads((data_dir / "slots.json").read_text()),
    "index": index,
    "files": files,
    "system_prompt": prompt,
    "brief_schema": brief_schema,
}

# ---- dashboard script, pointed at the in-page backend
js = (ROOT / "web/app.js").read_text()
js = must_replace(js, """const api = async (path, opts = {}) => {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.detail || r.statusText);
  return body;
};""", """const api = (path, opts = {}) => Backend.handle(path, opts);
function showError(msg) { const d = document.createElement("div"); d.className = "error"; d.textContent = msg; $("#briefArea").prepend(d); }""")
js = must_replace(js, "new EventSource(", "new Backend.Stream(")
js = js.replace("alert(e.message)", "showError(e.message)")
js = must_replace(js, 'if (confirm("Clear all briefs, actions and outbox?")) { ', "{ ")
js = must_replace(js, """    if (ev.type === "thought") addFeed(""", """    if (ev.type === "thought_live") {
      let li = document.querySelector("#feed li.live-thought");
      if (!li) { addFeed('<span class="ic">…</span><span class="txt"></span>', "thought live-thought"); li = document.querySelector("#feed li.live-thought"); }
      const t = ev.text.trim(); li.querySelector(".txt").textContent = t.length > 320 ? "…" + t.slice(-320) : t;
      $("#feed").appendChild(li); $("#feed").scrollTop = $("#feed").scrollHeight;
    }
    if (ev.type === "tool_result_note") addFeed(`<span class="ic">·</span><span class="txt">${esc(ev.text)}</span>`);
    if (ev.type === "thought") addFeed(""")
js = must_replace(js, """      const tok = ev.usage ?""", """      const tierNote = ev.tier ? ` · ${ev.tier} model` : "";
      const tok = ev.usage ?""")
js = must_replace(js, "${ev.seconds ? ` · ${ev.seconds}s` : \"\"}${tok}`", "${ev.seconds ? ` · ${ev.seconds}s` : \"\"}${tok}${tierNote}`")

# ---- markup and styles
html = (ROOT / "web/index.html").read_text()
body = re.search(r"<body>(.*?)<script", html, re.S).group(1)
body = must_replace(body, 'Synthetic data only</span>', 'Synthetic data only</span>\n    <span class="pill">Online · runs on your Claude account</span>')
body = must_replace(body, '<div id="briefArea"></div>', '<div id="briefArea"></div>\n      <div id="askArea"></div>')
body = must_replace(body, '<div class="outbox-wrap">', '<button class="ghost wide" id="auditBtn">Audit log</button>\n    <div class="outbox-wrap">')
css = (ROOT / "web/style.css").read_text()
css += """
#replayBtn, #printBtn { display: none !important; }
.feed > li.live-thought .txt { white-space: pre-wrap; }
:root { color-scheme: light; }
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) { --paper: #f6f3ec; --card: #fffdf8; --ink: #17252a; color-scheme: light; }
}
:root[data-theme="dark"] { --paper: #f6f3ec; --card: #fffdf8; --ink: #17252a; }
body { background: var(--paper); }
#askArea { margin-top: 16px; }
.ask-row { display: flex; gap: 8px; }
.ask-row input { flex: 1; min-width: 0; font: 15px var(--sans); padding: 9px 12px; border: 1px solid var(--line); border-radius: 8px; background: var(--card); color: var(--ink); }
.ask-row input:focus-visible { outline: 2px solid var(--teal); outline-offset: 1px; }
.ask-chips { display: flex; flex-wrap: wrap; gap: 6px; margin: 10px 0 4px; }
.ask-answer { margin-top: 12px; border-top: 1px solid var(--line); padding-top: 12px; }
.ask-answer p { margin: 6px 0 8px; }
.ask-q { font-weight: 600; }
.ask-meta { font-size: 12.5px; color: var(--green); margin-top: 8px; font-weight: 500; }
.ask-meta.warn { color: var(--amber); }
.audit { list-style: none; margin: 0; padding: 0; font-size: 13.5px; }
.audit li { display: grid; grid-template-columns: 150px 80px 1fr; gap: 8px; padding: 6px 0; border-top: 1px solid var(--line); }
.audit li.guard { background: var(--amber-soft); }
.audit .t { font: 12px var(--mono); color: var(--ink-3); }
.audit .who { font: 600 11.5px var(--sans); text-transform: uppercase; letter-spacing: .05em; color: var(--ink-2); }
"""

page = f"""<title>ChartBridge</title>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,500;9..144,600&family=Instrument+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
{css}
</style>
{body.strip()}
<script>const DATA = {json.dumps(DATA)};</script>
<script>
{(ROOT / "online/backend.js").read_text()}
</script>
<script>
{js}
</script>
<script>
{(ROOT / "online/extras.js").read_text()}
</script>
"""
out = ROOT / "online/chartbridge-online.html"
out.write_text(page)
print(f"wrote {out} ({len(page)//1024} KB)")
