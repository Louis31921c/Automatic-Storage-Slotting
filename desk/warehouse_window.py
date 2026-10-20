"""Warehouse input window, styled as a Windows XP error message box.

  .venv/bin/python desk/warehouse_window.py              (engine must run: scripts/dev.sh)
  .venv/bin/python desk/warehouse_window.py [files...]     preload data sheets (detected automatically)
  .venv/bin/python desk/warehouse_window.py --screenshot out.png [--expanded]

- Data sheets: Ordered / In stock / Shipments (pdf, xlsx, xlsm, csv). The engine inspects each file; a
  workbook holding several datasets is one database for all of them and is imported once.
- Import copies the files into data/inbox/<folder>/; the watcher hands them to the engine, results are
  read back from the engine's event log. Re-importing an edited file updates it (keyed by business keys).
- Parameters >> : matrix weights + quick slotting parameters, applied live (engine /admin/params).
"""
import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import urllib.error
import urllib.request
from pathlib import Path
from tkinter import filedialog

sys.path.insert(0, str(Path(__file__).resolve().parent))
from routing import LABEL, ROOT, SLOTS, assign, describe, import_plan  # noqa: E402

ENGINE = os.environ.get("WH_ENGINE_URL", "http://127.0.0.1:8000")
EXPORT = ROOT / "data" / "out" / "warehouse_status.xlsx"

# Windows XP "Luna" palette
FACE = "#ECE9D8"
BORDER = "#0054E3"
TITLE_STOPS = [(0.0, "#3D95FF"), (0.12, "#0A64EE"), (0.55, "#0055E5"), (1.0, "#0042C2")]
BTN_BORDER = "#003C74"
TEXT = "#000000"
GREY = "#6D6D6D"
OK_GREEN = "#1E7B1E"
ERR_RED = "#B11A1A"
WARN = "#8A5A00"
FIELD_BORDER = "#7F9DB9"


# ---------------------------------------------------------------- engine
def call(path: str, body=None, method: str | None = None, timeout: float = 30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{ENGINE}{path}", data=data, method=method or ("POST" if data else "GET"),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read()).get("detail")
        except Exception:
            detail = e.reason
        raise RuntimeError(detail if isinstance(detail, str) else json.dumps(detail)) from None


# ---------------------------------------------------------------- XP widgets
def _mix(c1: str, c2: str, t: float) -> str:
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(a, b))


def gradient(stops, t: float) -> str:
    for (t0, c0), (t1, c1) in zip(stops, stops[1:]):
        if t <= t1:
            return _mix(c0, c1, (t - t0) / (t1 - t0) if t1 > t0 else 0)
    return stops[-1][1]


def rounded(cv: tk.Canvas, x0, y0, x1, y1, r, **kw):
    pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1, x1 - r, y1, x0 + r, y1, x0, y1,
           x0, y1 - r, x0, y0 + r, x0, y0]
    return cv.create_polygon(pts, smooth=True, **kw)


