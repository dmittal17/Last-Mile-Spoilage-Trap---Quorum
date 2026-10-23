"""Run: pip install fastapi uvicorn && uvicorn api:app --reload   (docs at /docs - judges can click through failures)"""
import sqlite3, json, itertools
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pathlib import Path
from pydantic import BaseModel
from engine import Engine, Batch, Rider, Shelter, Lease

app = FastAPI(title="Last-Mile Spoilage Trap")
DB_PATH = Path(__file__).with_name("state.db")

# ---------- SQLite Persistence Helpers ----------

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY,
                data TEXT NOT NULL,
                timestamp REAL NOT NULL
            )
        """)

def serialize_engine(eng: Engine) -> dict:
    max_id = max([0] + [int(k[1:]) for d in (eng.batches, eng.riders, eng.shelters, eng.leases) for k in d if len(k) > 1 and k[1:].isdigit()])
    return {
        "now": eng.now,
        "batches": {b.id: vars(b) for b in eng.batches.values()},
        "riders": {r.id: vars(r) for r in eng.riders.values()},
        "shelters": {s.id: {**vars(s), "supplies": list(s.supplies)} for s in eng.shelters.values()},
        "leases": {l.id: vars(l) for l in eng.leases.values()},
        "log": eng.log,
        "max_id": max_id
    }

def deserialize_engine(data: dict) -> Engine:
    eng = Engine()
    eng.now = data.get("now", 0.0)
    eng.batches = {
        k: Batch(v["id"], tuple(v["loc"]), v["qty"], v["category"], v["expires_at"], v["unallocated"])
        for k, v in data.get("batches", {}).items()
    }
    eng.riders = {
        k: Rider(v["id"], tuple(v["loc"]), v["capacity"], v.get("last_seen", 0.0), v.get("busy", False), v.get("strikes", 0))
        for k, v in data.get("riders", {}).items()
    }
    eng.shelters = {
        k: Shelter(v["id"], tuple(v["loc"]), v["need"], set(v.get("supplies", [])), v.get("open", True), v.get("delivered", 0))
        for k, v in data.get("shelters", {}).items()
    }
    eng.leases = {
        k: Lease(v["id"], v["batch"], v["rider"], v["shelter"], v["qty"], v["deadline"], v.get("state", "RESERVED"))
        for k, v in data.get("leases", {}).items()
    }
    eng.log = [tuple(item) for item in data.get("log", [])]
    max_id = data.get("max_id", 0)
    eng._n = itertools.count(max_id + 1)
    return eng

def save_state():
    raw = json.dumps(serialize_engine(E))
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT OR REPLACE INTO snapshots (id, data, timestamp) VALUES (1, ?, ?)", (raw, E.now))

def load_state() -> Engine | None:
    if not DB_PATH.exists():
        return None
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT data FROM snapshots WHERE id = 1").fetchone()
        if row:
            return deserialize_engine(json.loads(row[0]))
    return None

def snapshot():
    data = E.status()
    data["now"] = E.now
    return data

class RiderIn(BaseModel): lat: float; lon: float; capacity: int
class ShelterIn(BaseModel): lat: float; lon: float; need: int; has_water: bool = True; has_fuel: bool = True
class BatchIn(BaseModel): lat: float; lon: float; qty: int; category: str = "cooked"; shelf_life_min: float
class Ping(BaseModel): lat: float | None = None; lon: float | None = None
class Donation(BaseModel): qty: int
class Advance(BaseModel): minutes: float

@app.get("/", include_in_schema=False)
def login_page():
    p = Path(__file__).with_name("login.html")
    if p.exists():
        return FileResponse(p, media_type="text/html")
    return dashboard_page()

@app.get("/dashboard", include_in_schema=False)
def dashboard_page():
    p = Path(__file__).with_name("dashboard.html")
    if p.exists():
        return FileResponse(p, media_type="text/html")
    return {"title": "Last-Mile Spoilage Trap", "status": "running", "docs": "/docs"}

@app.post("/riders")
def rider(r: RiderIn):
    res = vars(E.add_rider((r.lat, r.lon), r.capacity))
    save_state()
    return res

@app.post("/shelters")
def shelter(s: ShelterIn):
    sup = {k for k, v in (("water", s.has_water), ("fuel", s.has_fuel)) if v}
    sh = E.add_shelter((s.lat, s.lon), s.need, sup)
    save_state()
    return {"id": sh.id}

@app.post("/batches")
def batch(b: BatchIn):
    # Delegate category validation to the engine (fail-closed via REQUIRES map).
    # Unknown categories pass through; engine emits UNPLACED with UNKNOWN_CATEGORY reason.
    x = E.add_batch((b.lat, b.lon), b.qty, b.category, b.shelf_life_min)
    save_state()
    return {"batch": x.id, "unplaced": x.unallocated, "leases": [l for l in E.status()["leases"].values() if l["batch"] == x.id]}

@app.post("/riders/{rid}/heartbeat")
def hb(rid: str, p: Ping):
    if rid not in E.riders: raise HTTPException(404, f"Rider {rid} not found")
    E.heartbeat(rid, (p.lat, p.lon) if p.lat is not None else None)
    save_state()
    return "ok"

@app.post("/leases/{lid}/pickup")
def pickup(lid: str):
    if lid not in E.leases: raise HTTPException(404, f"Lease {lid} not found")
    res = E.pickup(lid)
    save_state()
    return {"ok": res}

@app.post("/leases/{lid}/deliver")
def deliver(lid: str):
    if lid not in E.leases: raise HTTPException(404, f"Lease {lid} not found")
    res = E.deliver(lid)
    save_state()
    return {"ok": res}

@app.post("/shelters/{sid}/close")
def close(sid: str):
    if sid not in E.shelters: raise HTTPException(404, f"Shelter {sid} not found")
    E.close_shelter(sid)
    save_state()
    return "closed"

@app.post("/shelters/{sid}/direct-donation")
def donate(sid: str, d: Donation):
    if sid not in E.shelters: raise HTTPException(404, f"Shelter {sid} not found")
    E.direct_donation(sid, d.qty)
    save_state()
    return "ok"

@app.post("/sim/advance")                      # fast-forward the clock to demo ghost riders / expiry
def adv(a: Advance):
    E.tick(a.minutes)
    save_state()
    return snapshot()

@app.post("/sim/demo")
def demo():
    global E
    E = Engine()
    P = (18.52, 73.85)
    for i in range(7):
        E.add_rider((P[0] + 0.002 * i, P[1] + 0.001), 40, rid=f"R{i+1}")
    for i, (n, (x, y)) in enumerate(zip((50, 60, 70), ((0.01, 0), (0, 0.02), (0.02, 0.01)))):
        E.add_shelter((P[0] + x, P[1] + y), n, ("water", "fuel"), sid=f"S{i+1}")
    E.add_batch(P, 180, "cooked", 75)
    save_state()
    return snapshot()

@app.post("/sim/ghost")
def ghost():
    live = [l for l in E.leases.values() if l.state == "RESERVED"]
    if live:
        ghost_rider = live[0].rider
        other_riders = [r.id for r in E.riders.values() if r.id != ghost_rider]
        E.tick(3)
        for rid in other_riders: E.heartbeat(rid)
        E.tick(3)
        for rid in other_riders: E.heartbeat(rid)
    else:
        E.tick(6)
    save_state()
    return snapshot()

@app.post("/sim/reset")
def reset_sim():
    global E
    E = Engine()
    save_state()
    return snapshot()

@app.get("/status")
def status(): return snapshot()

# ---------- Startup Initialization ----------
init_db()
loaded = load_state()
if loaded is not None:
    E = loaded
else:
    E = Engine()
    demo()
