"""Watcher service: watches the Excel folders, debounces, and hands stable files to the engine.

Separate process from the engine. Never crashes on a locked/half-written file: it waits for a stable
mtime + size, then retries with backoff while the engine answers 423 (locked) or is unreachable.
"""
import json
import logging
import signal
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver

from .config import ROOT, LiveSyncConfig, load_config

log = logging.getLogger("watcher")
HEARTBEAT = ROOT / "data" / "run" / "watcher.heartbeat"
SPREADSHEET_SUFFIXES = {".xlsx", ".xlsm", ".csv", ".pdf"}   # same as engine tables.SUPPORTED
MAX_BACKOFF_S = 60.0


def is_spreadsheet(path: str) -> bool:
    p = Path(path)
    # ~$name.xlsx = Office lock file, .~lock.name# = LibreOffice lock file
    return p.suffix.lower() in SPREADSHEET_SUFFIXES and not p.name.startswith(("~$", ".~lock"))


@dataclass
class Pending:
    event: str                       # modified | deleted | moved
    path: str
    dest: str | None = None
    stat: tuple[float, int] | None = None
    stable_since: float = 0.0
    attempts: int = 0
    next_try: float = 0.0


@dataclass
class Debouncer:
    """Collects fs events per path; releases a path once its mtime + size held still for `debounce_s`."""
    debounce_s: float
    pending: dict[str, Pending] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def add(self, event: str, path: str, dest: str | None = None) -> None:
        with self.lock:
            if event == "moved":
                self.pending.pop(path, None)
                self.pending[dest or path] = Pending("moved", path, dest)
            elif event == "deleted":
                self.pending[path] = Pending("deleted", path)
            else:
                prev = self.pending.get(path)
                if prev is None or prev.event == "deleted":
                    self.pending[path] = Pending("modified", path)
                else:                                   # keep a pending move, restart the stability clock
                    prev.stat, prev.stable_since = None, 0.0

    def ready(self, now: float) -> list[Pending]:
        out = []
        with self.lock:
            for key, p in list(self.pending.items()):
                if now < p.next_try:
                    continue
                if p.event == "deleted":
                    out.append(p)
                    continue
                target = Path(p.dest or p.path)
                try:
                    st = target.stat()
                except FileNotFoundError:
                    self.pending.pop(key)              # vanished again: a later 'deleted' event will follow
                    continue
                cur = (st.st_mtime, st.st_size)
                if cur != p.stat:
                    p.stat, p.stable_since = cur, now
                elif now - p.stable_since >= self.debounce_s:
                    out.append(p)
        return out

    def done(self, p: Pending) -> None:
        with self.lock:
            key = p.dest or p.path
            if self.pending.get(key) is p:
                self.pending.pop(key)

    def retry(self, p: Pending, now: float) -> None:
        p.attempts += 1
        p.next_try = now + min(2 ** p.attempts, MAX_BACKOFF_S)


class Handler(FileSystemEventHandler):
    def __init__(self, source: str, deb: Debouncer):
        self.source, self.deb = source, deb

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.is_directory or event.event_type in ("opened", "closed_no_write"):
            return
        src = str(event.src_path)
        if event.event_type == "moved":
            dst = str(event.dest_path)
            if is_spreadsheet(dst):
                # editor save = temp file renamed onto the target -> plain modification
                self.deb.add("moved" if is_spreadsheet(src) else "modified", src if is_spreadsheet(src) else dst,
                             dst if is_spreadsheet(src) else None)
            elif is_spreadsheet(src):
                self.deb.add("deleted", src)
            return
        if not is_spreadsheet(src):
            return
        self.deb.add("deleted" if event.event_type == "deleted" else "modified", src)
        log.debug("%s %s %s", self.source, event.event_type, src)


def post_event(engine_url: str, p: Pending, timeout: float = 120.0) -> tuple[int, str]:
    body = json.dumps({"event": p.event, "path": p.path, "dest_path": p.dest}).encode()
    req = urllib.request.Request(f"{engine_url}/ingest/file", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def dispatch(cfg: LiveSyncConfig, deb: Debouncer, now: float, send=post_event) -> None:
    for p in deb.ready(now):
        try:
            code, body = send(cfg.engine_url, p)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            log.warning("engine unreachable (%s), retry %s", e, p.path)
            deb.retry(p, now)
            continue
        if code == 200:
            log.info("ingested %s %s -> %s", p.event, p.dest or p.path, body)
            deb.done(p)
        elif code in (423, 503, 500, 502, 504):          # locked / engine busy: back off, never crash
            log.warning("engine %s for %s, retry #%d", code, p.path, p.attempts + 1)
            deb.retry(p, now)
        else:                                            # 4xx: bad file; engine logged it, drop
            log.error("engine %s for %s: %s", code, p.path, body[:300])
            deb.done(p)


def write_heartbeat(path: Path = HEARTBEAT) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


def start_observers(cfg: LiveSyncConfig, deb: Debouncer) -> list:
    observers = []
    for w in cfg.watch:
        w.path.mkdir(parents=True, exist_ok=True)
        obs = Observer() if w.mode == "event" else PollingObserver(timeout=w.poll_interval_s)
        obs.schedule(Handler(w.name, deb), str(w.path), recursive=False)
        obs.start()
        observers.append(obs)
        for f in sorted(w.path.iterdir()):               # catch up on files changed while we were down
            if f.is_file() and is_spreadsheet(str(f)):
                deb.add("modified", str(f))
        log.info("watching %s (%s) -> %s", w.name, w.mode, w.path)
    return observers


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = load_config()
    cfg.dead_letter_path.mkdir(parents=True, exist_ok=True)
    deb = Debouncer(cfg.debounce_s)
    observers = start_observers(cfg, deb)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    last_hb = 0.0
    while not stop.is_set():
        now = time.time()
        if now - last_hb >= cfg.heartbeat_s:
            write_heartbeat()
            last_hb = now
        try:
            dispatch(cfg, deb, now)
        except Exception:                                # never let the loop die
            log.exception("dispatch failed")
        stop.wait(0.5)
    for obs in observers:
        obs.stop()
        obs.join()
    log.info("stopped")


if __name__ == "__main__":
    main()
