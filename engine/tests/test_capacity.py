"""Height-aware fit, scarcity (keep) and shared cases."""
import random

from app import scoring
from conftest import ingest, pallet_line, units

W = {"size_fit": 25, "dwell": 25, "fragility": 25, "weight": 25}
P = scoring.Policy(max_dock_dist_m=100)


def cs(cid, h, w=1.34, d=1.1, shared=False, level=1, dock=10.0, vol=None):
    v = vol if vol is not None else w * d * h
    return scoring.CaseState(scoring.Slot(cid, w, d, h, v, 1250, 1250, level, 2, dock), "pallet", cid, shared)


def test_low_pallet_takes_low_slot_and_leaves_tall_one():
    low = scoring.Item(1.2, 0.8, 1.0, 300, 0, 14)
    states = [cs("A1S1L1P1", 1.68), cs("A2S1L1P1", 1.15, d=2.3)] + [cs(f"A3S{i}L1P1", 1.15, d=2.3) for i in range(2, 10)]
    best = scoring.rank(low, "X", states, {}, W, P)[0][0].slot.id
    assert best.startswith("A2") or best.startswith("A3")            # not the rare 1.68 m slot


def test_tall_pallet_still_gets_tall_slot():
    tall = scoring.Item(1.2, 0.8, 1.4, 300, 0, 14)
    states = [cs("A1S1L1P1", 1.68), cs("A2S1L1P1", 1.15)]
    assert [r[0].slot.id for r in scoring.rank(tall, "X", states, {}, W, P)] == ["A1S1L1P1"]


def test_keep_score():
    hs = sorted([1.15] * 9 + [1.68])
    assert scoring.keep_score(1.15, 1.15, hs, P) == 1.0
    assert scoring.keep_score(1.68, 1.15, hs, P) == 0.1                 # 1 of 10 slots that tall


def test_shared_room_limits():
    bin_ = cs("A9S1L1P1", 0.4, w=1.0, d=1.2, shared=True)
    small = scoring.Item(0.3, 0.2, 0.1, 1, 0, 14)
    assert scoring.room_ok(small, "K1", bin_, P)
    bin_.skus = {"K1", "K2", "K3"}
    bin_.used_m3 = 0.1
    assert not scoring.room_ok(small, "K4", bin_, P)                   # 4th SKU refused
    assert scoring.room_ok(small, "K2", bin_, P)                       # same SKU fine
    bin_.used_m3 = 1.0 * 1.2 * 0.4 * P.fill_factor - 0.001
    assert not scoring.room_ok(small, "K2", bin_, P)                   # volume full
    pallet = cs("A1S1L1P1", 1.68)
    pallet.skus, pallet.used_m3 = {"X"}, 1.0
    assert not scoring.room_ok(small, "X", pallet, P)                  # pallet positions never shared


def test_numpy_rank_matches_reference():
    for seed in range(4):
        rng = random.Random(seed)
        states = []
        for i in range(800):
            s = cs(f"A{rng.randint(1, 30)}S{rng.randint(1, 9)}L{rng.randint(1, 3)}P{rng.randint(1, 2)}" if i % 40
                   else f"FLOOR{i}", rng.choice([0.4, 0.69, 1.15, 1.68]), rng.choice([1.0, 1.34]),
                   rng.choice([1.1, 1.2, 2.3]), shared=rng.random() < 0.6, level=rng.randint(1, 2),
                   dock=rng.uniform(0, 100))
            if s.shared and rng.random() < 0.5:
                s.used_m3, s.used_kg, s.skus = rng.uniform(0, 0.3), rng.uniform(0, 40), {f"K{rng.randint(0, 4)}"}
            states.append(s)
        loads = {(st.shelf_id, st.slot.level): rng.uniform(0, 1200) for st in states}
        for item, sku in [(scoring.Item(0.3, 0.2, 0.1, 2, 0, 14), "K1"), (scoring.Item(1.2, 0.8, 1.0, 400, 1, 3), "Z"),
                          (scoring.Item(0.6, 0.4, 0.35, 9, 2, 30), "K3")]:
            a = scoring.rank(item, sku, states, loads, W, P, limit=None)
            b = scoring.rank_reference(item, sku, states, loads, W, P)
            assert [(x[0].slot.id, x[2], x[1]) for x in a] == [(y[0].slot.id, y[2], y[1]) for y in b]


def small_line(line_ref="50", qty=3, sku="BLT-1"):
    return {"line_ref": line_ref, "sku": sku, "qty_units": qty, "packaging": "small", "length_mm": 200,
            "width_mm": 150, "height_mm": 100, "weight_kg": 1.0}


def place_all(client, code):
    out = []
    for a in units(client, code):
        r = client.post("/slot", json={"article_id": a["id"]}).json()
        client.post(f"/articles/{a['id']}/placed")
        out.append(r["slot"])
    return out


def test_small_units_share_one_bin(client):
    ingest(client, [small_line(qty=3)])
    assert place_all(client, "PO-1-50") == ["A3S1L1P1"] * 3
    stats = client.get("/admin/stats").json()
    assert stats["cases"]["OCCUPIED"] == 1


def test_fourth_sku_does_not_enter_bin(client):
    ingest(client, [small_line(f"{k}0", 1, f"SKU-{k}") for k in range(1, 5)])
    slots = [place_all(client, f"PO-1-{k}0")[0] for k in range(1, 5)]
    assert slots[:3] == ["A3S1L1P1"] * 3 and slots[3] != "A3S1L1P1"


def test_removing_one_unit_keeps_shared_case_occupied(client):
    ingest(client, [small_line(qty=2)])
    place_all(client, "PO-1-50")
    ingest(client, [small_line(qty=1)])                              # qty reduced: one unit cancelled
    assert client.get("/admin/stats").json()["cases"]["OCCUPIED"] == 1
    assert sorted(u["state"] for u in units(client, "PO-1-50")) == ["CANCELLED", "PLACED"]


def test_pallet_positions_stay_single(client):
    ingest(client, [pallet_line(qty=2, kg=100)])
    a, b = place_all(client, "PO-1-10")
    assert a != b
