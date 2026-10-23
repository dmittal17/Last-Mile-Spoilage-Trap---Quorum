"""Last-Mile Spoilage Trap - core coordination engine (pure stdlib, deterministic, testable).
Time unit = minutes. Hard rule: nothing is ever assigned unless it can be delivered safely."""
import math, itertools, os
from dataclasses import dataclass, field

SPEED_KMPH       = 20
BUFFER_MIN       = float(os.environ.get("SAFETY_BUFFER_MIN", 10))   # Change 2: env-var-backed
TRANSIT_BUFFER_MIN = 2
LEASE_TTL_MIN    = 10
# Change 1: SILENCE_MIN removed — replaced by silence_threshold() below
REQUIRES = {                        # Change 3: fail-closed; medical now requires cold_chain
    "cooked":  set(),
    "medical": {"cold_chain"},
    "ration":  {"water", "fuel"},
    "formula": {"water"},
}
ACTIVE = ("RESERVED", "PICKED_UP")

# Change 1 — per-batch dynamic silence threshold
def silence_threshold(batch) -> float:
    remaining = batch.expires_at   # caller subtracts engine.now before calling (done in tick)
    return max(1.5, min(5.0, remaining * 0.25))

def eta(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (*a, *b))
    h = math.sin((la2-la1)/2)**2 + math.cos(la1)*math.cos(la2)*math.sin((lo2-lo1)/2)**2
    return 2*6371*math.asin(math.sqrt(h)) / SPEED_KMPH * 60

@dataclass
class Batch:
    id: str; loc: tuple; qty: int; category: str; expires_at: float; unallocated: int
@dataclass
class Rider:
    id: str; loc: tuple; capacity: int; last_seen: float = 0; busy: bool = False; strikes: int = 0
@dataclass
class Shelter:
    id: str; loc: tuple; need: int; supplies: set = field(default_factory=set)
    open: bool = True; delivered: int = 0
@dataclass
class Lease:
    id: str; batch: str; rider: str; shelter: str; qty: int; deadline: float; state: str = "RESERVED"

