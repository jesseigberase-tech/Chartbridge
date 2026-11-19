"""Data access layer: loads the synthetic clinic data and keeps runtime state.

Everything here is synthetic demo data. Runtime state (proposed actions,
outbox, audit log, saved briefs) lives in memory and is mirrored to
runtime/state.json so a server restart doesn't lose the demo.
"""
from __future__ import annotations

import json
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
RUNTIME = ROOT / "runtime"
RUNTIME.mkdir(exist_ok=True)
STATE_FILE = RUNTIME / "state.json"

_lock = threading.RLock()


def _load(name: str):
    return json.loads((DATA / name).read_text())


APPOINTMENTS = _load("appointments.json")
SOURCE_INDEX = _load("sources/index.json")
_SLOTS = _load("slots.json")["open_slots"]


def _fresh_state():
    return {"actions": {}, "outbox": [], "audit": [], "briefs": {}, "booked": {}}


def _read_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return _fresh_state()


STATE = _read_state()


def save():
    with _lock:
        STATE_FILE.write_text(json.dumps(STATE, indent=2))


def reset():
    with _lock:
        STATE.clear()
        STATE.update(_fresh_state())
        save()


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------- appointments

def get_appointment(appointment_id: str):
    for a in APPOINTMENTS["appointments"]:
        if a["appointment_id"] == appointment_id:
            return a
    return None


def list_sources(patient_id: str):
    return [
        {k: s[k] for k in ("source_id", "kind", "system", "description", "last_updated")}
        for s in SOURCE_INDEX.get(patient_id, [])
    ]


def source_meta(source_id: str):
    for pid, sources in SOURCE_INDEX.items():
        for s in sources:
            if s["source_id"] == source_id:
                return pid, s
    return None, None


# --------------------------------------------------------------- source views
# Each source is turned into a "view": content for the model plus a map of
# locator -> text. Citations in a brief must use one of those locators and
# quote text that actually appears there; that is how we check the agent
# isn't making things up.

def _json_paths(obj, prefix=""):
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{prefix}.{k}" if prefix else k
            out[p] = json.dumps(v) if not isinstance(v, str) else v
            out.update(_json_paths(v, p))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            p = f"{prefix}[{i}]"
            out[p] = json.dumps(v) if not isinstance(v, str) else v
            out.update(_json_paths(v, p))
    return out


def build_view(source_id: str):
    pid, meta = source_meta(source_id)
    if meta is None:
        return None
    path = DATA / "sources" / meta["file"]
    kind = meta["kind"]
    if kind == "fhir":
        bundle = json.loads(path.read_text())
        resources, locators = [], {}
        for e in bundle["entry"]:
            r = e["resource"]
            loc = f'{r["resourceType"]}/{r["id"]}'
            resources.append({"locator": loc, "resource": r})
            locators[loc] = json.dumps(r)
        content = {
            "citation_format": "Cite FHIR resources by locator, e.g. 'MedicationStatement/ms-1'.",
            "resources": resources,
        }
    elif kind == "emr_export":
        raw = json.loads(path.read_text())
        locators = _json_paths(raw)
        content = {
            "citation_format": "Cite with a JSON path into this export, e.g. 'rx_changes[0]' or 'visit.assessment_note'.",
            "export": raw,
        }
    else:  # document (fax / lab text)
        lines = path.read_text().splitlines()
        locators = {f"L{i}": line for i, line in enumerate(lines, 1)}
        numbered = "\n".join(f"L{i}: {line}" for i, line in enumerate(lines, 1))
        content = {
            "citation_format": "Cite with a line number such as 'L7' or a range such as 'L6-L8'.",
            "text": numbered,
        }
    return {
        "source_id": source_id,
        "patient_id": pid,
        "system": meta["system"],
        "kind": kind,
        "last_updated": meta["last_updated"],
        "content": content,
        "locators": locators,
    }


_ws = re.compile(r"\s+")


def _norm(s: str) -> str:
    return _ws.sub(" ", s.replace('"', "").lower()).strip()


def resolve_locator(view, locator: str):
    """Return the text a locator points to, or None if it doesn't exist."""
    locs = view["locators"]
    if locator in locs:
        return locs[locator]
    m = re.fullmatch(r"L(\d+)\s*-\s*L?(\d+)", locator.strip())
    if m and view["kind"] == "document":
        a, b = int(m.group(1)), int(m.group(2))
        if a <= b and f"L{a}" in locs and f"L{b}" in locs:
            return "\n".join(locs[f"L{i}"] for i in range(a, b + 1))
    return None


_word = re.compile(r"[a-z]+")


def _key_name(s: str) -> str:
    """'LISINOPRIL 10MG' -> 'lisinopril', 'Budesonide/formoterol 200/6' -> 'budesonide'."""
    m = _word.search(s.lower())
    return m.group(0) if m else ""


