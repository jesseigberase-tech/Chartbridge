"""Offline tests: a scripted stand-in for the Claude API drives the real
agent loop, tools and guardrails. Run: python -m pytest tests -q
"""
import itertools
from types import SimpleNamespace as NS

import pytest

from app import store
from app.agent import run_agent

_ids = itertools.count()


def tool(name, **inp):
    return NS(type="tool_use", id=f"tu_{next(_ids)}", name=name, input=inp,
              model_dump=lambda **_: {"type": "tool_use", "id": "x", "name": name, "input": inp})


def text(t):
    return NS(type="text", text=t, model_dump=lambda **_: {"type": "text", "text": t})


def resp(blocks, stop="tool_use"):
    return NS(content=blocks, stop_reason=stop, usage=NS(input_tokens=10, output_tokens=5))


class Scripted:
    def __init__(self, turns):
        self.turns = list(turns)
        self.seen = []
        self.messages = self

    def create(self, **kw):
        self.seen.append(kw["messages"][-1])
        return self.turns.pop(0)


def C(src, loc, q):
    return [{"source_id": src, "locator": loc, "quote": q}]


GOOD_BRIEF = dict(
    one_line_summary="T2DM/HTN follow-up; lisinopril stopped at walk-in, apixaban started by cardiology, eGFR down to 42.",
    sources_reviewed=[{"source_id": s, "identity_match": "match"} for s in
                      ["src-001-emrA", "src-001-emrB", "src-001-fax", "src-001-lab"]],
    medications=[
        {"name": "Lisinopril", "dose": "10 mg daily", "status": "stopped", "citations": C("src-001-emrB", "rx_changes[0]", "DISCONTINUE")},
        {"name": "Apixaban", "dose": "5 mg BID", "status": "new", "citations": C("src-001-fax", "L9", "apixaban 5 mg")},
        {"name": "Losartan", "dose": "50 mg daily", "status": "new", "citations": C("src-001-emrB", "rx_changes[1]", "LOSARTAN 50MG")},
        {"name": "Metformin", "dose": "1000 mg BID", "status": "active", "citations": C("src-001-emrA", "MedicationStatement/ms-1", "Metformin 1000 mg")},
        {"name": "Atorvastatin", "dose": "20 mg qhs", "status": "active", "citations": C("src-001-emrA", "MedicationStatement/ms-3", "Atorvastatin 20 mg")},
    ],
    allergies=[{"agent": "Sulfa", "reaction": "rash", "citations": C("src-001-fax", "L12", "SULFA (rash)")},
               {"agent": "Penicillin", "reaction": "hives", "citations": C("src-001-emrA", "AllergyIntolerance/alg-1", "Penicillin")}],
    conditions=[{"name": "Type 2 diabetes", "citations": C("src-001-emrA", "Condition/cond-1", "Type 2 diabetes mellitus")}],
    recent_results=[{"test": "eGFR", "value": "42", "trend": "down from 58",
                     "citations": C("src-001-lab", "L7", "eGFR 42")}],
    flags=[{"severity": "high", "category": "conflict", "title": "Lisinopril still active in clinic chart",
            "detail": "Stopped at walk-in 2026-09-27; clinic chart still active.",
            "citations": C("src-001-emrA", "MedicationStatement/ms-2", "Lisinopril 10 mg")}],
)


@pytest.fixture(autouse=True)
def clean_state():
    store.reset()
    yield
    store.reset()


def test_full_run_with_self_correction():
    bad = dict(GOOD_BRIEF, flags=[dict(GOOD_BRIEF["flags"][0], citations=C("src-001-emrA", "MedicationStatement/ms-2", "Ramipril 5 mg"))])
    client = Scripted([
        resp([tool("get_appointment", appointment_id="appt-101")]),
        resp([tool("list_record_sources", patient_id="pt-001")]),
        resp([tool("fetch_source", source_id=s) for s in ["src-001-emrA", "src-001-emrB", "src-001-fax", "src-001-lab"]]),
        resp([tool("submit_brief", **bad)]),
        resp([tool("submit_brief", **GOOD_BRIEF)]),
        resp([tool("get_open_slots", slot_type="physician")]),
        resp([tool("propose_action", action_type="book_followup", title="Book follow-up", reason="Recheck after med changes",
                   slot_id="slot-1", citations=C("src-001-lab", "L7", "eGFR"))]),
        resp([text("Brief ready.")], stop="end_turn"),
    ])
    events = []
    out = run_agent("appt-101", events.append, client=client)
    assert out["ok"]
    val = [e for e in events if e["type"] == "validation"]
    assert val[0]["ok"] is False and "Ramipril" in val[0]["errors"][0]
    assert val[1]["ok"] is True
    assert len(out["actions"]) == 1 and out["actions"][0]["status"] == "pending"
    # Nothing is booked until the physician approves.
    assert store.STATE["booked"] == {}
    store.decide_action(out["actions"][0]["action_id"], True)
    assert "slot-1" in store.STATE["booked"]