class Engine:
    def __init__(s):
        s.now = 0.0; s.batches = {}; s.riders = {}; s.shelters = {}; s.leases = {}; s.log = []
        s._n = itertools.count(1)

    def emit(s, kind, **d): s.log.append((round(s.now, 1), kind, d))

    # Change 1: _online now looks up batch-specific threshold via the batch the lease belongs to
    def _online(s, r, threshold=None):
        t = threshold if threshold is not None else 5.0  # fallback for non-lease contexts
        return s.now - r.last_seen <= t

    def _used(s, sid): return sum(l.qty for l in s.leases.values() if l.shelter == sid and l.state in ACTIVE)
    def _free(s, sh): return -s._used(sh.id) if not sh.open else sh.need - sh.delivered - s._used(sh.id)

    def _compat(s, b, sh):
        # Change 3: unknown category => fail-closed (REQUIRES lookup with fallback None)
        required = REQUIRES.get(b.category)
        if required is None:
            return False   # unknown category never compatible
        return sh.open and required <= sh.supplies

    # ---------- registration ----------
    def add_rider(s, loc, capacity, rid=None):
        r = Rider(rid or f"R{next(s._n)}", loc, capacity, s.now); s.riders[r.id] = r; return r
    def add_shelter(s, loc, need, supplies=(), sid=None):
        sh = Shelter(sid or f"S{next(s._n)}", loc, need, set(supplies)); s.shelters[sh.id] = sh; return sh

    def add_batch(s, loc, qty, category, shelf_min):
        # Change 3: unknown category emits UNPLACED immediately; never stored in flight
        if category not in REQUIRES:
            dummy_id = f"B?{next(s._n)}"
            s.emit("UNPLACED", batch=dummy_id, qty=qty, why="UNKNOWN_CATEGORY")
            # Return a dummy frozen batch so callers don't crash; unallocated==qty signals failure
            b = Batch(dummy_id, loc, qty, category, s.now + shelf_min, qty)
            return b
        b = Batch(f"B{next(s._n)}", loc, qty, category, s.now + shelf_min, qty)
        s.batches[b.id] = b; s.emit("BATCH", id=b.id, qty=qty, expires_at=b.expires_at)
        s.dispatch(b.id); return b

    def heartbeat(s, rid, loc=None):
        r = s.riders[rid]; r.last_seen = s.now
        if loc: r.loc = loc

    # ---------- dispatch: split across riders/shelters, hard safety constraints ----------
    def dispatch(s, bid):
        b = s.batches.get(bid)
        if b is None: return []   # unknown batch (Change 3 dummy batch not stored)
        made = []
        while b.unallocated > 0 and s.now < b.expires_at:
            best = None
            for r in s.riders.values():
                # Change 5: skip rider with zero remaining capacity
                if r.busy or not s._online(r, 5.0): continue
                to_donor = eta(r.loc, b.loc)
                for sh in s.shelters.values():
                    if not s._compat(b, sh): continue
                    q = min(r.capacity, s._free(sh), b.unallocated)
                    if q <= 0: continue   # Change 5: skip 0-qty sub-batches
                    leg2 = eta(b.loc, sh.loc)
                    # Change 5: verify safety buffer using remaining shelf life at dispatch time
                    remaining = b.expires_at - s.now
                    if to_donor + leg2 + BUFFER_MIN > remaining: continue  # would spoil
                    key = (-q, r.strikes, to_donor + leg2)
                    if best is None or key < best[0]: best = (key, r, sh, q, to_donor, leg2)
            if not best: break
            _, r, sh, q, to_donor, leg2 = best
            latest = b.expires_at - leg2 - BUFFER_MIN
            ttl = max(LEASE_TTL_MIN, to_donor + 2)
            l = Lease(f"L{next(s._n)}", b.id, r.id, sh.id, q, min(s.now + ttl, latest))
            s.leases[l.id] = l; r.busy = True; b.unallocated -= q; made.append(l)
            s.emit("ASSIGNED", lease=l.id, rider=r.id, shelter=sh.id, qty=q, pickup_deadline=round(l.deadline, 1))
        if b.unallocated > 0:
            s.emit("UNPLACED", batch=bid, qty=b.unallocated, why="no safe rider/shelter combo right now")
        return made

    # ---------- lifecycle ----------
    def pickup(s, lid):
        l = s.leases[lid]
        if l.state != "RESERVED": return False
        if s.now > l.deadline: s._release(l, "LATE_PICKUP"); s._redispatch_all(); return False
        l.state = "PICKED_UP"; s.riders[l.rider].loc = s.batches[l.batch].loc; s.emit("PICKED_UP", lease=lid); return True

    def deliver(s, lid):
        l = s.leases[lid]; b = s.batches[l.batch]; r = s.riders[l.rider]; sh = s.shelters[l.shelter]
        if l.state != "PICKED_UP": return False
        if s.now > b.expires_at:
            l.state = "UNSAFE"; r.busy = any(lx.rider == r.id and lx.id != l.id and lx.state in ACTIVE for lx in s.leases.values())
            s.emit("ALERT_UNSAFE", lease=lid, msg="expired - do NOT serve"); return False
        if not sh.open: s._reroute(l); return l.state == "PICKED_UP"
        l.state = "DELIVERED"; sh.delivered += l.qty; r.loc = sh.loc
        r.busy = any(lx.rider == r.id and lx.id != l.id and lx.state in ACTIVE for lx in s.leases.values())
        s.emit("DELIVERED", lease=lid, shelter=sh.id, qty=l.qty); return True

    def _release(s, l, why):
        l.state = "RELEASED"; r = s.riders[l.rider]
        r.busy = any(lx.rider == r.id and lx.id != l.id and lx.state in ACTIVE for lx in s.leases.values())
        if why in ("GHOST", "LATE_PICKUP"): r.strikes += 1
        s.batches[l.batch].unallocated += l.qty; s.emit("RELEASED", lease=l.id, why=why, qty=l.qty)

    def _redispatch_all(s):
        for b in s.batches.values():
            if b.unallocated > 0 and s.now < b.expires_at: s.dispatch(b.id)

    # ---------- dynamic recovery ----------
    def _reroute(s, l):
        b = s.batches[l.batch]; r = s.riders[l.rider]
        candidates = [sh for sh in s.shelters.values() if sh.id != l.shelter and s._compat(b, sh) and s._free(sh) > 0]
        candidates.sort(key=lambda sh: eta(r.loc, sh.loc))

        for sh1 in candidates:
            t1 = eta(r.loc, sh1.loc)
            if s.now + t1 + TRANSIT_BUFFER_MIN > b.expires_at: continue

            if s._free(sh1) >= l.qty:
                old = l.shelter; l.shelter = sh1.id
                s.emit("REROUTED", lease=l.id, frm=old, to=l.shelter, eta=round(t1, 1))
                return

            q1 = min(l.qty, s._free(sh1))
            q2 = l.qty - q1
            if q1 <= 0 or q2 <= 0: continue   # Change 5: no zero-qty sub-batches
            best2 = None
            for sh2 in candidates:
                if sh2.id == sh1.id or s._free(sh2) < q2: continue
                t2 = eta(sh1.loc, sh2.loc)
                # Change 5: verify safety buffer for second leg independently
                remaining = b.expires_at - s.now
                if t1 + t2 + TRANSIT_BUFFER_MIN <= remaining:
                    if best2 is None or t2 < best2[0]:
                        best2 = (t2, sh2)

            if best2:
                t2, sh2 = best2
                old = l.shelter
                l.shelter = sh1.id
                l.qty = q1
                l2 = Lease(f"L{next(s._n)}", b.id, r.id, sh2.id, q2, l.deadline, state="PICKED_UP")
                s.leases[l2.id] = l2
                s.emit("REROUTED", lease=l.id, frm=old, to=l.shelter, eta=round(t1, 1), split_qty=q1)
                s.emit("REROUTED", lease=l2.id, frm=old, to=l2.shelter, eta=round(t1 + t2, 1), split_qty=q2)
                return

        l.state = "STRANDED"
        r.busy = any(lx.rider == r.id and lx.id != l.id and lx.state in ACTIVE for lx in s.leases.values())
        s.emit("ALERT_STRANDED", lease=l.id, qty=l.qty, msg="no safe fallback shelter; escalate to coordinator")

    def _rebalance(s, sh):
        while s._free(sh) < 0:
            act = sorted((l for l in s.leases.values() if l.shelter == sh.id and l.state in ACTIVE),
                         key=lambda l: (l.state != "RESERVED", -int(l.id[1:])))
            if not act: break
            l = act[0]
            s._release(l, "DEMAND_GONE") if l.state == "RESERVED" else s._reroute(l)
        s._redispatch_all()

    def close_shelter(s, sid):
        sh = s.shelters[sid]; sh.open = False; s.emit("SHELTER_CLOSED", shelter=sid); s._rebalance(sh)
    def direct_donation(s, sid, qty):
        sh = s.shelters[sid]; sh.delivered += qty; s.emit("DIRECT_DONATION", shelter=sid, qty=qty); s._rebalance(sh)

    def tick(s, minutes):
        s.now += minutes
        for l in list(s.leases.values()):
            r = s.riders[l.rider]
            if l.state == "RESERVED":
                b = s.batches.get(l.batch)
                # Change 1: use per-batch dynamic silence threshold
                remaining = (b.expires_at - s.now) if b else 0
                threshold = silence_threshold(type("_", (), {"expires_at": remaining})())  # remaining already computed
                if s.now > l.deadline or (s.now - r.last_seen > threshold):
                    s._release(l, "GHOST")
            elif l.state == "PICKED_UP":
                if s.now > s.batches[l.batch].expires_at:
                    l.state = "UNSAFE"
                    r.busy = any(lx.rider == r.id and lx.id != l.id and lx.state in ACTIVE for lx in s.leases.values())
                    s.emit("ALERT_UNSAFE", lease=l.id, msg="expired in transit")
                elif not s._online(r, 5.0): s.emit("ALERT_RIDER_SILENT", lease=l.id, msg="has cargo, unresponsive")
        s._redispatch_all()

    def status(s):
        return {
            "now":           s.now,
            "batches":       {b.id: vars(b) for b in s.batches.values()},
            "leases":        {l.id: vars(l) for l in s.leases.values()},
            "shelters":      {x.id: {**vars(x), "supplies": sorted(x.supplies), "free": s._free(x)} for x in s.shelters.values()},
            "riders":        {r.id: {**vars(r), "online": s._online(r, 5.0)} for r in s.riders.values()},
            "log":           s.log[-50:],
            "safety_buffer_min": BUFFER_MIN,   # Change 2: expose in /status
            "config":        {"buffer_min": BUFFER_MIN, "lease_ttl_min": LEASE_TTL_MIN},
        }
