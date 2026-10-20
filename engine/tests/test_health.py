import os
import time

from app.contract import CONTRACT_VERSION
from app.settings import settings


def test_health_degraded_without_watcher(client):
    body = client.get("/health").json()
    assert body["contract_version"] == CONTRACT_VERSION
    assert body["db"]["ok"] is True
    assert body["watcher"]["ok"] is False
    assert body["status"] == "degraded"


def test_health_ok_with_fresh_heartbeat(client):
    settings.run_dir.mkdir(parents=True, exist_ok=True)
    (settings.run_dir / "watcher.heartbeat").touch()
    assert client.get("/health").json()["status"] == "ok"


def test_health_stale_heartbeat(client):
    settings.run_dir.mkdir(parents=True, exist_ok=True)
    hb = settings.run_dir / "watcher.heartbeat"
    hb.touch()
    old = time.time() - settings.watcher_stale_after_s - 5
    os.utime(hb, (old, old))
    assert client.get("/health").json()["watcher"]["ok"] is False


def test_storage_imported_with_ship_slots(client):
    stats = client.get("/admin/stats").json()
    assert stats["cases"]["FREE"] == 8 + 3 + 1 + 1 + 2


def test_map(client):
    m = client.get("/map").json()
    assert len(m["racks"]) == 3 and m["docks"] and len(m["floor"]) == 1
