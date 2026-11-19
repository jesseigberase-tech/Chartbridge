"""FastAPI server: runs the agent and serves the physician dashboard.

Run:  uvicorn app.server:app --reload
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import store
from .agent import MODEL, run_agent

try:  # optional: load ANTHROPIC_API_KEY from a .env file
    from dotenv import load_dotenv
    load_dotenv(store.ROOT / ".env")
except ImportError:
    pass

WEB = store.ROOT / "web"
RUNS_DIR = store.RUNTIME / "runs"
RUNS_DIR.mkdir(exist_ok=True)

app = FastAPI(title="ChartBridge")
app.mount("/static", StaticFiles(directory=WEB), name="static")

RUNS: dict[str, dict] = {}  # run_id -> {"events": [...], "done": bool}
ACTIVE: dict[str, str] = {}  # appointment_id -> run_id currently running


def _new_run():
    run_id = uuid.uuid4().hex[:8]
    RUNS[run_id] = {"events": [], "done": False}
    return run_id


def _emitter(run_id):
    def emit(ev):
        ev = {"t": round(time.time(), 2), **ev}
        RUNS[run_id]["events"].append(ev)
    return emit


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


@app.get("/api/status")
def status():
    return {"model": MODEL, "has_api_key": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "clinic": store.APPOINTMENTS["clinic"], "physician": store.APPOINTMENTS["physician"],
            "date": store.APPOINTMENTS["date"]}


@app.get("/api/schedule")
def schedule():
    out = []
    for a in store.APPOINTMENTS["appointments"]:
        brief = store.STATE["briefs"].get(a["appointment_id"])
        acts = store.actions_for(a["appointment_id"])
        out.append({
            **a,
            "sources": len(store.SOURCE_INDEX.get(a["patient_id"], [])),
            "prepared": brief is not None,
            "high_flags": sum(1 for f in (brief or {}).get("flags", []) if f["severity"] == "high"),
            "pending_actions": sum(1 for x in acts if x["status"] == "pending"),
            "has_recording": (RUNS_DIR / f'{a["appointment_id"]}.json').exists(),
            "running": a["appointment_id"] in ACTIVE,
        })
    return out


def _require_key():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise HTTPException(400, "ANTHROPIC_API_KEY is not set. Add it to .env and restart, or use Replay.")


def _run_blocking(appointment_id: str, run_id: str):
    emit = _emitter(run_id)
    try:
        run_agent(appointment_id, emit)
        # Keep a recording of real runs so the demo can be replayed offline.
        (RUNS_DIR / f"{appointment_id}.json").write_text(json.dumps({
            "events": RUNS[run_id]["events"],
            "brief": store.STATE["briefs"].get(appointment_id),
            "actions": store.actions_for(appointment_id),
        }, indent=2))
    except Exception as e:  # surface API/key errors in the UI
        emit({"type": "error", "message": f"{type(e).__name__}: {e}"})
        emit({"type": "done", "ok": False})
    finally:
        RUNS[run_id]["done"] = True
        ACTIVE.pop(appointment_id, None)


@app.post("/api/prep/{appointment_id}")
def prep(appointment_id: str):
    if store.get_appointment(appointment_id) is None:
        raise HTTPException(404, "Unknown appointment")
    _require_key()
    if appointment_id in ACTIVE:
        return {"run_id": ACTIVE[appointment_id]}
    run_id = _new_run()
    ACTIVE[appointment_id] = run_id
    threading.Thread(target=_run_blocking, args=(appointment_id, run_id), daemon=True).start()
    return {"run_id": run_id}


BATCH = {"running": False, "done": 0, "total": 0}


@app.post("/api/prep-all")
def prep_all():
    """Prepare every appointment on the schedule that has no brief yet, one after
    another. In a real clinic this would run automatically the night before."""
    _require_key()
    if BATCH["running"]:
        return BATCH
    todo = [a["appointment_id"] for a in store.APPOINTMENTS["appointments"]
            if a["appointment_id"] not in store.STATE["briefs"] and a["appointment_id"] not in ACTIVE]
    BATCH.update(running=bool(todo), done=0, total=len(todo))

    def work():
        try:
            for appt_id in todo:
                run_id = _new_run()
                ACTIVE[appt_id] = run_id
                _run_blocking(appt_id, run_id)
                BATCH["done"] += 1
        finally:
            BATCH["running"] = False

    if todo:
        threading.Thread(target=work, daemon=True).start()
    return BATCH


@app.get("/api/prep-all")
def prep_all_status():
    return BATCH


@app.post("/api/replay/{appointment_id}")
def replay(appointment_id: str):
    """Replays a recorded real run (for flaky conference Wi-Fi). Clearly labelled in the UI."""
    path = RUNS_DIR / f"{appointment_id}.json"
    if not path.exists():
        raise HTTPException(404, "No recorded run for this appointment yet. Run it live once first.")
    rec = json.loads(path.read_text())
    run_id = _new_run()
    emit = _emitter(run_id)

    def work():
        store.clear_actions_for(appointment_id)
        prev = None
        for ev in rec["events"]:
            gap = 0 if prev is None else min(max(ev.get("t", 0) - prev, 0.15), 1.2)
            prev = ev.get("t", 0)
            time.sleep(gap)
            ev = {k: v for k, v in ev.items() if k != "t"}
            if ev["type"] == "start":
                ev["replay"] = True
            if ev["type"] == "action":
                ev["action"] = store.add_action({k: v for k, v in ev["action"].items()
                                                 if k not in ("action_id", "status", "created", "decided", "result")})
            if ev["type"] == "done" and rec.get("brief"):
                store.save_brief(appointment_id, rec["brief"])
            emit(ev)
        RUNS[run_id]["done"] = True

    threading.Thread(target=work, daemon=True).start()
    return {"run_id": run_id}


@app.get("/api/runs/{run_id}/stream")
async def stream(run_id: str):
    if run_id not in RUNS:
        raise HTTPException(404, "Unknown run")

    async def gen():
        i = 0
        while True:
            run = RUNS[run_id]
            while i < len(run["events"]):
                yield f"data: {json.dumps(run['events'][i])}\n\n"
                i += 1
            if run["done"] and i >= len(run["events"]):
                break
            await asyncio.sleep(0.15)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/briefs/{appointment_id}")
def get_brief(appointment_id: str):
    appt = store.get_appointment(appointment_id)
    if appt is None:
        raise HTTPException(404, "Unknown appointment")
    return {"appointment": appt, "brief": store.STATE["briefs"].get(appointment_id),
            "actions": store.actions_for(appointment_id),
            "sources": store.list_sources(appt["patient_id"])}


@app.get("/api/sources/{source_id}")
def get_source(source_id: str):
    view = store.build_view(source_id)
    if view is None:
        raise HTTPException(404, "Unknown source")
    store.audit("physician", "view_source", {"source_id": source_id})
    return {k: view[k] for k in ("source_id", "system", "kind", "last_updated", "content")} | {
        "locators": list(view["locators"].keys())}


class Decision(BaseModel):
    approve: bool
    message: str | None = None


@app.post("/api/actions/{action_id}")
def decide(action_id: str, d: Decision):
    a = store.decide_action(action_id, d.approve, d.message)
    if a is None:
        raise HTTPException(409, "Action not found or already decided")
    return a


@app.get("/api/outbox")
def outbox():
    return store.STATE["outbox"]


@app.get("/api/audit")
def audit_log():
    return store.STATE["audit"][-200:]


@app.post("/api/reset")
def reset():
    store.reset()
    return {"ok": True}