class XPButton(tk.Canvas):
    """Rounded XP button: white-to-beige face, dark blue border, orange glow on hover."""

    def __init__(self, master, text, command, width=None, default=False, font=None, fits=()):
        """fits: other labels the button will show later; it is sized for the longest one."""
        f = font or tkfont.nametofont("TkDefaultFont")
        w = width or max(75, *(f.measure(t) + 22 for t in (text, *fits)))
        super().__init__(master, width=w, height=23, bg=FACE, highlightthickness=0, cursor="hand2")
        self.text, self.command, self.font, self.default, self.w = text, command, f, default, w
        self.enabled, self.hover, self.down = True, False, False
        for ev, fn in (("<Enter>", self._enter), ("<Leave>", self._leave), ("<ButtonPress-1>", self._press),
                       ("<ButtonRelease-1>", self._release)):
            self.bind(ev, fn)
        self.draw()

    def draw(self):
        self.delete("all")
        w, h = self.w, 23
        for y in range(2, h - 2):                       # face gradient
            top, bot = ("#FFFFFF", "#E3E1D6") if not self.down else ("#E3E1D6", "#FFFFFF")
            self.create_line(3, y, w - 3, y, fill=gradient([(0, top), (0.75, "#F3F2EC"), (1, bot)], y / h))
        if self.hover and self.enabled:
            rounded(self, 2, 2, w - 3, h - 3, 4, outline="#F8B330", fill="", width=2)
        elif self.default and self.enabled:
            rounded(self, 2, 2, w - 3, h - 3, 4, outline="#8AA9E2", fill="", width=1)
        rounded(self, 0, 0, w - 1, h - 1, 5, outline=BTN_BORDER if self.enabled else "#C9C7BA", fill="", width=1)
        self.create_text(w // 2, h // 2 + (1 if self.down else 0), text=self.text, font=self.font,
                         fill=TEXT if self.enabled else "#A3A28F")

    def set_enabled(self, on: bool):
        self.enabled = on
        self.configure(cursor="hand2" if on else "arrow")
        self.draw()

    def _enter(self, _):
        self.hover = True
        self.draw()

    def _leave(self, _):
        self.hover = self.down = False
        self.draw()

    def _press(self, _):
        if self.enabled:
            self.down = True
            self.draw()

    def _release(self, _):
        was = self.down
        self.down = False
        self.draw()
        if was and self.enabled and self.hover:
            self.command()


