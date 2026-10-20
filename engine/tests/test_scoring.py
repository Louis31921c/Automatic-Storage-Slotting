from app import scoring

P = scoring.Policy(overhang_m=0.05, short_dwell_days=7, max_dock_dist_m=100)


def slot(level=1, max_level=2, w=1.337, d=1.1, h=1.68, dock=10.0, cid=None, level_max=1250):
    return scoring.Slot(cid or f"A1S1L{level}P1", w, d, h, w * d * h, 1250, level_max, level, max_level, dock)


def item(l=1.2, w=0.8, h=1.144, kg=325, frag=0, dwell=14):
    return scoring.Item(l, w, h, kg, frag, dwell)


def test_fits_with_rotation_and_overhang():
    assert scoring.fits(item(), slot(), 0.0)                        # rotated: 1.2 along 1.337, 0.8 deep
    sq = slot(w=1.1, d=1.1)
    assert scoring.fits(item(), sq, 0.05)                           # 1.2 <= 1.1 + 2 x 0.05
    assert not scoring.fits(item(), sq, 0.0)
    assert not scoring.fits(item(h=1.7), slot(), 0.05)              # never overhangs in height


def test_hard_weight_limits():
    assert scoring.hard_ok(item(kg=600), slot(), 600, P)
    assert not scoring.hard_ok(item(kg=700), slot(), 600, P)        # 1300 > 1250 on the level
    assert not scoring.hard_ok(item(kg=1300), slot(), 0, P)


def test_components_in_range_and_fragile_prefers_low():
    for lv in (1, 2):
        c = scoring.components(item(frag=1), slot(level=lv), P)
        assert all(0 <= v <= 1 for v in c.values())
    low = scoring.components(item(frag=1), slot(level=1), P)["fragility"]
    high = scoring.components(item(frag=1), slot(level=2), P)["fragility"]
    assert low > high


def test_short_dwell_prefers_access_long_dwell_the_opposite():
    near, far = slot(dock=0), slot(dock=100)
    s_short = scoring.components(item(dwell=2), near, P)["dwell"], scoring.components(item(dwell=2), far, P)["dwell"]
    s_long = scoring.components(item(dwell=60), near, P)["dwell"], scoring.components(item(dwell=60), far, P)["dwell"]
    assert s_short[0] > s_short[1] and s_long[0] < s_long[1]


def test_rank_tie_break_lowest_level_then_dock_then_id():
    a, b, c = slot(level=2, cid="A1S1L2P1"), slot(level=1, dock=5, cid="A1S10L1P1"), slot(level=1, dock=5, cid="A1S2L1P1")
    order = sorted([a, b, c], key=lambda s: scoring.rank_key(0.5, s))
    assert [s.id for s in order] == ["A1S2L1P1", "A1S10L1P1", "A1S1L2P1"]


def test_weighted_sum():
    assert scoring.total({"a": 1.0, "b": 0.0}, {"a": 75, "b": 25}) == 0.75