def structured_items(view):
    """Medications and allergies read deterministically (no AI) from structured
    sources. Used to check the agent didn't silently leave anything out.
    Free-text documents (faxes, lab text) are not parsed here."""
    meds, allergies = [], []
    if view["kind"] == "fhir":
        for r in (x["resource"] for x in view["content"]["resources"]):
            loc = f'{r["resourceType"]}/{r["id"]}'
            text = (r.get("medicationCodeableConcept") or r.get("code") or {}).get("text", "")
            if r["resourceType"] == "MedicationStatement" and text:
                meds.append((_key_name(text), text, loc))
            if r["resourceType"] == "AllergyIntolerance" and text and not text.lower().startswith("no known"):
                allergies.append((_key_name(text), text, loc))
    elif view["kind"] == "emr_export":
        for key, val in view["content"]["export"].items():
            k = key.lower()
            if not isinstance(val, list):
                continue
            for i, item in enumerate(val):
                loc = f"{key}[{i}]"
                if isinstance(item, str):
                    label = item
                elif isinstance(item, dict):
                    label = item.get("drug") or item.get("name") or item.get("agent") or ""
                else:
                    continue
                if not label:
                    continue
                if "allerg" in k:
                    allergies.append((_key_name(label), label, loc))
                elif "rx" in k or "medication" in k:
                    meds.append((_key_name(label), label, loc))
    return {"medications": meds, "allergies": allergies}


INJECTION_PATTERNS = [
    r"ignore (all |any )?(previous|prior|above) (instructions|rules)",
    r"\b(system|developer) (note|prompt|message|instruction)s?\b",
    r"\bnote to (the )?(ai|assistant|agent|model)\b",
    r"\byou are (now )?(an?|the) (ai|assistant|agent)\b",
    r"\b(mark|set) (all|every) (flags?|items?) (as )?(low|resolved)\b",
    r"\bdo not (flag|mention|report)\b",
]


def detect_injection(view):
    """Cheap, deterministic scan for text that looks like instructions aimed at
    an AI. Hits are reported to the agent and shown to the physician; the
    record is still treated purely as data."""
    hits = []
    for loc, text in view["locators"].items():
        for pat in INJECTION_PATTERNS:
            if re.search(pat, text, re.I):
                hits.append({"locator": loc, "text": text[:200]})
                break
    # Keep only the most specific location (drop a JSON parent when its child also matched).
    locs = [h["locator"] for h in hits]
    return [h for h in hits if not any(o != h["locator"] and o.startswith(h["locator"]) for o in locs)]


def quote_supported(text: str, quote: str) -> bool:
    q = _norm(quote)
    return bool(q) and q in _norm(text)


# ------------------------------------------------------------------- schedule

def open_slots(slot_type: str | None = None):
    with _lock:
        booked = set(STATE["booked"].keys())
        reserved = {
            a["slot_id"] for a in STATE["actions"].values()
            if a.get("slot_id") and a["status"] == "pending"
        }
    return [
        s for s in _SLOTS
        if s["slot_id"] not in booked and s["slot_id"] not in reserved
        and (slot_type is None or s["type"] == slot_type)
    ]


def slot(slot_id: str):
    return next((s for s in _SLOTS if s["slot_id"] == slot_id), None)


# -------------------------------------------------------------------- actions

def add_action(action: dict) -> dict:
    with _lock:
        action_id = "act-" + uuid.uuid4().hex[:6]
        action.update({"action_id": action_id, "status": "pending", "created": now_iso()})
        STATE["actions"][action_id] = action
        save()
        return action


def actions_for(appointment_id: str):
    with _lock:
        return [a for a in STATE["actions"].values() if a["appointment_id"] == appointment_id]


def clear_actions_for(appointment_id: str):
    """Drop still-pending proposals before a re-run so they don't pile up."""
    with _lock:
        for aid in [a["action_id"] for a in actions_for(appointment_id) if a["status"] == "pending"]:
            del STATE["actions"][aid]
        save()


def decide_action(action_id: str, approve: bool, edited_message: str | None = None):
    """Physician approves or rejects. Approval is the only way anything executes."""
    with _lock:
        a = STATE["actions"].get(action_id)
        if a is None or a["status"] != "pending":
            return None
        if not approve:
            a["status"] = "rejected"
        else:
            if edited_message is not None:
                a["message"] = edited_message
            if a.get("slot_id"):
                if a["slot_id"] in STATE["booked"]:
                    a["status"] = "failed"
                    a["result"] = "Slot was already taken."
                    save()
                    return a
                STATE["booked"][a["slot_id"]] = {
                    "patient_id": a["patient_id"], "action_id": action_id, "title": a["title"],
                }
                a["result"] = f'Booked {slot(a["slot_id"])["start"].replace("T", " ")}'
            if a["action_type"] in ("patient_message", "request_records") and a.get("message"):
                STATE["outbox"].append({
                    "to": a.get("recipient") or "patient",
                    "patient_id": a["patient_id"],
                    "message": a["message"],
                    "sent": now_iso(),
                    "action_id": action_id,
                })
                a["result"] = (a.get("result", "") + " Message queued (demo outbox, nothing really sent).").strip()
            if a["action_type"] == "create_task":
                a["result"] = "Added to clinic task list."
            a["status"] = "approved"
        a["decided"] = now_iso()
        audit("physician", "approve_action" if approve else "reject_action", {"action_id": action_id})
        save()
        return a


# ---------------------------------------------------------------------- audit

def audit(actor: str, event: str, detail: dict):
    with _lock:
        STATE["audit"].append({"t": now_iso(), "actor": actor, "event": event, "detail": detail})
        STATE["audit"] = STATE["audit"][-500:]


def save_brief(appointment_id: str, brief: dict):
    with _lock:
        STATE["briefs"][appointment_id] = brief
        save()
