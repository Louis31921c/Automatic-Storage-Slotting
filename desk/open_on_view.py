"""Open the input window whenever the status spreadsheet is opened.

Spreadsheet apps drop a lock file next to the document while it is open:
LibreOffice `.~lock.<name>#`, Excel `~$<name>`. This watches data/out/ for a new lock file of
warehouse_status.* (xlsx, or a copy saved as ods) and starts desk/warehouse_window.py; the window is
single-instance, so a second open just brings it to the front. Started by scripts/dev.sh.
"""
import logging
import subprocess
import sys
import threading
import time
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "out"
STEM = "warehouse_status"
WINDOW = Path(__file__).resolve().parent / "warehouse_window.py"
log = logging.getLogger("open_on_view")


def is_open_marker(name: str, stem: str = STEM) -> bool:
    """Lock file a spreadsheet app creates while `<stem>.<ext>` is open."""
    if name.startswith(".~lock.") and name.endswith("#"):
        doc = name[len(".~lock."):-1]
    elif name.startswith("~$"):
        doc = name[2:]
    else:
        return False
    return Path(doc).stem == stem


class Handler(FileSystemEventHandler):
    def __init__(self, launch, min_gap_s: float = 3.0):
        self.launch, self.min_gap_s, self.last = launch, min_gap_s, 0.0
        self.lock = threading.Lock()

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.is_directory or event.event_type not in ("created", "moved"):
            return
        name = Path(str(event.dest_path if event.event_type == "moved" else event.src_path)).name
        if not is_open_marker(name):
            return
        with self.lock:                                  # editors may touch the lock twice
            now = time.monotonic()
            if now - self.last < self.min_gap_s:
                return
            self.last = now
        log.info("spreadsheet opened (%s): showing the input window", name)
        self.launch()


def launch_window() -> None:
    subprocess.Popen([sys.executable, str(WINDOW)], start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    OUT.mkdir(parents=True, exist_ok=True)
    obs = Observer()
    obs.schedule(Handler(launch_window), str(OUT), recursive=False)
    obs.start()
    log.info("watching %s for %s.* being opened", OUT, STEM)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    obs.stop()
    obs.join()


if __name__ == "__main__":
    main()
