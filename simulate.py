"""Judge-scenario tests. Run: python simulate.py"""
from engine import Engine, eta, BUFFER_MIN, silence_threshold
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
    # Change 1: dynamic threshold — with 75-min batch threshold is 5.0 min; ghost after 6 min
    e = Engine(); a = e.add_rider(at(0.001), 40); b = e.add_rider(at(0.002), 40)
    e.add_shelter(at(0.01), 40)
    batch = e.add_batch(P, 40, "cooked", 75)
    ghost = next(iter(e.leases.values())).rider; other = b.id if ghost == a.id else a.id
    e.tick(3); e.heartbeat(other); e.tick(3); e.heartbeat(other)   # ghost goes silent; 6 min elapsed
    live = [l for l in e.leases.values() if l.state == "RESERVED"]
    assert "RELEASED" in kinds(e), "ghost lease not released"
    # Change 1: assert reassigned rider satisfies safety buffer; if not, batch must be UNPLACED
    sh = e.shelters[next(iter(e.shelters))]
    remaining = batch.expires_at - e.now
    leg1 = eta(e.riders[other].loc, batch.loc)
    leg2 = eta(batch.loc, sh.loc)
    can_deliver = leg1 + leg2 + BUFFER_MIN <= remaining
    if can_deliver:
        assert len(live) == 1 and live[0].rider == other, "expected reassignment to other rider"
        print(f"PASS ghost reservation by {ghost} auto-released and reassigned to {other} in 6 min")
    else:
        assert "UNPLACED" in kinds(e), "reassigned rider can't deliver safely; expected UNPLACED"
        print(f"PASS ghost reservation by {ghost} auto-released; remaining time too short -> UNPLACED")

def t_reroute_single_fallback():
    # Change 4a: reroute to a SINGLE fallback succeeds
    e = Engine(); r = e.add_rider(at(0.001), 40)
    A = e.add_shelter(at(0.02), 40, sid="A"); e.add_shelter(at(0.03, 0.01), 40, sid="B")
    e.add_batch(P, 40, "cooked", 75); l = next(iter(e.leases.values()))
    e.pickup(l.id); e.heartbeat(r.id, at(0.01)); e.tick(2); e.heartbeat(r.id, at(0.01))
    sh_original = l.shelter
    e.direct_donation(sh_original, 40)    # citizen fills primary shelter mid-journey
    assert l.shelter != sh_original, f"lease should have rerouted away from {sh_original}"
    assert l.state == "PICKED_UP", "lease should still be in flight after single reroute"
    assert "REROUTED" in kinds(e)
    print("PASS demand filled mid-transit -> rerouted to single fallback shelter successfully")

def t_both_shelters_close_stranded():
    # Change 4b: primary AND fallback both close → ALERT_STRANDED, cargo qty preserved
    e = Engine(); r = e.add_rider(at(0.001), 40)
    A = e.add_shelter(at(0.02), 40, sid="A"); e.add_shelter(at(0.03, 0.01), 40, sid="B")
    e.add_batch(P, 40, "cooked", 75); l = next(iter(e.leases.values()))
    e.pickup(l.id); e.heartbeat(r.id, at(0.01)); e.tick(2); e.heartbeat(r.id, at(0.01))
    sh_original = l.shelter; cargo_qty = l.qty
    e.direct_donation(sh_original, 40)   # fills primary -> rerouted to B
    assert l.shelter != sh_original, "should have rerouted first"
    e.close_shelter(l.shelter)           # now close the fallback too
    assert l.state == "STRANDED", f"expected STRANDED, got {l.state}"
    assert "ALERT_STRANDED" in kinds(e)
    stranded_evt = next(d for _, k, d in e.log if k == "ALERT_STRANDED")
    assert stranded_evt["qty"] == cargo_qty, f"cargo qty {cargo_qty} must be logged in ALERT_STRANDED"
    print(f"PASS primary AND fallback closed -> ALERT_STRANDED with qty={cargo_qty} logged")

def t_shelter_closes_in_transit():
    # Legacy combined test kept for backward compat — delegates to the two new tests
    t_reroute_single_fallback()
    t_both_shelters_close_stranded()
    print("PASS demand filled mid-transit -> rerouted; fallback closed -> STRANDED escalated")

def t_compatibility():
    e = Engine(); e.add_rider(P, 40); e.add_shelter(at(0.01), 40, supplies={"fuel"})   # no clean water
    e.add_batch(P, 40, "formula", 120)
    assert not e.leases and "UNPLACED" in kinds(e)
    print("PASS infant formula refused for shelter without clean water")

def t_medical_requires_cold_chain():
    # Change 3: medical now fails-closed without cold_chain
    e = Engine(); e.add_rider(P, 40); e.add_shelter(at(0.01), 40)  # no cold_chain
    e.add_batch(P, 40, "medical", 120)
    assert not e.leases and "UNPLACED" in kinds(e)
    print("PASS medical refused for shelter without cold_chain")

