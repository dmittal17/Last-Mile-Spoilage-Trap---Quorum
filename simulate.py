"""Judge-scenario tests. Run: python simulate.py"""
from engine import Engine
P = (18.52, 73.85)
def at(dlat=0, dlon=0): return (P[0]+dlat, P[1]+dlon)
def kinds(e): return [k for _, k, _ in e.log]

def t_split_180():
    e = Engine()
    for i in range(7): e.add_rider(at(0.002*i, 0.001), 40)
    for n, (x, y) in zip((50, 60, 70), ((0.01, 0), (0, 0.02), (0.02, 0.01))): e.add_shelter(at(x, y), n)
    b = e.add_batch(P, 180, "cooked", 75)
    assert b.unallocated == 0 and sum(l.qty for l in e.leases.values()) == 180
    assert all(l.qty <= 40 for l in e.leases.values())
    for s in e.shelters.values(): assert e._used(s.id) <= s.need
    print("PASS split 180 meals ->", len(e.leases), "riders, no shelter overfilled")

def t_tight_expiry():
    e = Engine(); e.add_rider(P, 40); e.add_shelter(at(0.03, 0.03), 100)
    e.add_batch(P, 100, "cooked", 12)
    assert "UNPLACED" in kinds(e) and not e.leases
    print("PASS 12-min shelf life + far shelter -> refused (no spoiled delivery)")

def t_ghost():
    e = Engine(); a = e.add_rider(at(0.001), 40); b = e.add_rider(at(0.002), 40); e.add_shelter(at(0.01), 40)
    e.add_batch(P, 40, "cooked", 75)
    ghost = next(iter(e.leases.values())).rider; other = b.id if ghost == a.id else a.id
    e.tick(3); e.heartbeat(other); e.tick(3); e.heartbeat(other)   # ghost goes silent
    live = [l for l in e.leases.values() if l.state == "RESERVED"]
    assert "RELEASED" in kinds(e) and len(live) == 1 and live[0].rider == other
    print(f"PASS ghost reservation by {ghost} auto-released and reassigned to {other} in 6 min")

def t_shelter_closes_in_transit():
    e = Engine(); r = e.add_rider(at(0.001), 40); A = e.add_shelter(at(0.02), 40, sid="A"); e.add_shelter(at(0.03, 0.01), 40, sid="B")
    e.add_batch(P, 40, "cooked", 75); l = next(iter(e.leases.values()))
    e.pickup(l.id); e.heartbeat(r.id, at(0.01)); e.tick(2); e.heartbeat(r.id, at(0.01))
    sh = l.shelter; e.direct_donation(sh, 40)           # citizen fills the shelter mid-journey
    assert l.shelter != sh and l.state == "PICKED_UP", e.log
    e.close_shelter(l.shelter)                           # and now the fallback closes too
    assert l.state == "STRANDED" and "ALERT_STRANDED" in kinds(e)
    print("PASS demand filled mid-transit -> rerouted; fallback closed -> STRANDED escalated")

def t_compatibility():
    e = Engine(); e.add_rider(P, 40); e.add_shelter(at(0.01), 40, supplies={"fuel"})   # no clean water
    e.add_batch(P, 40, "formula", 120)
    assert not e.leases and "UNPLACED" in kinds(e)
    print("PASS infant formula refused for shelter without clean water")

if __name__ == "__main__":
    for t in (t_split_180, t_tight_expiry, t_ghost, t_shelter_closes_in_transit, t_compatibility): t()
    print("ALL SCENARIOS PASS")