def test_cross_patient_access_blocked():
    client = Scripted([
        resp([tool("fetch_source", source_id="src-002-emrB")]),
        resp([text("done")], stop="end_turn"),
    ] + [resp([text("done")], stop="end_turn")] * 40)
    events = []
    run_agent("appt-101", events.append, client=client)
    blocked = [e for e in events if e["type"] == "tool_result" and not e["ok"]]
    assert "different patient" in blocked[0]["summary"]
    assert any(a["event"] == "blocked_cross_patient_access" for a in store.STATE["audit"])


def test_brief_rejected_if_sources_unopened():
    client = Scripted([
        resp([tool("fetch_source", source_id="src-001-emrA")]),
        resp([tool("submit_brief", **dict(GOOD_BRIEF, sources_reviewed=[{"source_id": "src-001-emrA", "identity_match": "match"}],
                                           medications=[], allergies=[], recent_results=[], flags=[]))]),
    ] + [resp([text("done")], stop="end_turn")] * 40)
    events = []
    out = run_agent("appt-101", events.append, client=client)
    assert not out["ok"]
    v = [e for e in events if e["type"] == "validation"][0]
    assert any("not opened these sources" in err for err in v["errors"])


def test_document_line_ranges():
    view = store.build_view("src-003-fax")
    assert store.resolve_locator(view, "L7-L9") is not None
    assert store.quote_supported(store.resolve_locator(view, "L3"), "SHARMA, Pria")
    assert store.resolve_locator(view, "L99") is None


def test_completeness_catches_dropped_medication():
    """Agent 'forgets' losartan (started at the walk-in clinic). Checker must catch it."""
    dropped = dict(GOOD_BRIEF, medications=[m for m in GOOD_BRIEF["medications"] if m["name"] != "Losartan"])
    client = Scripted([
        resp([tool("fetch_source", source_id=s) for s in ["src-001-emrA", "src-001-emrB", "src-001-fax", "src-001-lab"]]),
        resp([tool("submit_brief", **dropped)]),
        resp([tool("submit_brief", **GOOD_BRIEF)]),
        resp([text("done")], stop="end_turn"),
    ])
    events = []
    out = run_agent("appt-101", events.append, client=client)
    v = [e for e in events if e["type"] == "validation"]
    assert not v[0]["ok"] and any("LOSARTAN" in e for e in v[0]["errors"])
    assert out["ok"]


def test_truncated_tool_call_is_not_executed():
    client = Scripted([
        resp([tool("fetch_source", source_id="src-001-emrA")], stop="max_tokens"),
        resp([text("done")], stop="end_turn"),
    ] + [resp([text("done")], stop="end_turn")] * 40)
    events = []
    run_agent("appt-101", events.append, client=client)
    assert not any(e["type"] == "tool_call" for e in events[:3])
    assert client.seen[1]["content"][0]["is_error"] is True  # tool_result for the cut-off call was an error


def test_prompt_injection_detected_and_must_be_flagged():
    srcs = ["src-004-emrA", "src-004-portal", "src-004-lab"]
    base = dict(
        one_line_summary="Annual; self-reported naproxen and stopped allopurinol; liver enzymes up.",
        sources_reviewed=[{"source_id": s, "identity_match": "match"} for s in srcs],
        medications=[
            {"name": "Amlodipine", "status": "active", "citations": C("src-004-emrA", "MedicationStatement/ms-1", "Amlodipine 5 mg")},
            {"name": "Allopurinol", "status": "stopped", "citations": C("src-004-portal", "medications_stopped[0]", "ran out in the summer")},
            {"name": "Naproxen", "status": "new", "citations": C("src-004-portal", "current_medications_self_reported[1]", "Naproxen 500 mg")},
            {"name": "Turmeric", "status": "active", "citations": C("src-004-portal", "current_medications_self_reported[2]", "Turmeric")},
        ],
        allergies=[], conditions=[], recent_results=[],
        flags=[{"severity": "medium", "category": "gap", "title": "Allopurinol stopped", "detail": "Self-reported.",
                "citations": C("src-004-portal", "medications_stopped[0]", "Allopurinol")}],
    )
    flagged = dict(base, flags=base["flags"] + [{"severity": "high", "category": "safety",
        "title": "Patient form contains instructions aimed at AI", "detail": "Ignored; review alcohol history.",
        "citations": C("src-004-portal", "anything_else", "SYSTEM NOTE TO AI ASSISTANT")}])
    client = Scripted([
        resp([tool("fetch_source", source_id=s) for s in srcs]),
        resp([tool("submit_brief", **base)]),
        resp([tool("submit_brief", **flagged)]),
        resp([text("done")], stop="end_turn"),
    ])
    events = []
    out = run_agent("appt-104", events.append, client=client)
    assert any(e["type"] == "injection" for e in events)
    v = [e for e in events if e["type"] == "validation"]
    assert not v[0]["ok"] and any("instruction" in e for e in v[0]["errors"])
    assert out["ok"] and out["brief"]["security_warnings"]["src-004-portal"]