def t_unknown_category_unplaced():
    # Change 3: unknown category must emit UNPLACED with UNKNOWN_CATEGORY, never assign
    e = Engine(); e.add_rider(P, 40); e.add_shelter(at(0.01), 40)
    b = e.add_batch(P, 40, "spices", 60)
    assert not e.leases, "unknown category must never produce a lease"
    unplaced = [d for _, k, d in e.log if k == "UNPLACED"]
    assert any(d.get("why") == "UNKNOWN_CATEGORY" for d in unplaced), f"expected UNKNOWN_CATEGORY reason, got {unplaced}"
    print("PASS unknown category 'spices' -> UNPLACED with UNKNOWN_CATEGORY reason")

def t_split_tight_expiry():
    # Change 4 / 5: tight 20-min shelf life, two riders; far rider's transit exceeds budget
    # Geometry (verified): donor=P, shelter=at(0.005,0.005)
    #   R1 at(0.001,0.001): leg1=0.46m, leg2=2.30m, total=12.76m  <=20 -> SAFE
    #   R2 at(0.05, 0.05):  leg1=22.98m, leg2=2.30m, total=35.28m > 20 -> REFUSED
    e = Engine()
    r1 = e.add_rider(at(0.001, 0.001), 40)            # close rider -> fits within 20-min shelf
    r2 = e.add_rider(at(0.05, 0.05), 40)              # far rider  -> transit + buffer > shelf life
    sh = e.add_shelter(at(0.005, 0.005), 80)          # shelter has room for 80
    b = e.add_batch(P, 80, "cooked", 20)              # 20-min shelf life (tight!)
    # R1 total = 0.46 + 2.30 + 10 = 12.76 <= 20 -> ASSIGNED
    # R2 total = 22.98 + 2.30 + 10 = 35.28 > 20  -> REFUSED, 40 units UNPLACED
    assigned = [l for l in e.leases.values() if l.state == "RESERVED"]
    assert len(assigned) >= 1, "at least R1 should be assigned"
    riders_used = {l.rider for l in assigned}
    assert r1.id in riders_used, "near rider must be assigned"
    assert r2.id not in riders_used, f"far rider must be refused: {riders_used}"
    assert "UNPLACED" in kinds(e), "remaining 40 units from far rider must be UNPLACED"
    unplaced_qty = next(d["qty"] for _, k, d in e.log if k == "UNPLACED")
    assert unplaced_qty == 40, f"expected 40 UNPLACED, got {unplaced_qty}"
    print(f"PASS 20-min tight batch: R1 assigned {b.qty - b.unallocated} units, "
          f"far rider refused -> {b.unallocated} units UNPLACED")

def t_split_fallback_shelters():
    e = Engine(); r = e.add_rider(at(0.001), 40)
    A = e.add_shelter(at(0.01), 40, sid="A")
    B = e.add_shelter(at(0.015), 25, sid="B")          # capacity 25 (< 40)
    C = e.add_shelter(at(0.025, 0.01), 25, sid="C")    # capacity 25 (< 40)
    e.add_batch(P, 40, "cooked", 75); l = next(iter(e.leases.values()))
    e.pickup(l.id); e.heartbeat(r.id, at(0.005)); e.tick(2); e.heartbeat(r.id, at(0.005))
    e.direct_donation(A.id, 40)                        # A fills up mid-journey
    active = [lx for lx in e.leases.values() if lx.rider == r.id and lx.state == "PICKED_UP"]
    assert len(active) == 2 and {lx.shelter for lx in active} == {"B", "C"}
    assert sum(lx.qty for lx in active) == 40
    assert l.shelter == "B" and l.qty == 25            # nearest feasible shelter visited first
    l2 = [lx for lx in active if lx.id != l.id][0]
    assert l2.shelter == "C" and l2.qty == 15          # remainder split to second fallback
    for sid in ("B", "C"): assert e._used(sid) <= e.shelters[sid].need
    assert e.deliver(l.id) and e.shelters["B"].delivered == 25 and r.busy
    assert e.deliver(l2.id) and e.shelters["C"].delivered == 15 and not r.busy
    print("PASS demand filled mid-transit -> split load across two fallback shelters (B: 25, C: 15)")

if __name__ == "__main__":
    tests = (
        t_split_180,
        t_tight_expiry,
        t_ghost,
        t_shelter_closes_in_transit,       # legacy wrapper (runs both sub-tests)
        t_reroute_single_fallback,         # Change 4a explicit
        t_both_shelters_close_stranded,    # Change 4b explicit
        t_compatibility,
        t_medical_requires_cold_chain,     # Change 3
        t_unknown_category_unplaced,       # Change 3
        t_split_tight_expiry,              # Change 4 / Change 5
        t_split_fallback_shelters,
    )
    for t in tests: t()
    print("ALL SCENARIOS PASS")
