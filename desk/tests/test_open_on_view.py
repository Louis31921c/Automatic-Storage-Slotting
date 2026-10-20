import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from open_on_view import Handler, is_open_marker  # noqa: E402


def test_lock_names():
    assert is_open_marker(".~lock.warehouse_status.xlsx#")          # LibreOffice
    assert is_open_marker(".~lock.warehouse_status.ods#")           # user's ods copy
    assert is_open_marker("~$warehouse_status.xlsx")                # Excel
    assert not is_open_marker("warehouse_status.xlsx")
    assert not is_open_marker(".~lock.other.xlsx#")
    assert not is_open_marker(".tmp_abc.xlsx")


def test_handler_debounces():
    calls = []
    h = Handler(lambda: calls.append(1), min_gap_s=60)
    ev = SimpleNamespace(is_directory=False, event_type="created", src_path="/o/.~lock.warehouse_status.xlsx#")
    h.on_any_event(ev)
    h.on_any_event(ev)
    h.on_any_event(SimpleNamespace(is_directory=False, event_type="deleted", src_path=ev.src_path))
    assert calls == [1]