def error_icon(master, size=32) -> tk.Canvas:
    cv = tk.Canvas(master, width=size, height=size, bg=FACE, highlightthickness=0)
    for i in range(size // 2, 0, -1):                   # red ball with a light top
        cv.create_oval(size / 2 - i, size / 2 - i, size / 2 + i, size / 2 + i, outline="",
                       fill=gradient([(0, "#FF9B85"), (0.45, "#E8402B"), (1, "#B3180B")], 1 - i / (size / 2)))
    cv.create_oval(1, 1, size - 2, size - 2, outline="#8E1307", width=1)
    m = size * 0.3
    cv.create_line(m, m, size - m, size - m, fill="white", width=size // 8, capstyle="round")
    cv.create_line(size - m, m, m, size - m, fill="white", width=size // 8, capstyle="round")
    return cv


class XPWindow(tk.Toplevel):
    """Borderless window with an XP title bar (drag to move) and the red close box."""

    def __init__(self, master, title: str, native: bool = False):
        super().__init__(master)
        self.configure(bg=BORDER)
        if not native:
            self.overrideredirect(True)
        self.title(title)
        self.bar = tk.Canvas(self, height=30, bg=BORDER, highlightthickness=0)
        self.bar.pack(fill="x")
        self.body = tk.Frame(self, bg=FACE, padx=12, pady=10)
        self.body.pack(fill="both", expand=True, padx=3, pady=(0, 3))
        self._title = title
        self.bar.bind("<Configure>", lambda _: self._draw_bar())
        self.bar.bind("<ButtonPress-1>", self._grab)
        self.bar.bind("<B1-Motion>", self._drag)
        self._off = (0, 0)

    def _draw_bar(self):
        cv, w, h = self.bar, self.bar.winfo_width(), 30
        cv.delete("all")
        for y in range(h):
            cv.create_line(0, y, w, y, fill=gradient(TITLE_STOPS, y / (h - 1)))
        bold = tkfont.Font(family=FONT_FAMILY, size=10, weight="bold")
        cv.create_text(9, 16, text=self._title, anchor="w", fill="#0A1E6E", font=bold)     # shadow
        cv.create_text(8, 15, text=self._title, anchor="w", fill="white", font=bold)
        x0 = w - 27                                                                           # close box
        rounded(cv, x0, 5, x0 + 21, 26, 4, fill="#E05A3A", outline="white", width=1)
        for y in range(7, 25):
            cv.create_line(x0 + 2, y, x0 + 20, y, fill=gradient([(0, "#F09A7E"), (0.5, "#E0532F"), (1, "#C23A16")],
                                                                   (y - 7) / 18))
        rounded(cv, x0, 5, x0 + 21, 26, 4, fill="", outline="white", width=1)
        cv.create_line(x0 + 6, 10, x0 + 15, 20, fill="white", width=2)
        cv.create_line(x0 + 15, 10, x0 + 6, 20, fill="white", width=2)
        cv.tag_bind(cv.create_rectangle(x0, 5, x0 + 21, 26, outline="", fill=""), "<Button-1>",
                    lambda _: self.master.destroy())

    def _grab(self, e):
        self._off = (e.x_root - self.winfo_x(), e.y_root - self.winfo_y())

    def _drag(self, e):
        self.geometry(f"+{e.x_root - self._off[0]}+{e.y_root - self._off[1]}")


def message_box(master, title: str, text: str) -> None:
    """Small XP error box (rejected rows, engine down, bad weights)."""
    win = XPWindow(master, title)
    row = tk.Frame(win.body, bg=FACE)
    row.pack(fill="x")
    error_icon(row).pack(side="left", anchor="n", padx=(0, 12))
    tk.Label(row, text=text, bg=FACE, justify="left", wraplength=380, font=BASE_FONT).pack(side="left")
    XPButton(win.body, "OK", win.destroy, default=True, font=BASE_FONT).pack(pady=(14, 0))
    win.update_idletasks()
    x = master.winfo_rootx() + 60 if master.winfo_viewable() else 300
    win.geometry(f"+{x}+{master.winfo_rooty() + 80}")
    win.lift()
    win.attributes("-topmost", True)


# ---------------------------------------------------------------- the window
class InputWindow:
    def __init__(self, root: tk.Tk, native: bool = False):
        self.root = root
        self.win = XPWindow(root, "Warehouse - Data input", native=native)
        self.slots: dict[str, dict | None] = {s: None for s in SLOTS}
        self.path_vars = {s: tk.StringVar() for s in SLOTS}
        self.note_lbls: dict[str, tk.Label] = {}
        self.params: list[dict] = []
        self.param_vars: dict[str, tk.Variable] = {}
        self.weight_vars: dict[str, tk.StringVar] = {}
        self.expanded = False
        self._build()
        self._refresh_status()

    def bring_to_front(self):
        self.win.deiconify()
        self.win.lift()
        self.win.attributes("-topmost", True)
        self.win.after(1500, lambda: self.win.attributes("-topmost", False))
        self.win.focus_force()

    # ---------- layout
    def _build(self):
        b = self.win.body
        top = tk.Frame(b, bg=FACE)
        top.pack(fill="x")
        error_icon(top).pack(side="left", anchor="n", padx=(0, 12), pady=2)
        tk.Label(top, bg=FACE, font=BASE_FONT, justify="left", anchor="w", text=(
            "Importation supported: PDF, XLSX, XLSM, CSV.\n"
            "1 single workbook for all is also supported")).pack(side="left", fill="x")

        grid = tk.Frame(b, bg=FACE)
        grid.pack(fill="x", pady=(12, 4))
        for r, slot in enumerate(SLOTS):
            tk.Label(grid, text=LABEL[slot] + ":", bg=FACE, font=BOLD_FONT, width=10, anchor="w").grid(
                row=2 * r, column=0, sticky="w")
            box = tk.Frame(grid, bg=FIELD_BORDER, padx=1, pady=1)
            box.grid(row=2 * r, column=1, sticky="we", padx=(0, 6))
            tk.Entry(box, textvariable=self.path_vars[slot], state="readonly", readonlybackground="white",
                     relief="flat", font=BASE_FONT, width=46).pack(fill="x", ipady=2)
            XPButton(grid, "Browse...", lambda s=slot: self._browse(s), font=BASE_FONT).grid(row=2 * r, column=2)
            lbl = tk.Label(grid, text="No file", bg=FACE, fg=GREY, font=SMALL_FONT, anchor="w", justify="left",
                           wraplength=430)
            lbl.grid(row=2 * r + 1, column=1, columnspan=2, sticky="w", pady=(1, 7))
            self.note_lbls[slot] = lbl
        grid.columnconfigure(1, weight=1)

        tk.Frame(b, bg="#ACA899", height=1).pack(fill="x", pady=(2, 1))
        tk.Frame(b, bg="white", height=1).pack(fill="x", pady=(0, 6))

        self.params_frame = tk.Frame(b, bg=FACE)
        btns = tk.Frame(b, bg=FACE)
        btns.pack(fill="x", side="bottom")
        self.toggle_btn = XPButton(btns, "Extend Parameters", self._toggle, font=BASE_FONT, fits=("Shrink Parameters",))
        self.toggle_btn.pack(side="left")
        XPButton(btns, "Close", self.root.destroy, font=BASE_FONT).pack(side="right")
        XPButton(btns, "Go to data", self._open_sheet, font=BASE_FONT).pack(side="right", padx=6)
        self.import_btn = XPButton(btns, "Import data", self._import, default=True, font=BASE_FONT)
        self.import_btn.pack(side="right")
        self.import_btn.set_enabled(False)
        self.status = tk.Label(b, text="Connecting to the engine...", bg=FACE, fg=GREY, font=SMALL_FONT,
                               anchor="w", justify="left", wraplength=500)
        self.status.pack(fill="x", side="bottom", pady=(0, 8))
        self.result = tk.Label(b, text="", bg=FACE, fg=GREY, font=BASE_FONT, anchor="w", justify="left",
                               wraplength=520)
        self.result.pack(fill="x", side="bottom", pady=(0, 4))

    def _build_params(self):
        f = self.params_frame
        for w in f.winfo_children():
            w.destroy()
        tk.Label(f, text="Correlation weight - importance of each factor - in %", bg=FACE, font=BOLD_FONT).grid(
            row=0, column=0, columnspan=4, sticky="w")
        for i, (k, lab) in enumerate((("size_fit", "Size fit"), ("dwell", "Dwell time"), ("fragility", "Fragility"),
                                      ("weight", "Weight"))):
            tk.Label(f, text=lab, bg=FACE, font=BASE_FONT).grid(row=1 + i // 2, column=(i % 2) * 2, sticky="w",
                                                                 padx=(0, 6))
            tk.Spinbox(f, from_=0, to=100, increment=5, width=5, textvariable=self.weight_vars[k], font=BASE_FONT,
                       relief="solid", bd=1).grid(row=1 + i // 2, column=(i % 2) * 2 + 1, sticky="w", padx=(0, 18))
        tk.Label(f, text="Slotting", bg=FACE, font=BOLD_FONT).grid(row=3, column=0, columnspan=4, sticky="w",
                                                                   pady=(8, 0))
        r = 4
        for p in self.params:
            v = self.param_vars[p["key"]]
            tk.Label(f, text=p["label"], bg=FACE, font=BASE_FONT).grid(row=r, column=0, columnspan=2, sticky="w",
                                                                        padx=(0, 12))
            if p["type"] == "bool":
                w = tk.Checkbutton(f, variable=v, bg=FACE, activebackground=FACE, font=BASE_FONT)
            elif p["type"] == "buildings":
                w = tk.Frame(f, bg=FACE)
                for bnum in (1, 2, 3, 4):
                    tk.Checkbutton(w, text=f"B{bnum}", variable=v[bnum - 1], bg=FACE, activebackground=FACE,
                                   font=BASE_FONT).pack(side="left")
            else:
                inc = 1 if p["type"] == "int" or (p["max"] or 0) > 5 else 0.05
                w = tk.Spinbox(f, from_=p["min"], to=p["max"], increment=inc, width=7, textvariable=v,
                               font=BASE_FONT, relief="solid", bd=1)
            w.grid(row=r, column=2, columnspan=2, sticky="w")
            if p["help"]:
                tk.Label(f, text=p["help"], bg=FACE, fg=GREY, font=SMALL_FONT).grid(
                    row=r + 1, column=0, columnspan=4, sticky="w", pady=(0, 3))
                r += 2
            else:
                r += 1
        XPButton(f, "Apply", self._apply_params, font=BASE_FONT).grid(
            row=r, column=0, columnspan=4, sticky="w", pady=(6, 8))

    # ---------- engine status
    def _bg(self, fn, done):
        def run():
            try:
                res, err = fn(), None
            except Exception as e:              # network / engine error -> shown in the window
                res, err = None, e
            self.root.after(0, lambda: done(res, err))
        threading.Thread(target=run, daemon=True).start()

    def _refresh_status(self):
        self.status.configure(text=f"Spreadsheet: {EXPORT.relative_to(ROOT)}", fg=OK_GREEN)
        if not self.params:                     # parameters panel: retry until the engine answers
            self._load_params()
            self.root.after(5000, self._refresh_status)

    def _load_params(self):
        def done(res, err):
            if err:
                return
            self.params, weights = res
            for k, v in weights.items():
                self.weight_vars.setdefault(k, tk.StringVar()).set(str(int(v) if float(v).is_integer() else v))
            for p in self.params:
                if p["type"] == "bool":
                    self.param_vars[p["key"]] = tk.BooleanVar(value=p["value"])
                elif p["type"] == "buildings":
                    self.param_vars[p["key"]] = [tk.BooleanVar(value=b in p["value"]) for b in (1, 2, 3, 4)]
                else:
                    self.param_vars[p["key"]] = tk.StringVar(value=str(p["value"]))
            self._build_params()
        self._bg(lambda: (call("/admin/params"), call("/admin/weights")), done)

    def _say(self, text, color=GREY):
        self.result.configure(text=text, fg=color)

    # ---------- inputs
    def _browse(self, slot):
        path = filedialog.askopenfilename(parent=self.win, title=f"{LABEL[slot]} data sheet", filetypes=[
            ("Data sheets", "*.xlsx *.xlsm *.pdf *.csv"), ("All files", "*.*")])
        if path:
            self.load(slot, path)

    def preload(self, paths: list[str]) -> None:
        """Files given on the command line: put each in the first empty slot; detection moves it if needed."""
        for path in paths:
            slot = next((s for s in SLOTS if self.slots[s] is None and not self.path_vars[s].get()), SLOTS[0])
            self.load(slot, str(Path(path).resolve()))

    def load(self, slot: str, path: str) -> None:
        self.path_vars[slot].set(Path(path).name)
        self.note_lbls[slot].configure(text="Reading...", fg=GREY)

        def done(info, err):
            if err:
                self.note_lbls[slot].configure(text=str(err), fg=ERR_RED)
                self.slots[slot] = None
            else:
                self._place(slot, info)
            self._refresh_import()
        self._bg(lambda: call("/ingest/inspect", {"path": path}), done)

    def _place(self, slot, info):
        text, tone = describe(info)
        color = {"ok": OK_GREEN, "warn": WARN, "err": ERR_RED}[tone]
        if tone == "err":
            self.slots[slot] = None
            self.note_lbls[slot].configure(text=text, fg=color)
            return
        targets = assign(slot, info)
        if slot not in targets:                             # chosen in the wrong slot
            self.path_vars[slot].set("")
            self.slots[slot] = None
            self.note_lbls[slot].configure(text=f"Moved to {', '.join(LABEL[t] for t in targets)}.", fg=WARN)
        first = next(t for t in SLOTS if t in targets)
        for t in targets:
            self.slots[t] = info
            self.path_vars[t].set(Path(info["path"]).name)
            self.note_lbls[t].configure(text=text if t == first else f"Same file as {LABEL[first]}.", fg=color)

    def _refresh_import(self):
        self.import_btn.set_enabled(bool(import_plan(self.slots)))

    def _import(self):
        plan = import_plan(self.slots)
        copied = []
        for src, folder in plan:
            folder.mkdir(parents=True, exist_ok=True)
            dest = folder / Path(src).name
            if Path(src).resolve() != dest.resolve():
                shutil.copy2(src, dest)
            else:
                os.utime(dest)                              # already in the inbox: touch to re-ingest
            copied.append(str(dest.resolve()))
        self._say(f"Sent {len(copied)} file(s) to the inbox, waiting for the engine...", GREY)
        self.import_btn.set_enabled(False)
        since = self._last_event_id()
        try:
            dl = call("/admin/dead_letter?limit=1", timeout=3)
            self._dl_since = dl[0]["id"] if dl else 0
        except Exception:
            self._dl_since = 0

        def wait():
            results, deadline = {}, time.time() + 90
            while time.time() < deadline and len(results) < len(copied):
                for ev in call(f"/events/recent?limit=100"):
                    if ev["id"] > since and ev["type"] == "ingest.file" and ev["data"]["path"] in copied:
                        results.setdefault(ev["data"]["path"], ev["data"])
                time.sleep(1)
            return results
        self._bg(wait, lambda res, err: self._imported(copied, res or {}, err))

    def _last_event_id(self) -> int:
        try:
            evs = call("/events/recent?limit=1", timeout=3)
            return evs[0]["id"] if evs else 0
        except Exception:
            return 0

    def _imported(self, copied, results, err):
        self._refresh_import()
        if err:
            message_box(self.root, "Warehouse - Import", f"Could not read the result: {err}")
            return
        lines, rejected = [], 0
        for p in copied:
            r = results.get(p)
            if r is None:
                lines.append(f"{Path(p).name}: no answer yet (watcher running?)")
                continue
            if r.get("skipped_file"):
                lines.append(f"{Path(p).name}: unchanged since last import")
                continue
            rejected += r["rejected"]
            lines.append(f"{Path(p).name}: {r['inserted']} new, {r['updated']} updated, {r['unchanged']} unchanged, "
                         f"{r['cancelled']} removed, {r['rejected']} rejected")
        self._say("\n".join(lines), ERR_RED if rejected else OK_GREEN)
        if rejected:
            dl = call("/admin/dead_letter?limit=200")
            mine = [d for d in dl if d["source_file"] in copied and d["id"] > self._dl_since][:6]
            detail = "\n".join(f"- {Path(d['source_file']).name} row {d['row_number']}: {d['error'][:110]}" for d in mine)
            message_box(self.root, "Warehouse - Import", f"{rejected} row(s) could not be imported:\n\n{detail}\n\n"
                                                         "Fix them in the source file and import it again.")

    # ---------- parameters
    def _toggle(self):
        self.expanded = not self.expanded
        self.toggle_btn.text = "Shrink Parameters" if self.expanded else "Extend Parameters"
        self.toggle_btn.draw()
        if self.expanded:
            self.params_frame.pack(fill="x", before=self.result)
        else:
            self.params_frame.pack_forget()

    def _apply_params(self):
        try:
            weights = {k: float(v.get()) for k, v in self.weight_vars.items()}
        except ValueError:
            message_box(self.root, "Warehouse - Parameters", "Weights must be numbers.")
            return
        if abs(sum(weights.values()) - 100) > 0.01:
            message_box(self.root, "Warehouse - Parameters",
                        f"Weights must add up to 100 (now {sum(weights.values()):g}).")
            return
        values = {}
        for p in self.params:
            v = self.param_vars[p["key"]]
            if p["type"] == "bool":
                values[p["key"]] = bool(v.get())
            elif p["type"] == "buildings":
                values[p["key"]] = [b for b, var in zip((1, 2, 3, 4), v) if var.get()]
            else:
                try:
                    values[p["key"]] = float(v.get())
                except ValueError:
                    message_box(self.root, "Warehouse - Parameters", f"{p['label']}: not a number.")
                    return

        def done(_, err):
            if err:
                message_box(self.root, "Warehouse - Parameters", str(err))
            else:
                self._say("Parameters applied. New deliveries use them right away.", OK_GREEN)
        self._bg(lambda: (call("/admin/weights", weights, method="PUT"), call("/admin/params", values, method="PUT")),
                 done)

    def _open_sheet(self):
        if not EXPORT.exists():
            message_box(self.root, "Warehouse - Spreadsheet", "The spreadsheet has not been generated yet. "
                                                              "Start the engine first.")
            return
        if sys.platform.startswith("win"):
            os.startfile(EXPORT)                            # noqa: S606 (local file)
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(EXPORT)])


# ---------------------------------------------------------------- single instance
PORT = int(os.environ.get("WH_WINDOW_PORT", "8766"))


def single_instance() -> socket.socket | None:
    """Own the window port, or ask the running window to come to the front and return None."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        srv.bind(("127.0.0.1", PORT))
        srv.listen(4)
        return srv
    except OSError:
        srv.close()
        try:
            with socket.create_connection(("127.0.0.1", PORT), timeout=2) as c:
                c.sendall(b"raise")
        except OSError:
            pass
        return None


def serve_raise(srv: socket.socket, root: tk.Tk, ui: "InputWindow") -> None:
    while True:
        try:
            conn, _ = srv.accept()
        except OSError:
            return
        with conn:
            if conn.recv(16).startswith(b"raise"):
                root.after(0, ui.bring_to_front)


# ---------------------------------------------------------------- main
def main():
    global FONT_FAMILY, BASE_FONT, BOLD_FONT, SMALL_FONT
    ap = argparse.ArgumentParser()
    ap.add_argument("--native", action="store_true", help="keep the system window frame")
    ap.add_argument("--screenshot", help="render, save a PNG of the window, exit (needs ImageMagick 'import')")
    ap.add_argument("--expanded", action="store_true", help="open with the parameters shown")
    ap.add_argument("--import-now", action="store_true", help="with files: import them right away (scripts)")
    ap.add_argument("files", nargs="*", help="data sheets to preload (pdf, xlsx, xlsm, csv)")
    a = ap.parse_args()
    server = None
    if not a.screenshot:
        server = single_instance()
        if server is None:                      # already open: it was asked to come to the front
            return
    root = tk.Tk()
    root.withdraw()
    fams = set(tkfont.families())
    FONT_FAMILY = next(f for f in ("Tahoma", "Verdana", "DejaVu Sans", "TkDefaultFont") if f in fams or f == "TkDefaultFont")
    BASE_FONT = tkfont.Font(family=FONT_FAMILY, size=9)
    BOLD_FONT = tkfont.Font(family=FONT_FAMILY, size=9, weight="bold")
    SMALL_FONT = tkfont.Font(family=FONT_FAMILY, size=8)
    ui = InputWindow(root, native=a.native)
    ui.win.protocol("WM_DELETE_WINDOW", root.destroy)
    ui.win.update_idletasks()
    sw, sh = ui.win.winfo_screenwidth(), ui.win.winfo_screenheight()
    ui.win.geometry(f"+{max((sw - ui.win.winfo_reqwidth()) // 2, 0)}+{max((sh - 620) // 3, 0)}")
    if a.expanded:
        root.after(1200, ui._toggle)
    if server is not None:
        threading.Thread(target=serve_raise, args=(server, root, ui), daemon=True).start()
    if a.files:
        root.after(300, lambda: ui.preload(a.files))
        if a.import_now:
            root.after(2500, ui._import)
    if a.screenshot:
        def shot():
            root.update()
            wins = [w for w in root.winfo_children() if isinstance(w, tk.Toplevel) and w.winfo_viewable()]
            base, ext = os.path.splitext(a.screenshot)
            for n, w in enumerate(wins):
                target = a.screenshot if n == 0 else f"{base}_{n}{ext}"
                subprocess.run(["import", "-window", w.wm_frame() if not a.native else str(w.winfo_id()), target],
                               check=False)
            root.destroy()
        root.after(int(os.environ.get("WH_SHOT_DELAY_MS", "2600")), shot)
    root.mainloop()


FONT_FAMILY = "TkDefaultFont"
BASE_FONT = BOLD_FONT = SMALL_FONT = None

if __name__ == "__main__":
    main()
