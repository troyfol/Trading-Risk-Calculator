import tkinter as tk
from tkinter import ttk, messagebox, colorchooser
import ctypes
from ctypes import wintypes
import json
import os
import shutil
import threading
import time
import re
import sys  # Required for the .exe to find files
import copy

# --- Win32 helpers ---
# We're already Windows-only (DPI awareness, ImageGrab, Tesseract path) so
# using the user32 API directly for click-location, foreground tracking,
# and per-monitor classification is fine — and far more reliable than Tk's
# focus events on Windows.
_GA_ROOT = 2          # GetAncestor: walk up parents to the root toplevel
_MONITOR_DEFAULTTONULL = 0
_MONITOR_DEFAULTTONEAREST = 2


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


# EnumDisplayMonitors callback signature
_MONITORENUMPROC = ctypes.WINFUNCTYPE(
    ctypes.c_int,
    wintypes.HMONITOR,
    wintypes.HDC,
    ctypes.POINTER(wintypes.RECT),
    wintypes.LPARAM,
)

try:
    _user32 = ctypes.windll.user32
    _user32.WindowFromPoint.argtypes = [wintypes.POINT]
    _user32.WindowFromPoint.restype = wintypes.HWND
    _user32.GetAncestor.argtypes = [wintypes.HWND, ctypes.c_uint]
    _user32.GetAncestor.restype = wintypes.HWND
    _user32.GetForegroundWindow.restype = wintypes.HWND
    _user32.EnumDisplayMonitors.argtypes = [
        wintypes.HDC, ctypes.POINTER(wintypes.RECT),
        _MONITORENUMPROC, wintypes.LPARAM,
    ]
    _user32.EnumDisplayMonitors.restype = wintypes.BOOL
    _user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
    _user32.MonitorFromWindow.restype = wintypes.HMONITOR
    _user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
    _user32.MonitorFromPoint.restype = wintypes.HMONITOR
    _user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(_MONITORINFO)]
    _user32.GetMonitorInfoW.restype = wintypes.BOOL
    _WIN32_AVAILABLE = True
except (AttributeError, OSError):
    _user32 = None
    _WIN32_AVAILABLE = False


def _enumerate_monitors():
    """Return [(hMonitor_int, (left, top, right, bottom)), ...] sorted
    left-to-right by left edge. Empty list if Win32 unavailable."""
    if not _WIN32_AVAILABLE:
        return []
    monitors = []

    def _cb(hMonitor, hdcMonitor, lprcMonitor, dwData):
        r = lprcMonitor.contents
        monitors.append((int(hMonitor), (r.left, r.top, r.right, r.bottom)))
        return 1  # continue enumeration

    cb = _MONITORENUMPROC(_cb)
    try:
        _user32.EnumDisplayMonitors(0, None, cb, 0)
    except (OSError, AttributeError):
        return []
    monitors.sort(key=lambda m: m[1][0])  # by left edge
    return monitors


def _sanitize_preset_name(name):
    """Filename-safe: strip non-alphanumeric except space/dash/underscore."""
    cleaned = re.sub(r'[^A-Za-z0-9 _\-]+', '', name).strip()
    return cleaned or "preset"


def _list_preset_names():
    """Return sorted list of preset names (without the .json suffix)."""
    if not os.path.isdir(_PRESETS_DIR):
        return []
    out = []
    for fname in os.listdir(_PRESETS_DIR):
        if fname.endswith(".json"):
            out.append(fname[:-5])
    out.sort(key=str.lower)
    return out


def _save_preset(name, region):
    """Write {ocr_left, ocr_right, ocr_above, ocr_below} JSON to
    presets/<name>.json atomically.

    Same shape as ``_save_config_now`` — write to a sibling ``.tmp`` file,
    fsync, then ``os.replace`` into the final name (atomic on Windows).
    A crash mid-write can't corrupt the existing preset; the worst case
    is a leftover ``.tmp`` that the next save overwrites.
    """
    os.makedirs(_PRESETS_DIR, exist_ok=True)
    safe = _sanitize_preset_name(name)
    path = os.path.join(_PRESETS_DIR, safe + ".json")
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(region, f, indent=2)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass
    os.replace(tmp_path, path)
    return path


def _load_preset(name):
    """Return the region dict for the named preset, or None on failure."""
    path = os.path.join(_PRESETS_DIR, name + ".json")
    try:
        with open(path, "r") as f:
            data = json.load(f)
        # Only return the four expected keys (ignore anything else)
        return {k: int(data.get(k, 0)) for k in
                ("ocr_left", "ocr_right", "ocr_above", "ocr_below")}
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return None


def _monitor_rect_at_point(x, y):
    """Return the (l, t, r, b) rect of the monitor containing (x, y),
    or None if Win32 unavailable / point off-screen."""
    if not _WIN32_AVAILABLE:
        return None
    try:
        hmon = _user32.MonitorFromPoint(
            wintypes.POINT(int(x), int(y)), _MONITOR_DEFAULTTONEAREST)
        if not hmon:
            return None
        mi = _MONITORINFO()
        mi.cbSize = ctypes.sizeof(_MONITORINFO)
        if not _user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            return None
        return (mi.rcMonitor.left, mi.rcMonitor.top,
                mi.rcMonitor.right, mi.rcMonitor.bottom)
    except (OSError, AttributeError):
        return None

# --- AUTOMATION SETUP ---
def _resolve_tesseract_path():
    """Lookup chain: TESSERACT_CMD env var → bundled (_MEIPASS) → Program Files
    (x64/x86) → PATH (shutil.which). Returns first existing path or None."""
    candidates = []
    env = os.environ.get("TESSERACT_CMD")
    if env:
        candidates.append(env)
    if getattr(sys, 'frozen', False):
        candidates.append(os.path.join(sys._MEIPASS, 'Tesseract-OCR', 'tesseract.exe'))
    candidates.append(r'C:\Program Files\Tesseract-OCR\tesseract.exe')
    candidates.append(r'C:\Program Files (x86)\Tesseract-OCR\tesseract.exe')
    on_path = shutil.which("tesseract")
    if on_path:
        candidates.append(on_path)
    for p in candidates:
        if p and os.path.exists(p):
            return p
    return None


try:
    import pytesseract
    from PIL import ImageGrab, ImageOps, ImageEnhance, Image, ImageTk
    from pynput import mouse

    TESSERACT_PATH = _resolve_tesseract_path()
    if TESSERACT_PATH:
        pytesseract.pytesseract.tesseract_cmd = TESSERACT_PATH
        AUTOMATION_AVAILABLE = True
    else:
        AUTOMATION_AVAILABLE = False
        print("Warning: Tesseract not found. Set TESSERACT_CMD or install to Program Files.")

except ImportError as _imp_err:
    AUTOMATION_AVAILABLE = False
    print(f"Warning: Automation unavailable — {_imp_err}")

def _deep_merge_defaults(defaults, override):
    """Recursive merge: overlay `override` onto a deep-copy of `defaults`.
    Dict values merge recursively; scalars/lists from override replace defaults.
    Forward-compatible: nested defaults added in newer versions show up for
    users with older config files."""
    result = copy.deepcopy(defaults)
    if not isinstance(override, dict):
        return result
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge_defaults(result[key], val)
        else:
            result[key] = copy.deepcopy(val)
    return result


# --- COMPILED REGEX (module-level so they're not rebuilt per click) ---
# Clipboard price: REQUIRES a decimal point + 1-4 fractional digits.
# Rejects bare integers (which were the source of accidental autofills).
_CLIPBOARD_PRICE_RE = re.compile(r'^\d+\.\d{1,4}$')
# OCR labeled-price match (TradeStation tooltip with a "Price:" / "Close:" label)
_OCR_LABELED_PRICE_RE = re.compile(
    r'(?:Price|Close|High|Low)[^0-9]*?(\d+\.\d{2,4})', re.IGNORECASE)
# OCR fallback: any decimal with 2-4 fractional digits (covers FX/crypto, was \d{2} only)
_OCR_FALLBACK_PRICE_RE = re.compile(r'\b(\d+\.\d{2,4})\b')

# Resolve config path relative to the .exe or script location, not CWD
if getattr(sys, 'frozen', False):
    _APP_DIR = os.path.dirname(sys.executable)
else:
    _APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(_APP_DIR, "window_config.json")
_PRESETS_DIR = os.path.join(_APP_DIR, "presets")  # capture-region preset .json files

# --- COLOR PALETTE ---
BG_COLOR = "#1e1e1e"
FG_COLOR = "#ffffff"
ENTRY_BG = "#333333"
ENTRY_FG = "#ffffff"
ACCENT_COLOR = "#4a4a4a"
HIGHLIGHT = "#007acc"
STALE_BG = "#6b5d1f"  # amber/olive tint applied to Stop+Shares after a failed OCR click

class TradeSolverApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Trade Solver")
        self.root.attributes('-topmost', True)
        self.root.configure(bg=BG_COLOR)

        self.vars = {
            "Entry": tk.StringVar(),
            "Stop": tk.StringVar(),
            "Risk $": tk.StringVar(),
            "Shares": tk.StringVar(),
            "Cost": tk.StringVar()
        }
        self.direction_var = tk.StringVar(value="Long")
        self.smart_click_enabled = tk.BooleanVar(value=False)

        # --- Stop Offset state ---
        # Mode: "manual" → user types Stop directly (legacy behavior).
        #       "pct"    → Stop is derived as Entry ± offset% (long: -, short: +).
        #       "dollar" → Stop is derived as Entry ± offset$ per share.
        # Each mode keeps its OWN persistent value, so toggling between them
        # restores the last value used in that mode (matches Risk $ persistence).
        self.stop_mode_var = tk.StringVar(value="manual")
        self.stop_offset_var = tk.StringVar()  # currently displayed offset value
        self._stop_offset_pct = ""             # last-used % offset
        self._stop_offset_dollar = ""          # last-used $ offset

        # --- Slippage state ---
        # Two sides (Entry, Exit), each with: enabled flag, mode (pct|dollar),
        # currently-displayed value, and persistent per-mode last-values.
        self.slip_entry_enabled = tk.BooleanVar(value=False)
        self.slip_exit_enabled = tk.BooleanVar(value=False)
        self.slip_entry_mode = tk.StringVar(value="pct")
        self.slip_exit_mode = tk.StringVar(value="pct")
        self.slip_entry_var = tk.StringVar()
        self.slip_exit_var = tk.StringVar()
        self._slip_entry_pct = ""
        self._slip_entry_dollar = ""
        self._slip_exit_pct = ""
        self._slip_exit_dollar = ""
        
        # LOGIC TOGGLE: True = Next Click is Entry; False = Next Click is Stop
        self.entry_turn = True
        self.freeze_entry = tk.BooleanVar(value=False)
        self.freeze_stop = tk.BooleanVar(value=False)
        self._last_stop = None
        self._last_shares = None
        self._last_cost = None
        self._ocr_lock = threading.Lock()  # Prevents overlapping OCR clicks
        self._ocr_thread = None
        self._closing = False

        # Snapshots of field values BEFORE indicate_loading() puts a "..." in.
        # On OCR failure we restore the prior text so a missed click never
        # nukes a good Entry/Stop. Keyed by field name.
        self._pre_click_values = {}

        # Debug capture window (lazy — only created when debug mode fires).
        self._debug_win = None
        self._debug_raw_label = None
        self._debug_proc_label = None
        self._debug_text_label = None
        # Hold ImageTk references; Tk garbage-collects images otherwise
        self._debug_imgrefs = []
        # Map of field name → ttk.Entry widget, kept so we can flip styles
        # (e.g., the Stale.TEntry yellow tint) without recreating widgets.
        self.entry_widgets = {}

        # LFA (Long First Arrival): use longer OCR delay any time the
        # Windows foreground HWND changes. Maximally permissive — every
        # app-to-app switch counts. The user explicitly preferred more
        # LFA firings over fewer.
        #
        # Approach: poll GetForegroundWindow every 100ms. If the top-level
        # HWND differs from the previous tick, stamp _last_app_switch_ts.
        # OCR clicks within _LFA_RECENT_WINDOW_S of the stamp use LFA delay.
        self.lfa_enabled = tk.BooleanVar(value=True)
        self._last_app_switch_ts = 0.0
        self._last_foreground_hwnd = 0
        # Track monitor of the last OCR click separately. Multi-monitor TS
        # often shares a single top-level HWND across monitors (chart
        # sub-windows roll up to the same root), so HWND change alone
        # misses cross-monitor transitions. Monitor is the discriminator.
        self._last_click_monitor = 0
        self._foreground_poll_id = None
        self._LFA_RECENT_WINDOW_S = 1.5

        # Platform toggle: "TradeStation" or "TradingView"
        self.platform_var = tk.StringVar(value="TradeStation")

        # TV clipboard polling state
        self._clipboard_poll_id = None
        self._last_clipboard = ""
        self._polling_active = False  # Authoritative on/off; checked inside _poll_clipboard

        # Mouse listener (lazy: only running when Smart Click is on in TS mode)
        self.listener = None

        # Settings (overwritten by load_config if saved values exist)
        self._DEFAULT_SETTINGS = {
            "platform": "TradeStation",
            "normal_delay": 0.1,
            "lfa_delay": 0.5,
            "ocr_left": 20,
            "ocr_above": 400,
            "ocr_right": 300,
            "ocr_below": 20,
            # Monitor lock: only fire OCR on this monitor (rect tuple).
            # null/None means "no lock — any monitor". On first launch
            # we'll set it to the calc's current monitor so it works
            # out of the box on the user's primary screen.
            "ocr_monitor_rect": None,
            # Debug mode: when True, every OCR capture pops up a small
            # preview window with the captured image and OCR text — for
            # visual tuning of the capture region.
            "ocr_debug_mode": False,
            "targets": [
                {"r_multiple": 1.0, "color": "#69db7c"},
                {"r_multiple": 2.0, "color": "#69db7c"},
                {"r_multiple": 3.0, "color": "#69db7c"},
            ]
        }
        self.settings = copy.deepcopy(self._DEFAULT_SETTINGS)

        # Load Config
        self.load_config()
        self.platform_var.set(self.settings.get("platform", "TradeStation"))
        self.smart_click_enabled.set(self._saved_smart_click)
        self.lfa_enabled.set(self._saved_lfa)
        self.direction_var.set(self._saved_direction)
        self.freeze_entry.set(self._saved_freeze_entry)
        self.freeze_stop.set(self._saved_freeze_stop)
        # Stop offset
        self.stop_mode_var.set(self._saved_stop_mode)
        self._stop_offset_pct = self._saved_stop_offset_pct
        self._stop_offset_dollar = self._saved_stop_offset_dollar
        # Seed the displayed offset value to the current mode's slot
        if self.stop_mode_var.get() == "pct":
            self.stop_offset_var.set(self._stop_offset_pct)
        elif self.stop_mode_var.get() == "dollar":
            self.stop_offset_var.set(self._stop_offset_dollar)
        # Slippage
        self.slip_entry_enabled.set(self._saved_slip_entry_enabled)
        self.slip_exit_enabled.set(self._saved_slip_exit_enabled)
        self.slip_entry_mode.set(self._saved_slip_entry_mode)
        self.slip_exit_mode.set(self._saved_slip_exit_mode)
        self._slip_entry_pct = self._saved_slip_entry_pct
        self._slip_entry_dollar = self._saved_slip_entry_dollar
        self._slip_exit_pct = self._saved_slip_exit_pct
        self._slip_exit_dollar = self._saved_slip_exit_dollar
        self.slip_entry_var.set(
            self._slip_entry_pct if self.slip_entry_mode.get() == "pct"
            else self._slip_entry_dollar)
        self.slip_exit_var.set(
            self._slip_exit_pct if self.slip_exit_mode.get() == "pct"
            else self._slip_exit_dollar)

        # First-launch monitor seeding: if no monitor lock has ever been
        # configured, default it to the monitor the calc currently lives on.
        # Done lazily after Tk is mapped so winfo_* returns valid coords.
        if self.settings.get("ocr_monitor_rect") is None:
            self.root.after(50, self._seed_default_monitor_lock)

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        # --- STYLING ---
        self.style = ttk.Style()
        self.style.theme_use('clam')
        self.style.configure("TFrame", background=BG_COLOR)
        self.style.configure("TLabelframe", background=BG_COLOR, foreground=FG_COLOR, bordercolor=ACCENT_COLOR)
        self.style.configure("TLabelframe.Label", background=BG_COLOR, foreground=FG_COLOR)
        self.style.configure("TButton", background=ACCENT_COLOR, foreground=FG_COLOR, borderwidth=1)
        self.style.map("TButton", background=[("active", HIGHLIGHT)])
        self.style.configure("TRadiobutton", background=BG_COLOR, foreground=FG_COLOR, indicatorcolor=ENTRY_BG)
        self.style.map("TRadiobutton", indicatorcolor=[("selected", HIGHLIGHT)], background=[("active", BG_COLOR)])
        self.style.configure("TCheckbutton", background=BG_COLOR, foreground=FG_COLOR)
        self.style.configure("TEntry", fieldbackground=ENTRY_BG, foreground=ENTRY_FG, bordercolor=ACCENT_COLOR)
        # Yellow-tinted variant for "stale" entries (last OCR click failed
        # without producing a valid value). Cleared on next successful calc.
        self.style.configure("Stale.TEntry", fieldbackground=STALE_BG, foreground=ENTRY_FG, bordercolor=ACCENT_COLOR)
        self.style.configure("Treeview", background=BG_COLOR, foreground=FG_COLOR, fieldbackground=BG_COLOR, borderwidth=0)
        self.style.configure("Treeview.Heading", background=ACCENT_COLOR, foreground=FG_COLOR, relief="flat")
        self.style.map("Treeview", background=[("selected", HIGHLIGHT)])

        self.apply_font_sizing() 

        # --- GUI LAYOUT ---
        
        # Top Frame: Direction + Smart Click
        top_frame = ttk.Frame(root)
        top_frame.pack(fill="x", padx=15, pady=(10, 0))

        # Direction Radio Buttons
        r_long = ttk.Radiobutton(top_frame, text="Long", variable=self.direction_var, value="Long", command=self.calculate)
        r_short = ttk.Radiobutton(top_frame, text="Short", variable=self.direction_var, value="Short", command=self.calculate)
        r_long.pack(side="left", padx=(0, 10))
        r_short.pack(side="left")

        # Smart Click + LFA + Platform toggle
        if AUTOMATION_AVAILABLE:
            chk_lfa = ttk.Checkbutton(top_frame, text="LFA", variable=self.lfa_enabled)
            chk_lfa.pack(side="right", padx=(0, 5))
            self.chk_smart = ttk.Checkbutton(
                top_frame, text="Smart Click",
                variable=self.smart_click_enabled, command=self.toggle_listener)
            self.chk_smart.pack(side="right")

            # Platform radio buttons (TS / TV)
            sep_plat = ttk.Label(top_frame, text="|")
            sep_plat.pack(side="right", padx=4)
            r_tv = ttk.Radiobutton(top_frame, text="TV", variable=self.platform_var,
                                   value="TradingView", command=self._on_platform_change)
            r_tv.pack(side="right", padx=(0, 2))
            r_ts = ttk.Radiobutton(top_frame, text="TS", variable=self.platform_var,
                                   value="TradeStation", command=self._on_platform_change)
            r_ts.pack(side="right", padx=(0, 2))

            # Start the foreground-window poll (Win32-based LFA detection).
            # Replaces the old bind_all <FocusOut>/<FocusIn> approach which
            # was unreliable for app-level activation tracking.
            self._start_foreground_poll()
        else:
            lbl_err = ttk.Label(top_frame, text="(Smart Click Unavailable)", foreground="#888")
            lbl_err.pack(side="right")

        # Input Frame
        input_frame = ttk.LabelFrame(root, text=" Trade Inputs ", padding="10")
        input_frame.pack(fill="x", padx=10, pady=5)

        row = 0
        # Entry row with freeze checkbox
        ttk.Label(input_frame, text="Entry").grid(row=row, column=0, sticky="w", pady=5)
        entry_widget = ttk.Entry(input_frame, textvariable=self.vars["Entry"], width=15)
        entry_widget.grid(row=row, column=1, padx=5, pady=5, sticky="w")
        entry_widget.bind('<Return>', self.calculate)
        self.entry_widgets["Entry"] = entry_widget
        ttk.Checkbutton(input_frame, text="Freeze", variable=self.freeze_entry).grid(row=row, column=2, padx=(5, 0), sticky="w")
        row += 1

        # Stop row with freeze checkbox + mode picker (Manual / % / $)
        ttk.Label(input_frame, text="Stop").grid(row=row, column=0, sticky="w", pady=5)
        stop_widget = ttk.Entry(input_frame, textvariable=self.vars["Stop"], width=15)
        stop_widget.grid(row=row, column=1, padx=5, pady=5, sticky="w")
        stop_widget.bind('<Return>', self.calculate)
        self.entry_widgets["Stop"] = stop_widget
        # Mode picker — Combobox is compact and discoverable
        stop_mode_combo = ttk.Combobox(
            input_frame, textvariable=self.stop_mode_var,
            values=["manual", "pct", "dollar"], state="readonly", width=7)
        stop_mode_combo.grid(row=row, column=2, padx=(5, 0), sticky="w")
        ttk.Checkbutton(input_frame, text="Freeze",
                        variable=self.freeze_stop).grid(row=row, column=3, padx=(5, 0), sticky="w")
        row += 1

        # Stop Offset row (visible only when mode != manual). Reuses the
        # entry-widget infrastructure so it gets the stale-marker treatment
        # and Return-to-calculate binding for free.
        offset_label = ttk.Label(input_frame, text="Offset")
        offset_label.grid(row=row, column=0, sticky="w", pady=5)
        offset_entry = ttk.Entry(input_frame, textvariable=self.stop_offset_var, width=15)
        offset_entry.grid(row=row, column=1, padx=5, pady=5, sticky="w")
        offset_entry.bind('<Return>', self.calculate)
        offset_unit_label = ttk.Label(input_frame, text="%")
        offset_unit_label.grid(row=row, column=2, padx=(5, 0), sticky="w")
        self.entry_widgets["Stop Offset"] = offset_entry
        # Stash so _refresh_stop_offset_ui can grid_remove/grid() them as a unit
        self._stop_offset_widgets = {
            "row": [offset_label, offset_entry, offset_unit_label],
            "unit_label": offset_unit_label,
        }
        # Calculate on focus-out (so typing 5 + tab triggers a re-solve)
        offset_entry.bind('<FocusOut>', lambda e: self.calculate())
        row += 1

        # Risk $ row
        ttk.Label(input_frame, text="Risk $").grid(row=row, column=0, sticky="w", pady=5)
        risk_widget = ttk.Entry(input_frame, textvariable=self.vars["Risk $"], width=15)
        risk_widget.grid(row=row, column=1, padx=5, pady=5, sticky="w")
        risk_widget.bind('<Return>', self.calculate)
        self.entry_widgets["Risk $"] = risk_widget
        row += 1

        ttk.Label(input_frame, text="Shares").grid(row=row, column=0, sticky="w", pady=5)
        shares_entry = ttk.Entry(input_frame, textvariable=self.vars["Shares"], width=15)
        shares_entry.grid(row=row, column=1, padx=5, pady=5, sticky="w")
        shares_entry.bind('<Return>', self.calculate)
        self.entry_widgets["Shares"] = shares_entry

        ttk.Label(input_frame, text="Cost").grid(row=row, column=2, sticky="w", pady=5, padx=(10,5))
        cost_entry = ttk.Entry(input_frame, textvariable=self.vars["Cost"], width=12)
        cost_entry.grid(row=row, column=3, padx=5, pady=5, sticky="w")
        cost_entry.bind('<Return>', self.calculate)
        self.entry_widgets["Cost"] = cost_entry

        # Slippage Frame — compact two-side row with quick-toggle checkboxes,
        # mode picker (% / $), value entry, and unit suffix
        slip_frame = ttk.LabelFrame(root, text=" Slippage ", padding="6")
        slip_frame.pack(fill="x", padx=10, pady=(0, 5))

        def _build_slip_side(parent, col_start, label_text, enabled_var,
                             mode_var, value_var, key_prefix):
            ttk.Label(parent, text=label_text).grid(
                row=0, column=col_start, sticky="w", padx=(2, 2))
            ttk.Checkbutton(parent, variable=enabled_var).grid(
                row=0, column=col_start + 1, sticky="w", padx=(0, 4))
            mode_combo = ttk.Combobox(
                parent, textvariable=mode_var, values=["pct", "dollar"],
                state="readonly", width=6)
            mode_combo.grid(row=0, column=col_start + 2, padx=(0, 4))
            value_entry = ttk.Entry(parent, textvariable=value_var, width=8)
            value_entry.grid(row=0, column=col_start + 3, padx=(0, 2))
            value_entry.bind('<Return>', self.calculate)
            value_entry.bind('<FocusOut>', lambda e: self.calculate())
            unit = ttk.Label(parent, text="%")
            unit.grid(row=0, column=col_start + 4, padx=(0, 8))
            self.entry_widgets[key_prefix] = value_entry
            return unit

        entry_unit = _build_slip_side(
            slip_frame, 0, "Entry", self.slip_entry_enabled,
            self.slip_entry_mode, self.slip_entry_var, "Slip Entry")
        exit_unit = _build_slip_side(
            slip_frame, 5, "Exit", self.slip_exit_enabled,
            self.slip_exit_mode, self.slip_exit_var, "Slip Exit")
        self._slip_widgets = {"entry_unit": entry_unit, "exit_unit": exit_unit}

        # Buttons
        btn_frame = ttk.Frame(root)
        btn_frame.pack(pady=5)
        
        calc_btn = ttk.Button(btn_frame, text="Calculate", command=self.calculate)
        calc_btn.pack(side="left", padx=5)
        
        clear_btn = ttk.Button(btn_frame, text="Clear", command=self.clear_inputs)
        clear_btn.pack(side="left", padx=5)

        sep = ttk.Label(btn_frame, text="|")
        sep.pack(side="left", padx=5)
        
        btn_minus = ttk.Button(btn_frame, text="-", width=3, command=lambda: self.change_font_size(-1))
        btn_minus.pack(side="left", padx=2)
        
        btn_plus = ttk.Button(btn_frame, text="+", width=3, command=lambda: self.change_font_size(1))
        btn_plus.pack(side="left", padx=2)

        sep2 = ttk.Label(btn_frame, text="|")
        sep2.pack(side="left", padx=5)

        settings_btn = ttk.Button(btn_frame, text="Settings", command=self.open_settings)
        settings_btn.pack(side="left", padx=5)

        # Output Table
        self.tree = ttk.Treeview(root, columns=("Level", "Price", "PnL"), show="headings", height=6)
        self.tree.heading("Level", text="Level")
        self.tree.heading("Price", text="Price")
        self.tree.heading("PnL", text="P/L ($)")
        
        self.tree.column("Level", width=80, anchor="center")
        self.tree.column("Price", width=80, anchor="center")
        self.tree.column("PnL", width=80, anchor="center")
        
        self.tree.pack(padx=10, pady=10, fill="both", expand=True)

        self.tree.tag_configure('stop', foreground='#ff6b6b')
        self.tree.tag_configure('entry', foreground='#4dabf7')
        self._apply_target_tags()

        # Status bar for user feedback
        self.status_label = ttk.Label(root, text="", foreground="#888888", background=BG_COLOR)
        self.status_label.pack(padx=10, pady=(0, 5))
        self._status_after_id = None

        # Listener + clipboard poll are activated lazily by toggle_listener
        if AUTOMATION_AVAILABLE and self.smart_click_enabled.get():
            self.toggle_listener()

        # Grey out Smart Click whenever there's no fillable target. Triggers
        # on freeze toggles AND on stop-mode changes (offset mode + Entry
        # frozen also has no target).
        self.freeze_entry.trace_add("write", lambda *a: self._update_smart_click_state())
        self.freeze_stop.trace_add("write", lambda *a: self._update_smart_click_state())
        self.stop_mode_var.trace_add("write", lambda *a: self._update_smart_click_state())
        self._update_smart_click_state()

        # Stop-mode + slippage-mode change handlers
        self._prev_stop_mode = self.stop_mode_var.get()
        self._prev_slip_entry_mode = self.slip_entry_mode.get()
        self._prev_slip_exit_mode = self.slip_exit_mode.get()
        self.stop_mode_var.trace_add("write", self._on_stop_mode_change)
        self.slip_entry_mode.trace_add("write", lambda *a: self._on_slip_mode_change("entry"))
        self.slip_exit_mode.trace_add("write", lambda *a: self._on_slip_mode_change("exit"))
        # Re-solve when slippage is toggled or the offset/slippage values are typed in
        self.slip_entry_enabled.trace_add("write", lambda *a: self.calculate())
        self.slip_exit_enabled.trace_add("write", lambda *a: self.calculate())
        # First-time UI sync to reflect the loaded mode
        self._refresh_stop_offset_ui()
        self._refresh_slip_unit_labels()

    # --- AUTOMATION LOGIC ---
    def _start_listener(self):
        """Start the mouse listener (TS mode). Idempotent."""
        if self.listener is not None and self.listener.running:
            return
        self.listener = mouse.Listener(on_click=self.on_click)
        self.listener.start()

    def _stop_listener(self):
        """Stop the mouse listener if running. Idempotent."""
        if self.listener is None:
            return
        try:
            self.listener.stop()
        except Exception:
            pass
        self.listener = None

    def _update_smart_click_state(self):
        """Disable the Smart Click checkbox whenever Smart Click would have
        no fillable target. Catches: both-frozen, offset-mode + Entry-frozen,
        and any future routing changes that return None."""
        chk = getattr(self, "chk_smart", None)
        if chk is None:
            return
        try:
            if self._smart_fill_field() is None:
                chk.state(["disabled"])
            else:
                chk.state(["!disabled"])
        except tk.TclError:
            pass

    def toggle_listener(self):
        """Manage Smart Click activation across both platforms.
        TS mode runs the mouse listener; TV mode runs clipboard polling.
        Both are off when Smart Click is disabled."""
        if not AUTOMATION_AVAILABLE:
            return
        if self.smart_click_enabled.get():
            if self.platform_var.get() == "TradingView":
                self._stop_listener()
                self._start_clipboard_poll()
            else:
                self._stop_clipboard_poll()
                self._start_listener()
        else:
            self._stop_listener()
            self._stop_clipboard_poll()

    def _start_foreground_poll(self):
        """Begin the periodic Win32 GetForegroundWindow poll. Idempotent."""
        if self._foreground_poll_id is not None:
            return
        # Seed the baseline so the first poll tick doesn't fire a spurious
        # "transition from 0 to current" stamp on app launch.
        self._last_foreground_hwnd = self._current_foreground_hwnd()
        self._poll_foreground()

    def _stop_foreground_poll(self):
        if self._foreground_poll_id is not None:
            try:
                self.root.after_cancel(self._foreground_poll_id)
            except (RuntimeError, tk.TclError):
                pass
            self._foreground_poll_id = None

    def _current_foreground_hwnd(self):
        """Return int HWND of the current foreground top-level, or 0."""
        if not _WIN32_AVAILABLE:
            return 0
        try:
            fg = _user32.GetForegroundWindow()
            if not fg:
                return 0
            top = _user32.GetAncestor(fg, _GA_ROOT) or fg
            return int(top)
        except (OSError, AttributeError):
            return 0

    def _poll_foreground(self):
        """100ms poll. Stamps _last_app_switch_ts on ANY change of the
        Windows foreground HWND. Maximally permissive: every app↔app
        transition counts (calc↔TS, browser↔TS, TS↔Slack — all of them)."""
        if self._closing:
            self._foreground_poll_id = None
            return
        cur = self._current_foreground_hwnd()
        if cur and cur != self._last_foreground_hwnd:
            self._last_app_switch_ts = time.time()
            self._last_foreground_hwnd = cur
        try:
            self._foreground_poll_id = self.root.after(100, self._poll_foreground)
        except (RuntimeError, tk.TclError):
            self._foreground_poll_id = None

    def _on_platform_change(self):
        """Sync platform_var into settings for persistence and switch modes."""
        self.settings["platform"] = self.platform_var.get()
        # Restart the active mode if Smart Click is on
        if self.smart_click_enabled.get():
            self._stop_clipboard_poll()
            if self.platform_var.get() == "TradingView":
                self._start_clipboard_poll()

    # --- TV MODE: Clipboard Polling ---
    def _start_clipboard_poll(self):
        """Begin polling clipboard for price values (TV mode). Idempotent."""
        if self._polling_active:
            return
        # Snapshot current clipboard so we don't immediately consume stale data
        try:
            self._last_clipboard = self.root.clipboard_get()
        except tk.TclError:
            self._last_clipboard = ""
        self._polling_active = True
        self._poll_clipboard()

    def _stop_clipboard_poll(self):
        """Cancel clipboard polling. Idempotent. The _polling_active flag is
        the authoritative gate: even if a poll callback is mid-flight, it
        will see the flag and not reschedule."""
        self._polling_active = False
        if self._clipboard_poll_id:
            try:
                self.root.after_cancel(self._clipboard_poll_id)
            except (RuntimeError, tk.TclError):
                pass
            self._clipboard_poll_id = None

    def _poll_clipboard(self):
        """Check clipboard every 200ms for a new price value (TV mode).
        The _polling_active flag is checked BOTH at entry and before reschedule
        so this loop terminates cleanly even if stop arrives mid-tick."""
        if self._closing or not self._polling_active:
            self._clipboard_poll_id = None
            return
        try:
            current = self.root.clipboard_get()
        except tk.TclError:
            current = ""

        if current != self._last_clipboard:
            self._last_clipboard = current
            cleaned = current.strip()
            # Strict: must be N.D... (1-4 fractional digits). Bare integers and
            # passwords/IDs that happen to be numeric are rejected.
            if _CLIPBOARD_PRICE_RE.match(cleaned):
                try:
                    price_val = float(cleaned)
                except ValueError:
                    price_val = None
                if price_val is not None and 0.01 <= price_val <= 999999:
                    self.auto_fill_price(cleaned)
                    # Clear last_clipboard so an identical re-copy will trigger again
                    self._last_clipboard = ""

        # Re-check before rescheduling to avoid a zombie loop if we were
        # stopped between the entry check and now.
        if self._polling_active and not self._closing:
            try:
                self._clipboard_poll_id = self.root.after(200, self._poll_clipboard)
            except (RuntimeError, tk.TclError):
                self._clipboard_poll_id = None
        else:
            self._clipboard_poll_id = None

    def _click_inside_window(self, x, y):
        """Return True if screen coords (x, y) fall inside the calculator —
        including title bar, frame, and any child windows. Uses Win32
        WindowFromPoint, which correctly handles DPI scaling, multi-monitor,
        z-order, and the non-client (chrome) area. Falls back to Tk's
        winfo_rootx/y/width/height (client area only) if Win32 is unavailable."""
        if _WIN32_AVAILABLE:
            try:
                pt = wintypes.POINT(int(x), int(y))
                hwnd_at = _user32.WindowFromPoint(pt)
                if not hwnd_at:
                    return False
                # Walk up to the top-level for both — Tk widgets and child
                # controls return their own HWNDs, so we compare roots.
                top_at = _user32.GetAncestor(hwnd_at, _GA_ROOT) or hwnd_at
                my_id = int(self.root.winfo_id())
                my_top = _user32.GetAncestor(my_id, _GA_ROOT) or my_id
                return int(top_at) == int(my_top)
            except (OSError, AttributeError, tk.TclError):
                pass
        # Fallback (client area only — title bar/frame clicks will read as outside)
        try:
            wx = self.root.winfo_rootx()
            wy = self.root.winfo_rooty()
            return wx <= x <= wx + self.root.winfo_width() and wy <= y <= wy + self.root.winfo_height()
        except tk.TclError:
            return False

    def on_click(self, x, y, button, pressed):
        """Runs on the pynput listener thread. Tk is NOT thread-safe, so do
        zero Tk work here — hop to the main thread immediately."""
        if not pressed:
            return
        if self._closing:
            return
        try:
            self.root.after(0, self._handle_click_main, x, y)
        except (RuntimeError, tk.TclError):
            # Main loop tearing down — drop the click silently
            pass

    def _handle_click_main(self, x, y):
        """Main-thread click handler. Safe to read Tk vars / widget geometry."""
        if self._closing:
            return
        if not self.smart_click_enabled.get():
            return
        # Ignore clicks inside the calculator itself
        if self._click_inside_window(x, y):
            return
        # In TV mode, clicks are ignored — clipboard polling handles it
        if self.platform_var.get() == "TradingView":
            return

        # Monitor lock: only OCR clicks on the configured monitor. Other
        # monitors have different sizes/DPI and the fixed pixel capture
        # box would produce garbage. User selects in Settings → Smart
        # Click Monitor. Null/None means "no lock — any monitor allowed".
        locked_rect = self.settings.get("ocr_monitor_rect")
        if locked_rect:
            click_rect = _monitor_rect_at_point(x, y)
            if click_rect is not None and tuple(click_rect) != tuple(locked_rect):
                self._show_status("Smart Click off this monitor")
                return

        # Bail (with status) when Smart Click has no fillable target. This
        # covers: (1) both freezes on, (2) offset mode + Entry frozen,
        # (3) any future routing that returns None.
        if self._smart_fill_field() is None:
            if self.stop_mode_var.get() != "manual" and self.freeze_entry.get():
                self._show_status("Offset mode + Entry frozen — Smart Click idle")
            else:
                self._show_status("Both fields frozen — Smart Click idle")
            return

        if not self._ocr_lock.acquire(blocking=False):
            return  # Already processing a previous click

        # Inline transition check at the click target. Stamp if EITHER:
        #   (a) the top-level HWND at the click point differs from what
        #       was last foreground, OR
        #   (b) the monitor the click landed on is different from where
        #       the previous click landed.
        #
        # Monitor is needed because multi-monitor TS often presents many
        # chart windows that all roll up to the same top-level HWND under
        # GetAncestor(GA_ROOT) — so cross-monitor TS clicks would otherwise
        # look like "same window" and fail to trigger LFA.
        if _WIN32_AVAILABLE:
            try:
                pt = wintypes.POINT(int(x), int(y))
                hwnd_at = _user32.WindowFromPoint(pt)
                hmon_at = _user32.MonitorFromPoint(pt, _MONITOR_DEFAULTTONEAREST)
                click_target = 0
                if hwnd_at:
                    top_at = _user32.GetAncestor(hwnd_at, _GA_ROOT) or hwnd_at
                    click_target = int(top_at)
                click_monitor = int(hmon_at) if hmon_at else 0

                hwnd_changed = (click_target != 0
                                and click_target != self._last_foreground_hwnd)
                monitor_changed = (click_monitor != 0
                                   and click_monitor != self._last_click_monitor)

                if hwnd_changed or monitor_changed:
                    self._last_app_switch_ts = time.time()
                    if click_target:
                        self._last_foreground_hwnd = click_target
                    if click_monitor:
                        self._last_click_monitor = click_monitor
            except (OSError, AttributeError):
                pass

        # Snapshot every Tk value the worker might need (Tk is not thread-safe).
        delay = self._compute_ocr_delay()
        # Diagnostic: tell the user which delay was used (helps tune LFA)
        delay_label = "LFA" if delay >= self.settings["lfa_delay"] else "normal"
        self._show_status(f"OCR: {delay_label} {int(delay * 1000)}ms")
        settings_snapshot = {
            "ocr_left": self.settings["ocr_left"],
            "ocr_right": self.settings["ocr_right"],
            "ocr_above": self.settings["ocr_above"],
            "ocr_below": self.settings["ocr_below"],
            # Also snapshot lfa_delay + debug_mode here so the worker
            # thread never reads from the live settings dict. Without
            # the snapshot, opening Settings and clicking Save while a
            # click is in flight could let the worker observe a stale
            # delay or debug-mode value — small window, real race.
            "lfa_delay": self.settings["lfa_delay"],
            "ocr_debug_mode": bool(self.settings.get("ocr_debug_mode")),
        }

        # INSTANT UI FEEDBACK: Show "..." so the app feels instantly responsive
        self.indicate_loading()

        # Run OCR in a separate thread so we don't block the UI
        self._ocr_thread = threading.Thread(
            target=self.process_click, args=(x, y, delay, settings_snapshot),
            daemon=True)
        self._ocr_thread.start()

    def _direction_compatible(self, target, new_value_str):
        """True if filling `target` with `new_value_str` would still be
        consistent with the currently-selected Long/Short direction. If only
        one of Entry/Stop will be populated after the fill, returns True
        (no constraint to check yet).

        In stop-offset mode, Stop is auto-derived from Entry ± offset and
        is always direction-consistent by construction. The old Stop value
        becomes stale the moment Entry changes, so we bypass the check
        entirely — any positive-numeric Entry is accepted and Stop will be
        recomputed downstream by calculate(). This lets the user click a
        new entry below the previous derived stop (Long) or above it
        (Short) without triggering a spurious 'wrong direction' rejection."""
        try:
            new_val = float(new_value_str)
        except (ValueError, TypeError):
            return False

        if self.stop_mode_var.get() != "manual":
            return True

        def _to_num(v):
            try:
                return float(v)
            except (ValueError, TypeError):
                return None

        entry = _to_num(self.vars["Entry"].get())
        stop = _to_num(self.vars["Stop"].get())
        if target == "Entry":
            entry = new_val
        else:
            stop = new_val
        if entry is None or stop is None:
            return True  # Not enough to check yet
        if entry == stop:
            return False
        if self.direction_var.get() == "Long":
            return stop < entry
        return stop > entry  # Short

    def _smart_fill_field(self):
        """Determine which field Smart Click should fill, respecting freeze states.
        Returns 'Entry', 'Stop', or None (both frozen / unable to fill).

        In Stop-offset mode, Stop is derived from Entry+offset and is not a
        valid OCR target — so we always route to Entry (or None if Entry is
        frozen, since there's nothing left to fill)."""
        e_frozen = self.freeze_entry.get()
        s_frozen = self.freeze_stop.get()
        if self.stop_mode_var.get() != "manual":
            return None if e_frozen else "Entry"
        if e_frozen and s_frozen:
            return None
        if e_frozen:
            return "Stop"
        if s_frozen:
            return "Entry"
        # Neither frozen — use alternating logic
        return "Entry" if self.entry_turn else "Stop"

    def indicate_loading(self):
        """Instantly puts a '...' in the box we are about to fill, after
        snapshotting the prior value so a failed OCR can restore it."""
        target = self._smart_fill_field()
        if target:
            current = self.vars[target].get()
            # Don't snapshot a stale "..." (would erase the real prior value)
            if current != "...":
                self._pre_click_values[target] = current
            self.vars[target].set("...")

    def _mark_stale(self, fields=("Stop", "Shares")):
        """Apply the yellow Stale.TEntry tint. Survives until next successful calc."""
        for f in fields:
            w = self.entry_widgets.get(f)
            if w is None:
                continue
            try:
                w.configure(style="Stale.TEntry")
            except tk.TclError:
                pass

    def _clear_stale(self):
        """Restore the default style on every input. Called when a calc
        successfully renders the table (i.e., user has fresh good values)."""
        for w in self.entry_widgets.values():
            try:
                w.configure(style="TEntry")
            except tk.TclError:
                pass

    # --- Debug capture pane ---
    def _ensure_debug_window(self):
        """Lazily create the debug Toplevel + its labels. Idempotent."""
        if self._debug_win is not None:
            try:
                # Validate it still exists (could have been closed by user)
                self._debug_win.winfo_exists()
                return
            except tk.TclError:
                self._debug_win = None
        win = tk.Toplevel(self.root)
        win.title("OCR Debug")
        win.configure(bg=BG_COLOR)
        win.attributes('-topmost', True)
        win.protocol("WM_DELETE_WINDOW", win.withdraw)
        # Layout: raw image | processed image | text
        ttk.Label(win, text="Raw capture:", background=BG_COLOR,
                  foreground=FG_COLOR).pack(anchor="w", padx=8, pady=(8, 0))
        self._debug_raw_label = ttk.Label(win, background=BG_COLOR)
        self._debug_raw_label.pack(padx=8, pady=4)
        ttk.Label(win, text="Processed (Tesseract input):", background=BG_COLOR,
                  foreground=FG_COLOR).pack(anchor="w", padx=8, pady=(8, 0))
        self._debug_proc_label = ttk.Label(win, background=BG_COLOR)
        self._debug_proc_label.pack(padx=8, pady=4)
        self._debug_text_label = ttk.Label(
            win, background=BG_COLOR, foreground=FG_COLOR,
            font=("Consolas", 9), justify="left", anchor="w", wraplength=600)
        self._debug_text_label.pack(fill="x", padx=8, pady=(4, 8))
        self._debug_win = win

    def _show_debug_capture(self, raw_img, proc_img, ocr_text, parsed):
        """Main-thread call from the OCR worker. Displays the raw and
        processed images plus the OCR text in the debug window."""
        if self._closing:
            return
        if not self.settings.get("ocr_debug_mode"):
            return
        try:
            self._ensure_debug_window()
            # Re-show if user previously withdrew it
            self._debug_win.deiconify()

            # Keep image dimensions reasonable so the window doesn't go off-screen
            def _scaled_photo(img, max_w=600, max_h=300):
                w, h = img.size
                scale = min(max_w / w, max_h / h, 1.0)
                if scale < 1.0:
                    img = img.resize((int(w * scale), int(h * scale)),
                                     Image.Resampling.LANCZOS)
                return ImageTk.PhotoImage(img)

            raw_photo = _scaled_photo(raw_img)
            proc_photo = _scaled_photo(proc_img)
            self._debug_raw_label.configure(image=raw_photo)
            self._debug_proc_label.configure(image=proc_photo)
            # Replace ref list (Tk would otherwise GC the prior image)
            self._debug_imgrefs = [raw_photo, proc_photo]

            ocr_disp = (ocr_text or "").strip().replace("\n", " | ")
            if len(ocr_disp) > 200:
                ocr_disp = ocr_disp[:200] + "…"
            self._debug_text_label.configure(
                text=f"OCR text: {ocr_disp!r}\nParsed: {parsed!r}")
        except (tk.TclError, RuntimeError):
            pass

    # --- Stop-offset / slippage persistence helpers ---
    def _stash_current_stop_offset(self):
        """Copy the displayed offset value into the slot for the current mode."""
        val = self.stop_offset_var.get().strip()
        mode = self.stop_mode_var.get()
        if mode == "pct":
            self._stop_offset_pct = val
        elif mode == "dollar":
            self._stop_offset_dollar = val
        # manual mode: nothing to stash

    def _stash_current_slip_value(self, side):
        """Copy the displayed slippage value into the slot for that side+mode."""
        if side == "entry":
            val = self.slip_entry_var.get().strip()
            mode = self.slip_entry_mode.get()
            if mode == "pct":
                self._slip_entry_pct = val
            else:
                self._slip_entry_dollar = val
        else:
            val = self.slip_exit_var.get().strip()
            mode = self.slip_exit_mode.get()
            if mode == "pct":
                self._slip_exit_pct = val
            else:
                self._slip_exit_dollar = val

    def _on_stop_mode_change(self, *_args):
        """Stop-mode changed: stash old mode's value, load new mode's value,
        toggle the visibility of the offset row, and toggle Stop's read-only
        state. Trigger a re-solve so the table follows the new mode."""
        # Determine previous mode: read what the displayed value HAS to be
        # right now is for the new mode. The trick: we use a class-level
        # _prev_stop_mode to know which slot to stash to.
        prev_mode = getattr(self, "_prev_stop_mode", "manual")
        new_mode = self.stop_mode_var.get()
        # Stash the displayed value back into the slot for prev_mode
        if prev_mode == "pct":
            self._stop_offset_pct = self.stop_offset_var.get().strip()
        elif prev_mode == "dollar":
            self._stop_offset_dollar = self.stop_offset_var.get().strip()
        # Load the slot for the new mode into the displayed offset
        if new_mode == "pct":
            self.stop_offset_var.set(self._stop_offset_pct)
        elif new_mode == "dollar":
            self.stop_offset_var.set(self._stop_offset_dollar)
        else:
            self.stop_offset_var.set("")
        self._prev_stop_mode = new_mode
        # Update widget visibility and stop-field editability, then re-solve
        self._refresh_stop_offset_ui()
        self.calculate()

    def _on_slip_mode_change(self, side):
        """Slippage mode changed for one side: stash + load like stop mode."""
        if side == "entry":
            prev = getattr(self, "_prev_slip_entry_mode", self.slip_entry_mode.get())
            new = self.slip_entry_mode.get()
            if prev == "pct":
                self._slip_entry_pct = self.slip_entry_var.get().strip()
            else:
                self._slip_entry_dollar = self.slip_entry_var.get().strip()
            self.slip_entry_var.set(
                self._slip_entry_pct if new == "pct" else self._slip_entry_dollar)
            self._prev_slip_entry_mode = new
        else:
            prev = getattr(self, "_prev_slip_exit_mode", self.slip_exit_mode.get())
            new = self.slip_exit_mode.get()
            if prev == "pct":
                self._slip_exit_pct = self.slip_exit_var.get().strip()
            else:
                self._slip_exit_dollar = self.slip_exit_var.get().strip()
            self.slip_exit_var.set(
                self._slip_exit_pct if new == "pct" else self._slip_exit_dollar)
            self._prev_slip_exit_mode = new
        self._refresh_slip_unit_labels()
        self.calculate()

    def _refresh_stop_offset_ui(self):
        """Show/hide the offset row, swap the unit suffix, and toggle the
        editability of the Stop field based on the current stop mode."""
        widgets = getattr(self, "_stop_offset_widgets", None)
        if not widgets:
            return  # UI not built yet (called during __init__ before grid)
        mode = self.stop_mode_var.get()
        if mode == "manual":
            for w in widgets["row"]:
                w.grid_remove()
            self.entry_widgets["Stop"].configure(state="normal")
        else:
            for w in widgets["row"]:
                w.grid()
            widgets["unit_label"].configure(text="%" if mode == "pct" else "$")
            # Stop field becomes display-only — value is computed
            self.entry_widgets["Stop"].configure(state="readonly")

    def _refresh_slip_unit_labels(self):
        widgets = getattr(self, "_slip_widgets", None)
        if not widgets:
            return
        widgets["entry_unit"].configure(
            text="%" if self.slip_entry_mode.get() == "pct" else "$")
        widgets["exit_unit"].configure(
            text="%" if self.slip_exit_mode.get() == "pct" else "$")

    # --- Stop-offset math ---
    def _compute_stop_from_offset(self, entry, mode_override=None):
        """Given Entry, compute Stop from the active offset. Returns None if
        the offset value isn't a positive number or mode is 'manual'."""
        mode = mode_override or self.stop_mode_var.get()
        if mode == "manual":
            return None
        raw = self.stop_offset_var.get().strip()
        if not raw:
            return None
        try:
            offset = float(raw)
        except ValueError:
            return None
        if offset <= 0:
            return None
        direction = self.direction_var.get()
        if mode == "pct":
            delta = entry * offset / 100.0
        else:  # "dollar"
            delta = offset
        # Long: stop is BELOW entry; Short: stop is ABOVE entry
        return (entry - delta) if direction == "Long" else (entry + delta)

    # --- Slippage math ---
    def _slippage_per_share(self, side, level_price):
        """Return the dollars-per-share slippage for one side at a given price
        level. Pct-mode slippage scales with the level price; dollar-mode is flat.
        Returns 0 if that side is disabled or the value is invalid/non-positive."""
        if side == "entry":
            if not self.slip_entry_enabled.get():
                return 0.0
            mode = self.slip_entry_mode.get()
            raw = self.slip_entry_var.get().strip()
        else:
            if not self.slip_exit_enabled.get():
                return 0.0
            mode = self.slip_exit_mode.get()
            raw = self.slip_exit_var.get().strip()
        if not raw:
            return 0.0
        try:
            v = float(raw)
        except ValueError:
            return 0.0
        if v <= 0:
            return 0.0
        if mode == "pct":
            return level_price * v / 100.0
        return v

    def _effective_risk_per_share(self, entry, stop):
        """Worst-case risk per share including slippage. Used for Shares
        derivation in model (a): Shares = floor(Risk / effective_risk_per_share).

        Long:  buy at (entry + slip_e), sell at (stop - slip_x)
        Short: sell at (entry - slip_e), buy at (stop + slip_x)
        Both directions give the same magnitude formula:
            ideal_rps + slip_e@entry + slip_x@stop"""
        ideal = abs(entry - stop)
        slip_e = self._slippage_per_share("entry", entry)
        slip_x = self._slippage_per_share("exit", stop)
        return ideal + slip_e + slip_x

    def _compute_ocr_delay(self):
        """Determine OCR delay (main thread only).

        LFA delay fires when:
          - fields_empty (cold start of a new trade), OR
          - the Windows foreground app changed within the last
            _LFA_RECENT_WINDOW_S seconds (any app↔app switch counts)

        Foreground tracking is via 100ms GetForegroundWindow poll — see
        _poll_foreground."""
        if not self.lfa_enabled.get():
            return self.settings["normal_delay"]
        e_val = self.vars["Entry"].get()
        s_val = self.vars["Stop"].get()
        fields_empty = (not e_val or e_val == "...") and (not s_val or s_val == "...")
        recent_switch = (time.time() - self._last_app_switch_ts) < self._LFA_RECENT_WINDOW_S
        if fields_empty or recent_switch:
            return self.settings["lfa_delay"]
        return self.settings["normal_delay"]

    def _ocr_image(self, screen):
        """Image-processing pipeline: grayscale → 3x upscale → contrast → sharpness → OCR.
        Returns (ocr_text, processed_pil_image) so the debug pane can show
        what Tesseract actually saw."""
        screen = ImageOps.grayscale(screen)
        width, height = screen.size
        screen = screen.resize((width * 3, height * 3), Image.Resampling.LANCZOS)
        screen = ImageEnhance.Contrast(screen).enhance(2.0)
        screen = ImageEnhance.Sharpness(screen).enhance(2.0)
        return pytesseract.image_to_string(screen, config='--psm 6'), screen

    def _process_ts(self, x, y, settings_snapshot, debug_mode=False):
        """TradeStation OCR: capture region around click, look for labeled price.
        Pure worker-thread function — no Tk reads, uses pre-snapshotted settings.
        When debug_mode is True, schedules a main-thread debug-window update
        with the captured image + OCR text on every click."""
        s = settings_snapshot
        bbox = (max(0, x - s["ocr_left"]), max(0, y - s["ocr_above"]),
                x + s["ocr_right"], y + s["ocr_below"])
        raw = ImageGrab.grab(bbox=bbox)
        text, processed = self._ocr_image(raw)

        match = _OCR_LABELED_PRICE_RE.search(text)
        parsed = match.group(1) if match else None
        if not parsed:
            fallback_matches = _OCR_FALLBACK_PRICE_RE.findall(text)
            if fallback_matches:
                parsed = fallback_matches[-1]

        if debug_mode:
            # Pass PIL images (raw bytes safe across thread boundaries) +
            # the text strings. Main thread will convert via ImageTk.
            self._safe_after(
                0, self._show_debug_capture, raw, processed, text, parsed)

        return parsed

    def process_click(self, x, y, delay, settings_snapshot):
        """Worker thread. Sleeps `delay`, runs OCR, schedules a main-thread
        autofill if successful. _ensure_unlock is always scheduled in the
        finally block so the lock can never leak.

        If the first attempt was an LFA-delay click and OCR returned no
        valid price, we automatically retry once after sleeping an
        additional 0.5 × delay (total wait = 1.5 × delay). This recovers
        the common 'chart not quite finished rendering' case that a single
        LFA delay sometimes misses.

        ``settings_snapshot`` carries ``lfa_delay`` and ``ocr_debug_mode``
        captured at click time so the worker never reads from the live
        ``self.settings`` dict — opening Settings + Save mid-click could
        otherwise let this worker observe a stale value."""
        debug = settings_snapshot["ocr_debug_mode"]
        lfa_delay = settings_snapshot["lfa_delay"]

        def _try_ocr():
            text = self._process_ts(x, y, settings_snapshot, debug_mode=debug)
            if not text:
                return None
            try:
                v = float(text)
            except ValueError:
                return None
            if 0.01 <= v <= 999999:
                return text
            return None

        try:
            time.sleep(delay)
            price_str = _try_ocr()

            if price_str is None and delay >= lfa_delay:
                # LFA delay wasn't enough — give the chart a bit more time
                time.sleep(delay * 0.5)
                price_str = _try_ocr()
                if price_str is not None:
                    self._safe_after(
                        0, self._show_status,
                        "OCR retry succeeded (consider raising LFA delay)")

            if price_str is not None:
                self._safe_after(0, self.auto_fill_price, price_str)
        except Exception as e:
            print(f"OCR error: {e}")
        finally:
            self._safe_after(0, self._ensure_unlock)

    def _safe_after(self, delay_ms, func, *args):
        """root.after that swallows TclError/RuntimeError during shutdown."""
        if self._closing:
            return
        try:
            self.root.after(delay_ms, func, *args)
        except (RuntimeError, tk.TclError):
            pass

    def _ensure_unlock(self):
        """Guaranteed cleanup: restore prior values if OCR failed (instead of
        blanking, which would destroy the user's good prior values), and
        release the OCR lock. Idempotent.

        On failure we also mark Stop+Shares yellow so the user knows the
        last click was wasted and that previously-derived values are stale.
        Yellow clears on the next successful calculation."""
        try:
            ocr_failed = False
            for fld in ("Entry", "Stop"):
                if self.vars[fld].get() == "...":
                    ocr_failed = True
                    prev = self._pre_click_values.pop(fld, "")
                    self.vars[fld].set(prev)
            if ocr_failed:
                self._mark_stale()
        except tk.TclError:
            pass
        try:
            if self._ocr_lock.locked():
                self._ocr_lock.release()
        except RuntimeError:
            pass

    def auto_fill_price(self, price):
        target = self._smart_fill_field()
        if target is None:
            return  # Both frozen — do nothing

        # Direction-consistency check: silently reject prices that would
        # create a setup contradicting the user-selected Long/Short. We
        # leave _pre_click_values intact so _ensure_unlock restores the
        # prior value and applies the wasted-click yellow tint — same UX
        # as a failed OCR.
        if not self._direction_compatible(target, price):
            return

        # Successful OCR → previously-cached "..." value is no longer needed
        self._pre_click_values.pop(target, None)

        # Clear Shares/Cost when filling Stop (new trade leg)
        if target == "Stop":
            self.vars["Shares"].set("")
            self.vars["Cost"].set("")

        self.vars[target].set(price)

        # Advance alternating toggle only when neither field is frozen
        if not self.freeze_entry.get() and not self.freeze_stop.get():
            self.entry_turn = target != "Entry"  # flip

        self.calculate()
        # Lock is released by _ensure_unlock (scheduled in process_click's finally block)

    # --- SETTINGS LOGIC ---
    def _apply_target_tags(self):
        """Create/update Treeview tags for each target with its configured color."""
        for i, t in enumerate(self.settings["targets"]):
            self.tree.tag_configure(f"target_{i}", foreground=t["color"])
        # Adjust treeview height: STOP + ENTRY + N targets
        self.tree.configure(height=2 + len(self.settings["targets"]))

    def _apply_settings(self):
        """Push current settings into the live app."""
        self._apply_target_tags()
        # Re-render the table if data is present
        try:
            v_entry = self.vars["Entry"].get()
            v_stop = self.vars["Stop"].get()
            v_shares = self.vars["Shares"].get()
            if v_entry and v_stop and v_shares:
                self.update_table(float(v_entry), float(v_stop), float(v_shares))
        except (ValueError, TypeError):
            pass

    def open_settings(self):
        """Open a modal settings window."""
        win = tk.Toplevel(self.root)
        win.title("Settings")
        win.configure(bg=BG_COLOR)
        win.transient(self.root)
        win.grab_set()
        win.resizable(False, False)

        # Working copy of settings so Cancel discards changes
        working = copy.deepcopy(self.settings)

        # --- OCR Timing ---
        timing_frame = ttk.LabelFrame(win, text=" OCR Timing ", padding="10")
        timing_frame.pack(fill="x", padx=10, pady=(10, 5))

        ttk.Label(timing_frame, text="Normal Delay (s):").grid(row=0, column=0, sticky="w", pady=3)
        var_normal = tk.StringVar(value=str(working["normal_delay"]))
        ttk.Entry(timing_frame, textvariable=var_normal, width=10).grid(row=0, column=1, padx=5, pady=3)

        ttk.Label(timing_frame, text="LFA Delay (s):").grid(row=1, column=0, sticky="w", pady=3)
        var_lfa = tk.StringVar(value=str(working["lfa_delay"]))
        ttk.Entry(timing_frame, textvariable=var_lfa, width=10).grid(row=1, column=1, padx=5, pady=3)

        # --- OCR Capture Region (TS) ---
        region_frame = ttk.LabelFrame(win, text=" TradeStation Capture Region (px from click) ", padding="10")
        region_frame.pack(fill="x", padx=10, pady=5)

        ocr_vars = {}
        for i, (label, key) in enumerate([("Left:", "ocr_left"), ("Right:", "ocr_right"),
                                           ("Above:", "ocr_above"), ("Below:", "ocr_below")]):
            r, c = divmod(i, 2)
            ttk.Label(region_frame, text=label).grid(row=r, column=c * 2, sticky="w", pady=3)
            v = tk.StringVar(value=str(working[key]))
            ttk.Entry(region_frame, textvariable=v, width=8).grid(row=r, column=c * 2 + 1, padx=5, pady=3)
            ocr_vars[key] = v

        # Preset save/load row
        preset_row = ttk.Frame(region_frame)
        preset_row.grid(row=2, column=0, columnspan=4, pady=(8, 0), sticky="w")

        def _do_save_preset():
            from tkinter import simpledialog
            name = simpledialog.askstring(
                "Save Preset",
                "Preset name:",
                parent=win)
            if not name:
                return
            try:
                region = {k: int(float(ocr_vars[k].get())) for k in ocr_vars}
            except ValueError:
                messagebox.showwarning("Invalid Region",
                                       "Capture region values must be numeric.",
                                       parent=win)
                return
            try:
                _save_preset(name, region)
                messagebox.showinfo("Saved", f"Preset '{_sanitize_preset_name(name)}' saved.",
                                    parent=win)
            except OSError as e:
                messagebox.showwarning("Save Failed", str(e), parent=win)

        def _do_load_preset():
            names = _list_preset_names()
            if not names:
                messagebox.showinfo("No Presets",
                                    f"No presets found in {_PRESETS_DIR}.",
                                    parent=win)
                return
            # Picker dialog
            picker = tk.Toplevel(win)
            picker.title("Load Preset")
            picker.transient(win)
            picker.grab_set()
            picker.configure(bg=BG_COLOR)
            ttk.Label(picker, text="Select a preset to load:").pack(padx=10, pady=(10, 5))
            sel = tk.StringVar(value=names[0])
            ttk.Combobox(picker, textvariable=sel, values=names,
                         state="readonly", width=30).pack(padx=10, pady=5)

            def _apply():
                preset = _load_preset(sel.get())
                if preset is None:
                    messagebox.showwarning("Load Failed",
                                           f"Could not read preset '{sel.get()}'.",
                                           parent=picker)
                    return
                for k, v in preset.items():
                    if k in ocr_vars:
                        ocr_vars[k].set(str(v))
                picker.destroy()

            btnrow = ttk.Frame(picker)
            btnrow.pack(pady=(5, 10))
            ttk.Button(btnrow, text="Load", command=_apply).pack(side="left", padx=5)
            ttk.Button(btnrow, text="Cancel", command=picker.destroy).pack(side="left", padx=5)
            picker.update_idletasks()
            px = win.winfo_x() + (win.winfo_width() - picker.winfo_width()) // 2
            py = win.winfo_y() + (win.winfo_height() - picker.winfo_height()) // 2
            picker.geometry(f"+{px}+{py}")

        ttk.Button(preset_row, text="Save Preset…",
                   command=_do_save_preset).pack(side="left", padx=(0, 5))
        ttk.Button(preset_row, text="Load Preset…",
                   command=_do_load_preset).pack(side="left", padx=5)

        # --- Smart Click Monitor lock ---
        monitor_frame = ttk.LabelFrame(win, text=" Smart Click Monitor ", padding="10")
        monitor_frame.pack(fill="x", padx=10, pady=5)

        ttk.Label(monitor_frame, text="OCR fires only on:").grid(
            row=0, column=0, sticky="w", padx=(0, 5))

        # Build dropdown values: "(any monitor)" + each detected monitor
        monitors = _enumerate_monitors()
        mon_choices = ["(any monitor)"]
        mon_rects = [None]  # parallel: index → rect tuple or None
        for i, (hmon, rect) in enumerate(monitors):
            l, t, r, b = rect
            mon_choices.append(f"Monitor {i+1}: {r-l}×{b-t} at ({l},{t})")
            mon_rects.append(rect)

        # Pick the selection that matches the saved rect
        cur_rect = working.get("ocr_monitor_rect")
        cur_idx = 0
        if cur_rect:
            cur_tuple = tuple(cur_rect)
            for i, mr in enumerate(mon_rects):
                if mr is not None and tuple(mr) == cur_tuple:
                    cur_idx = i
                    break

        mon_var = tk.StringVar(value=mon_choices[cur_idx])
        ttk.Combobox(monitor_frame, textvariable=mon_var,
                     values=mon_choices, state="readonly", width=42).grid(
            row=0, column=1, padx=5, pady=3, sticky="w")

        # --- Debug Mode ---
        debug_frame = ttk.LabelFrame(win, text=" Debug ", padding="10")
        debug_frame.pack(fill="x", padx=10, pady=5)
        debug_var = tk.BooleanVar(value=bool(working.get("ocr_debug_mode")))
        ttk.Checkbutton(debug_frame, text="Show OCR captures (visual tuning)",
                        variable=debug_var).pack(anchor="w")

        # --- Profit Targets ---
        targets_frame = ttk.LabelFrame(win, text=" Profit Targets ", padding="10")
        targets_frame.pack(fill="x", padx=10, pady=5)

        # Scrollable target rows container
        target_rows_frame = ttk.Frame(targets_frame)
        target_rows_frame.pack(fill="x")

        # Header row
        ttk.Label(target_rows_frame, text="#", width=3).grid(row=0, column=0, pady=2)
        ttk.Label(target_rows_frame, text="R-Multiple", width=10).grid(row=0, column=1, pady=2)
        ttk.Label(target_rows_frame, text="Color", width=10).grid(row=0, column=2, pady=2)

        target_widgets = []  # List of (r_var, color_var, color_btn) per target

        def add_target_row(r_val=1.0, color_val="#69db7c"):
            idx = len(target_widgets)
            if idx >= 10:
                self._show_status("Target limit reached (10)")
                return
            row_num = idx + 1  # Row 0 is header

            ttk.Label(target_rows_frame, text=str(idx + 1), width=3).grid(row=row_num, column=0, pady=2)

            r_var = tk.StringVar(value=str(r_val))
            ttk.Entry(target_rows_frame, textvariable=r_var, width=10).grid(row=row_num, column=1, padx=5, pady=2)

            color_var = tk.StringVar(value=color_val)
            color_btn = tk.Button(target_rows_frame, width=8, bg=color_val, activebackground=color_val,
                                  relief="flat", cursor="hand2")

            def pick_color(cv=color_var, cb=color_btn):
                result = colorchooser.askcolor(color=cv.get(), parent=win, title="Pick Target Color")
                if result and result[1]:
                    cv.set(result[1])
                    cb.configure(bg=result[1], activebackground=result[1])

            color_btn.configure(command=pick_color)
            color_btn.grid(row=row_num, column=2, padx=5, pady=2)

            target_widgets.append((r_var, color_var, color_btn))

        def remove_target_row():
            if len(target_widgets) <= 1:
                return
            target_widgets.pop()
            # Destroy widgets in the last row
            row_num = len(target_widgets) + 1
            for widget in target_rows_frame.grid_slaves(row=row_num):
                widget.destroy()

        # Populate existing targets
        for t in working["targets"]:
            add_target_row(t["r_multiple"], t["color"])

        # Add / Remove buttons
        btn_row = ttk.Frame(targets_frame)
        btn_row.pack(fill="x", pady=(5, 0))
        ttk.Button(btn_row, text="+ Add Target", command=add_target_row).pack(side="left", padx=5)
        ttk.Button(btn_row, text="- Remove Last", command=remove_target_row).pack(side="left", padx=5)

        # --- Save / Cancel ---
        bottom_frame = ttk.Frame(win)
        bottom_frame.pack(pady=10)

        def on_save():
            try:
                ocr_offsets = {}
                for key in ("ocr_left", "ocr_right", "ocr_above", "ocr_below"):
                    val = int(float(ocr_vars[key].get()))
                    if val < 0:
                        messagebox.showwarning(
                            "Invalid Capture Region",
                            f"OCR offsets must be >= 0 ({key} = {val}).",
                            parent=win)
                        return
                    ocr_offsets[key] = val

                # Resolve the selected monitor lock back to a rect tuple
                # (or None for "any monitor")
                try:
                    sel_idx = mon_choices.index(mon_var.get())
                except ValueError:
                    sel_idx = 0
                monitor_rect = (list(mon_rects[sel_idx])
                                if mon_rects[sel_idx] is not None else None)

                new_settings = {
                    "platform": self.platform_var.get(),
                    "normal_delay": float(var_normal.get()),
                    "lfa_delay": float(var_lfa.get()),
                    **ocr_offsets,
                    "ocr_monitor_rect": monitor_rect,
                    "ocr_debug_mode": bool(debug_var.get()),
                    "targets": []
                }
                for r_var, color_var, _ in target_widgets:
                    rm = float(r_var.get())
                    if rm <= 0:
                        messagebox.showwarning("Invalid Target", "R-multiples must be positive.", parent=win)
                        return
                    new_settings["targets"].append({"r_multiple": rm, "color": color_var.get()})

                if new_settings["normal_delay"] <= 0 or new_settings["lfa_delay"] <= 0:
                    messagebox.showwarning("Invalid Delay", "Delays must be positive.", parent=win)
                    return

                self.settings = new_settings
                self._apply_settings()
                # If debug was just turned OFF, hide the debug window
                if not new_settings["ocr_debug_mode"] and self._debug_win is not None:
                    try:
                        self._debug_win.withdraw()
                    except tk.TclError:
                        pass
                # Persist to disk immediately so a later crash doesn't lose changes
                self._save_config_now()
                win.destroy()
            except ValueError:
                messagebox.showwarning("Invalid Input", "Please enter valid numbers.", parent=win)

        ttk.Button(bottom_frame, text="Save", command=on_save).pack(side="left", padx=10)
        ttk.Button(bottom_frame, text="Cancel", command=win.destroy).pack(side="left", padx=10)

        # Center the settings window over the main window
        win.update_idletasks()
        x = self.root.winfo_x() + (self.root.winfo_width() - win.winfo_width()) // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - win.winfo_height()) // 2
        win.geometry(f"+{x}+{y}")

    # --- CALCULATION LOGIC ---
    def apply_font_sizing(self):
        default_font = ("Segoe UI", self.font_size)
        bold_font = ("Segoe UI", self.font_size, "bold")
        self.style.configure(".", font=default_font)
        self.style.configure("Treeview.Heading", font=bold_font)
        self.style.configure("Treeview", rowheight=int(self.font_size * 2.5), font=default_font)

    def change_font_size(self, delta):
        new_size = self.font_size + delta
        if 8 <= new_size <= 24:
            self.font_size = new_size
            self.apply_font_sizing()

    def _show_status(self, msg, duration_ms=3000):
        """Display a transient message in the status bar."""
        if self._status_after_id:
            self.root.after_cancel(self._status_after_id)
        self.status_label.config(text=msg)
        self._status_after_id = self.root.after(duration_ms, lambda: self.status_label.config(text=""))

    def calculate(self, event=None, auto_infer_direction=False):
        try:
            v_entry = self.vars["Entry"].get()
            v_stop = self.vars["Stop"].get()
            v_risk = self.vars["Risk $"].get()
            v_shares = self.vars["Shares"].get()
            v_cost = self.vars["Cost"].get()

            def to_num(val):
                if not val or val == "...":
                    return None
                try:
                    return float(val)
                except ValueError:
                    return None

            entry, stop, risk, shares, cost = (
                to_num(v_entry), to_num(v_stop), to_num(v_risk),
                to_num(v_shares), to_num(v_cost))

            # Check for non-numeric input in non-empty fields
            for label, raw in [("Entry", v_entry), ("Stop", v_stop),
                               ("Risk $", v_risk), ("Shares", v_shares)]:
                if raw and raw != "..." and to_num(raw) is None:
                    self._show_status(f"Invalid input in {label}")
                    return

            # Sync any in-flight offset/slippage values into per-mode slots
            # so what the user typed survives a close even if they didn't
            # change modes after.
            self._stash_current_stop_offset()
            self._stash_current_slip_value("entry")
            self._stash_current_slip_value("exit")

            # Direction is whatever the user has selected — no auto-flip.
            # Inputs that contradict the current direction are silently
            # rejected (see direction-consistency gate below).
            mode = self.direction_var.get()
            stop_mode = self.stop_mode_var.get()

            # --- Offset mode: derive Stop from Entry + offset BEFORE math ---
            # In offset mode the Stop entry is read-only and the offset is the
            # source of truth, so we recompute Stop on every calculate.
            if stop_mode != "manual" and entry is not None:
                derived = self._compute_stop_from_offset(entry)
                if derived is not None:
                    stop = derived
                    self.vars["Stop"].set(f"{stop:.2f}")

            # Early exit: Entry and Stop cannot be equal
            if entry and stop and entry == stop:
                self._show_status("Entry and Stop cannot be equal")
                return

            # Explicit Risk=$0 with other fields present: the downstream
            # math correctly produces Shares=0 (zero budget → zero size),
            # but that's silent and confusing. Surface the cause so the
            # user knows why the table didn't populate.
            if (risk is not None and risk == 0
                    and (entry is not None or stop is not None
                         or shares is not None)):
                self._show_status("Risk $ must be > 0")
                return

            # Direction-consistency gate: silently bail if Entry+Stop
            # contradict the current Long/Short selection. User explicitly
            # chose direction; we don't auto-flip and we don't accept the
            # bad input. Gated to manual stop mode — in offset mode, Stop
            # is derived from Entry ± offset and is always consistent.
            if (self.stop_mode_var.get() == "manual"
                    and entry is not None and stop is not None):
                _dir = self.direction_var.get()
                if _dir == "Long" and stop >= entry:
                    return
                if _dir == "Short" and stop <= entry:
                    return

            def eff_rps():
                """Effective risk-per-share (ideal + entry-slip + exit-slip).
                Returns None if entry/stop are not both numeric/positive."""
                if entry is None or stop is None:
                    return None
                r = self._effective_risk_per_share(entry, stop)
                return r if r > 0 else None

            def shares_from_risk_rps(risk_val, rps_val):
                """``int(risk / rps)`` with a UX guard.

                When the result floors to 0 — either because slippage
                consumed the full risk budget per share, or because the
                stop is too tight relative to Risk $ — surface the
                cause in the status bar so the user isn't staring at a
                silent ``Shares = 0``.
                """
                if rps_val is None or rps_val <= 0:
                    return 0
                raw = risk_val / rps_val
                if raw < 1:
                    self._show_status(
                        "Risk budget can't cover one share — "
                        "raise Risk $, tighten Stop, or reduce slippage"
                    )
                    return 0
                return int(raw)

            def solve_stop_from_risk_shares(target_rps):
                """Given target eff-rps (Risk / Shares), back-solve Stop using
                slippage at *entry* as a first-order approximation for slip_x
                (exact pct-of-stop solution requires iteration; the difference
                is sub-cent for typical 1–5% stops)."""
                slip_e = self._slippage_per_share("entry", entry)
                slip_x_approx = self._slippage_per_share("exit", entry)
                ideal_rps = target_rps - slip_e - slip_x_approx
                if ideal_rps <= 0:
                    return None
                return entry - ideal_rps if mode == "Long" else entry + ideal_rps

            # --- RE-SOLVE: all key values present, detect what changed ---
            if entry and stop and risk and shares and self._last_stop is not None:
                cost_changed = (cost is not None and self._last_cost is not None
                                and cost != self._last_cost)
                shares_changed = (shares != self._last_shares)
                stop_changed = (stop != self._last_stop)
                rps = eff_rps()

                if stop_mode != "manual":
                    # OFFSET MODE: Stop is the anchor (read-only). Cost / Shares /
                    # Risk re-derive each other through eff_rps.
                    if rps is not None:
                        if cost_changed and not shares_changed:
                            if entry > 0:
                                new_shares = int(cost / entry)
                                if new_shares > 0:
                                    actual_cost = entry * new_shares
                                    if abs(actual_cost - cost) > 0.005:
                                        self._show_status(
                                            f"Cost adjusted to {actual_cost:.2f} "
                                            f"({new_shares} sh × {entry:.2f})")
                                    shares = new_shares
                                    self.vars["Shares"].set(str(new_shares))
                                    self.vars["Risk $"].set(f"{rps * new_shares:.2f}")
                        elif shares_changed:
                            self.vars["Risk $"].set(f"{rps * shares:.2f}")
                        else:
                            # Risk edited (or anything else) → derive Shares
                            new_shares = int(risk / rps)
                            shares = new_shares
                            self.vars["Shares"].set(str(new_shares))
                else:
                    # MANUAL MODE: existing semantics, with slippage folded in
                    if cost_changed and not shares_changed and not stop_changed:
                        # Cost edited → derive Shares, then Stop using eff_rps target
                        if entry != 0:
                            new_shares = int(cost / entry)
                            if new_shares > 0:
                                actual_cost = entry * new_shares
                                if abs(actual_cost - cost) > 0.005:
                                    self._show_status(
                                        f"Cost adjusted to {actual_cost:.2f} "
                                        f"({new_shares} sh × {entry:.2f})")
                                shares = new_shares
                                self.vars["Shares"].set(str(new_shares))
                                target_rps = risk / new_shares
                                new_stop = solve_stop_from_risk_shares(target_rps)
                                if new_stop is not None:
                                    stop = new_stop
                                    self.vars["Stop"].set(f"{stop:.2f}")
                    elif shares_changed and not stop_changed:
                        # Shares edited → derive Stop
                        if shares > 0:
                            target_rps = risk / shares
                            new_stop = solve_stop_from_risk_shares(target_rps)
                            if new_stop is not None:
                                stop = new_stop
                                self.vars["Stop"].set(f"{stop:.2f}")
                    else:
                        # Stop edited (or Risk edited, or ambiguous) → derive Shares
                        if rps is not None:
                            shares = shares_from_risk_rps(risk, rps)
                            self.vars["Shares"].set(str(int(shares)))
            else:
                # --- INITIAL FILL: one or more fields still missing ---

                # Scenario: Cost + Entry → Shares
                if entry and cost and not shares and entry != 0:
                    shares = int(cost / entry)
                    self.vars["Shares"].set(str(shares))

                rps = eff_rps()

                # Scenario A: Entry + Stop + Risk → Shares
                if entry and stop and risk and not shares:
                    if rps is not None:
                        shares = shares_from_risk_rps(risk, rps)
                        self.vars["Shares"].set(str(shares))

                # Scenario B: Entry + Stop + Shares → Risk
                elif entry and stop and shares and not risk:
                    if rps is not None:
                        risk = rps * shares
                        self.vars["Risk $"].set(f"{risk:.2f}")

                # Scenario C: Entry + Shares + Risk → Stop (manual mode only;
                # in offset mode Stop is derived from offset above).
                elif stop_mode == "manual" and entry and shares and risk and not stop:
                    if shares > 0:
                        target_rps = risk / shares
                        new_stop = solve_stop_from_risk_shares(target_rps)
                        if new_stop is not None:
                            stop = new_stop
                            self.vars["Stop"].set(f"{stop:.2f}")

            # Final updates for Cost and Table
            cur_entry = self.vars["Entry"].get()
            cur_stop = self.vars["Stop"].get()
            cur_shares = self.vars["Shares"].get()

            if cur_entry and cur_shares:
                try:
                    f_entry = float(cur_entry)
                    f_shares = float(cur_shares)
                    self.vars["Cost"].set(f"{f_entry * f_shares:.2f}")
                except ValueError:
                    pass

            if cur_entry and cur_stop and cur_shares:
                try:
                    f_entry = float(cur_entry)
                    f_stop = float(cur_stop)
                    f_shares = float(cur_shares)
                    self.update_table(f_entry, f_stop, f_shares)
                except ValueError:
                    pass

            # Store last values for change detection on next calculate. Each
            # field is captured independently so a single bad value doesn't
            # poison the others.
            for attr, key in (("_last_stop", "Stop"),
                              ("_last_shares", "Shares"),
                              ("_last_cost", "Cost")):
                raw = self.vars[key].get()
                try:
                    setattr(self, attr, float(raw) if raw else None)
                except ValueError:
                    setattr(self, attr, None)

        except ValueError:
            self._show_status("Invalid input -- enter numeric values")
        except ZeroDivisionError:
            self._show_status("Entry and Stop cannot be equal")
        except TypeError:
            self._show_status("Unexpected error -- check inputs")

    def update_table(self, entry, stop, shares):
        for i in self.tree.get_children():
            self.tree.delete(i)
        risk_per_share = abs(entry - stop)
        direction = 1 if self.direction_var.get() == "Long" else -1
        long_dir = (direction == 1)

        slip_on = self.slip_entry_enabled.get() or self.slip_exit_enabled.get()
        # Effective entry fill price (worse than displayed when slippage on)
        slip_e = self._slippage_per_share("entry", entry)
        eff_entry = entry + slip_e if long_dir else entry - slip_e

        # Toggle the (net) suffix on the PnL column header so the user knows
        # the numbers are slippage-adjusted.
        try:
            self.tree.heading("PnL", text="P/L ($) (net)" if slip_on else "P/L ($)")
        except tk.TclError:
            pass

        # Build levels: STOP + ENTRY + dynamic targets from settings
        levels = [{"r": -1, "label": "STOP", "tag": "stop"},
                  {"r": 0,  "label": "ENTRY", "tag": "entry"}]
        for i, t in enumerate(self.settings["targets"]):
            rm = t["r_multiple"]
            label = f"{rm}R" if rm != int(rm) else f"{int(rm)}R"
            levels.append({"r": rm, "label": f"Target {label}", "tag": f"target_{i}"})

        for lv in levels:
            # Ideal price level (what's displayed in the Price column)
            price = entry + (risk_per_share * lv["r"] * direction)

            # Net PnL: actual fill on exit minus actual fill on entry, × shares.
            # Long:  buy at eff_entry, sell at (price - slip_x)
            # Short: sell at eff_entry, buy at (price + slip_x). Sign flips.
            slip_x = self._slippage_per_share("exit", price)
            if long_dir:
                pnl_per_share = (price - slip_x) - eff_entry
            else:
                pnl_per_share = eff_entry - (price + slip_x)
            pnl = pnl_per_share * shares

            rounded = round(pnl)
            if rounded == 0:
                pnl_str = "$0"
            elif rounded > 0:
                pnl_str = f"+${rounded:.0f}"
            else:
                pnl_str = f"-${abs(rounded):.0f}"
            self.tree.insert("", "end",
                             values=(lv["label"], f"{price:.2f}", pnl_str),
                             tags=(lv["tag"],))

        # Table rendered successfully → fields are fresh-good, drop yellow tint
        self._clear_stale()

    def clear_inputs(self):
        """Clears trade-specific inputs. Risk $ is intentionally preserved (persistent preference)."""
        self.vars["Entry"].set("")
        self.vars["Stop"].set("")
        self.vars["Shares"].set("")
        self.vars["Cost"].set("")
        self.entry_turn = True
        self._last_stop = None
        self._last_shares = None
        self._last_cost = None
        self._pre_click_values.clear()
        self._clear_stale()
        for i in self.tree.get_children():
            self.tree.delete(i)

    def load_config(self):
        self.font_size = 10
        geometry = "360x550"
        saved_risk = ""
        self._saved_smart_click = False
        self._saved_lfa = True
        self._saved_direction = "Long"
        self._saved_freeze_entry = False
        self._saved_freeze_stop = False
        # Stop-offset defaults (also serve as fallback when keys missing in old configs)
        self._saved_stop_mode = "manual"
        self._saved_stop_offset_pct = ""
        self._saved_stop_offset_dollar = ""
        # Slippage defaults
        self._saved_slip_entry_enabled = False
        self._saved_slip_exit_enabled = False
        self._saved_slip_entry_mode = "pct"
        self._saved_slip_exit_mode = "pct"
        self._saved_slip_entry_pct = ""
        self._saved_slip_entry_dollar = ""
        self._saved_slip_exit_pct = ""
        self._saved_slip_exit_dollar = ""
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r") as f:
                    data = json.load(f)
                    geometry = data.get("geometry", "360x550")
                    self.font_size = data.get("font_size", 10)
                    saved_risk = data.get("risk_value", "")
                    self._saved_smart_click = data.get("smart_click", False)
                    self._saved_lfa = data.get("lfa_enabled", True)
                    self._saved_direction = data.get("direction", "Long")
                    self._saved_freeze_entry = data.get("freeze_entry", False)
                    self._saved_freeze_stop = data.get("freeze_stop", False)
                    self._saved_stop_mode = data.get("stop_mode", "manual")
                    self._saved_stop_offset_pct = str(data.get("stop_offset_pct", ""))
                    self._saved_stop_offset_dollar = str(data.get("stop_offset_dollar", ""))
                    self._saved_slip_entry_enabled = data.get("slip_entry_enabled", False)
                    self._saved_slip_exit_enabled = data.get("slip_exit_enabled", False)
                    self._saved_slip_entry_mode = data.get("slip_entry_mode", "pct")
                    self._saved_slip_exit_mode = data.get("slip_exit_mode", "pct")
                    self._saved_slip_entry_pct = str(data.get("slip_entry_pct", ""))
                    self._saved_slip_entry_dollar = str(data.get("slip_entry_dollar", ""))
                    self._saved_slip_exit_pct = str(data.get("slip_exit_pct", ""))
                    self._saved_slip_exit_dollar = str(data.get("slip_exit_dollar", ""))
                    # Merge saved settings over defaults (deep, forward-compatible)
                    self.settings = _deep_merge_defaults(
                        self._DEFAULT_SETTINGS, data.get("settings", {}))
            except (json.JSONDecodeError, KeyError, ValueError, OSError) as e:
                # Preserve the corrupt file for inspection rather than deleting it
                print(f"Warning: Config corrupt — preserving as .bad: {e}")
                bad_path = CONFIG_FILE + ".bad"
                try:
                    if os.path.exists(bad_path):
                        os.remove(bad_path)
                    os.replace(CONFIG_FILE, bad_path)
                except OSError as e2:
                    print(f"Warning: Could not move corrupt config: {e2}")
        self.root.geometry(geometry)
        self.vars["Risk $"].set(saved_risk)

    def _seed_default_monitor_lock(self):
        """First-launch: lock Smart Click to the monitor the calc is on so
        OCR works out of the box. User can change it in Settings later."""
        try:
            cx = self.root.winfo_rootx() + self.root.winfo_width() // 2
            cy = self.root.winfo_rooty() + self.root.winfo_height() // 2
        except tk.TclError:
            return
        rect = _monitor_rect_at_point(cx, cy)
        if rect is not None:
            self.settings["ocr_monitor_rect"] = list(rect)

    def _build_config_dict(self):
        """Snapshot all persistent state into a dict (main thread only)."""
        # Sync any in-flight offset/slippage value into its mode-specific slot
        # so the most recently typed value is always what gets persisted.
        self._stash_current_stop_offset()
        self._stash_current_slip_value("entry")
        self._stash_current_slip_value("exit")
        return {
            "geometry": self.root.geometry(),
            "font_size": self.font_size,
            "risk_value": self.vars["Risk $"].get(),
            "smart_click": self.smart_click_enabled.get(),
            "lfa_enabled": self.lfa_enabled.get(),
            "direction": self.direction_var.get(),
            "freeze_entry": self.freeze_entry.get(),
            "freeze_stop": self.freeze_stop.get(),
            "stop_mode": self.stop_mode_var.get(),
            "stop_offset_pct": self._stop_offset_pct,
            "stop_offset_dollar": self._stop_offset_dollar,
            "slip_entry_enabled": self.slip_entry_enabled.get(),
            "slip_exit_enabled": self.slip_exit_enabled.get(),
            "slip_entry_mode": self.slip_entry_mode.get(),
            "slip_exit_mode": self.slip_exit_mode.get(),
            "slip_entry_pct": self._slip_entry_pct,
            "slip_entry_dollar": self._slip_entry_dollar,
            "slip_exit_pct": self._slip_exit_pct,
            "slip_exit_dollar": self._slip_exit_dollar,
            "settings": self.settings,
        }

    def _save_config_now(self):
        """Atomic write: write to .tmp, fsync, swap into place. On success
        keep the previous file as .bak so a future corruption is recoverable."""
        try:
            data = self._build_config_dict()
        except tk.TclError:
            return  # Tk torn down already
        tmp_path = CONFIG_FILE + ".tmp"
        bak_path = CONFIG_FILE + ".bak"
        try:
            with open(tmp_path, "w") as f:
                json.dump(data, f)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            # Save previous good file as .bak before replace
            if os.path.exists(CONFIG_FILE):
                try:
                    if os.path.exists(bak_path):
                        os.remove(bak_path)
                    os.replace(CONFIG_FILE, bak_path)
                except OSError:
                    pass  # non-fatal
            os.replace(tmp_path, CONFIG_FILE)
        except (OSError, TypeError) as e:
            print(f"Warning: Could not save config: {e}")
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass

    def on_close(self):
        self._closing = True
        self._stop_clipboard_poll()
        self._stop_listener()
        self._stop_foreground_poll()
        if self._ocr_thread and self._ocr_thread.is_alive():
            self._ocr_thread.join(timeout=1.0)
        self._save_config_now()
        try:
            self.root.destroy()
        except tk.TclError:
            pass

if __name__ == "__main__":
    # DPI awareness so pynput coords and ImageGrab coords agree on scaled displays
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        pass

    root = tk.Tk()
    app = TradeSolverApp(root)
    root.mainloop()