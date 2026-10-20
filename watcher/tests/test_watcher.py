import pytest

from watcher.config import load_config
from watcher.main import is_spreadsheet, write_heartbeat


def test_repo_config_loads():
    cfg = load_config()
    assert {w.name for w in cfg.watch} == {"supplier", "customer", "stock", "mixed"}
    assert all(w.path.is_absolute() for w in cfg.watch)


@pytest.mark.parametrize("name,expected", [
    ("PO_2026_10.xlsx", True),
    ("orders.CSV", True),
    ("ddt_scan.pdf", True),
    ("old.xls", False),
    ("~$PO_2026_10.xlsx", False),
    (".~lock.PO_2026_10.xlsx#", False),
    ("notes.txt", False),
])
def test_is_spreadsheet(name, expected):
    assert is_spreadsheet(f"/inbox/{name}") is expected


def test_write_heartbeat(tmp_path):
    hb = tmp_path / "run" / "watcher.heartbeat"
    write_heartbeat(hb)
    assert hb.exists()


# ---------------------------------------------------------------- debounce + dispatch
import os  # noqa: E402

from watcher.main import Debouncer, dispatch  # noqa: E402


def test_debounce_waits_for_stable_file(tmp_path):
    f = tmp_path / "po.xlsx"
    f.write_bytes(b"a")
    d = Debouncer(debounce_s=2.0)
    d.add("modified", str(f))
    assert d.ready(100.0) == []                 # first sighting starts the clock
    assert d.ready(101.0) == []
    f.write_bytes(b"ab")                        # still being written
    assert d.ready(102.5) == []
    assert d.ready(103.0) == []
    assert [p.path for p in d.ready(104.6)] == [str(f)]


def test_deleted_is_immediate_and_move_keeps_dest(tmp_path):
    d = Debouncer(debounce_s=2.0)
    d.add("deleted", "/x/a.xlsx")
    assert d.ready(0)[0].event == "deleted"
    g = tmp_path / "b.xlsx"
    g.write_bytes(b"x")
    d.add("moved", "/x/old.xlsx", str(g))
    d.ready(10.0)
    p = [p for p in d.ready(13.0) if p.event == "moved"][0]
    assert p.path == "/x/old.xlsx" and p.dest == str(g)


class _Cfg:
    engine_url = "http://engine"


def test_dispatch_backs_off_on_lock_then_succeeds(tmp_path):
    f = tmp_path / "po.xlsx"
    f.write_bytes(b"a")
    os.utime(f, (1, 1))
    d = Debouncer(debounce_s=0.0)
    d.add("modified", str(f))
    d.ready(0.0)
    answers = iter([(423, "locked"), (200, "{}")])
    calls = []

    def send(url, p):
        calls.append(p.path)
        return next(answers)

    dispatch(_Cfg, d, 1.0, send)
    assert d.pending and calls == [str(f)]
    dispatch(_Cfg, d, 1.5, send)                # backoff: not retried yet
    assert len(calls) == 1
    dispatch(_Cfg, d, 4.0, send)
    assert len(calls) == 2 and not d.pending


def test_dispatch_survives_engine_down(tmp_path):
    import urllib.error
    d = Debouncer(debounce_s=0.0)
    d.add("deleted", "/x/a.xlsx")

    def send(url, p):
        raise urllib.error.URLError("refused")

    dispatch(_Cfg, d, 0.0, send)
    assert d.pending["/x/a.xlsx"].attempts == 1
