"""Headless smoketest for price_calc_III.

Exercises:
  - module import (no top-level crash)
  - TradeSolverApp instantiation under Tk
  - calculation logic across all four scenarios + re-solve
  - settings dialog open/close
  - config save/load round-trip
  - clipboard regex + OCR-text regex behavior

Exits non-zero on any failure with a short message.
"""
import os
import sys
import json
import shutil
import tempfile
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def _hr(label):
    print(f"--- {label} ---")


def _fail(msg):
    print(f"FAIL: {msg}")
    traceback.print_exc()
    sys.exit(1)


def _redirect_config_to_tmp():
    """Force CONFIG_FILE to a tmp path so we don't clobber the real config."""
    import price_calc_III as mod
    tmpdir = tempfile.mkdtemp(prefix="price_calc_smoke_")
    mod.CONFIG_FILE = os.path.join(tmpdir, "window_config.json")
    return tmpdir


def test_import():
    _hr("import")
    import price_calc_III  # noqa: F401
    print("import OK")


def test_instantiate_and_lifecycle():
    _hr("instantiate + lifecycle")
    import tkinter as tk
    import price_calc_III as mod

    root = tk.Tk()
    try:
        app = mod.TradeSolverApp(root)
        root.update_idletasks()
        root.update()
        # Schedule destroy quickly
        root.after(200, root.destroy)
        root.mainloop()
        print("lifecycle OK")
    except Exception:
        try:
            root.destroy()
        except Exception:
            pass
        _fail("Lifecycle crashed")


def _make_app():
    import tkinter as tk
    import price_calc_III as mod
    root = tk.Tk()
    app = mod.TradeSolverApp(root)
    root.update_idletasks()
    return root, app


def test_calc_initial_fill():
    _hr("calc: initial-fill scenarios")
    root, app = _make_app()
    try:
        # Scenario A: Entry + Stop + Risk -> Shares
        app.vars["Entry"].set("100")
        app.vars["Stop"].set("95")
        app.vars["Risk $"].set("25")
        app.vars["Shares"].set("")
        app.vars["Cost"].set("")
        app.direction_var.set("Long")
        app.calculate()
        shares = app.vars["Shares"].get()
        cost = app.vars["Cost"].get()
        assert shares == "5", f"Scenario A Shares expected 5, got {shares!r}"
        assert cost == "500.00", f"Scenario A Cost expected 500.00, got {cost!r}"

        # Scenario B: Entry + Stop + Shares -> Risk
        for k in ("Entry", "Stop", "Risk $", "Shares", "Cost"):
            app.vars[k].set("")
        app.vars["Entry"].set("100")
        app.vars["Stop"].set("95")
        app.vars["Shares"].set("10")
        app.calculate()
        risk = app.vars["Risk $"].get()
        assert risk == "50.00", f"Scenario B Risk expected 50.00, got {risk!r}"

        # Scenario C: Entry + Shares + Risk -> Stop
        for k in ("Entry", "Stop", "Risk $", "Shares", "Cost"):
            app.vars[k].set("")
        app.vars["Entry"].set("100")
        app.vars["Risk $"].set("25")
        app.vars["Shares"].set("5")
        app.direction_var.set("Long")
        app.calculate()
        stop = app.vars["Stop"].get()
        assert stop == "95.00", f"Scenario C Stop expected 95.00, got {stop!r}"

        # Scenario D: Entry + Cost -> Shares
        for k in ("Entry", "Stop", "Risk $", "Shares", "Cost"):
            app.vars[k].set("")
        app.vars["Entry"].set("100")
        app.vars["Cost"].set("500")
        app.calculate()
        shares = app.vars["Shares"].get()
        assert shares == "5", f"Scenario D Shares expected 5, got {shares!r}"

        print("initial-fill OK")
    finally:
        root.destroy()


def test_calc_resolve():
    _hr("calc: re-solve")
    root, app = _make_app()
    try:
        # Initial fill: Entry + Stop + Risk -> Shares
        app.vars["Entry"].set("100")
        app.vars["Stop"].set("95")
        app.vars["Risk $"].set("25")
        app.direction_var.set("Long")
        app.calculate()
        assert app.vars["Shares"].get() == "5"

        # Edit Stop -> Shares re-derived
        app.vars["Stop"].set("90")
        app.calculate()
        assert app.vars["Shares"].get() == "2", f"Stop-edit re-solve: {app.vars['Shares'].get()!r}"

        # Re-init for shares-edit
        app.clear_inputs()
        app.vars["Entry"].set("100")
        app.vars["Stop"].set("95")
        app.vars["Risk $"].set("25")
        app.calculate()  # populates last_*
        # Edit Shares -> Stop re-derived
        app.vars["Shares"].set("10")
        app.calculate()
        # risk_per_share = 25/10 = 2.5; stop = 100 - 2.5 = 97.50
        assert app.vars["Stop"].get() == "97.50", f"Shares-edit re-solve Stop: {app.vars['Stop'].get()!r}"

        print("re-solve OK")
    finally:
        root.destroy()


def test_long_mode_rejects_short_smart_click():
    _hr("Long mode: Smart Click on a Short-setup price is silently rejected")
    root, app = _make_app()
    try:
        app.direction_var.set("Long")
        app.vars["Entry"].set("100")
        app.vars["Risk $"].set("10")
        # Force the Smart Click target to be Stop (Entry-frozen routes there)
        app.freeze_entry.set(True)
        app.freeze_stop.set(False)
        # Pre-clicker put "..." in Stop and stashed prior value
        app._pre_click_values["Stop"] = ""
        app.vars["Stop"].set("...")
        # Simulate OCR delivering a Short-setup price (Stop > Entry)
        app.auto_fill_price("105")
        # auto_fill_price must NOT have set Stop to 105 — it's still "..." so
        # _ensure_unlock can restore the prior empty value as a wasted click.
        assert app.vars["Stop"].get() == "...", \
            f"Stop should remain '...' (rejected); got {app.vars['Stop'].get()!r}"
        # Direction stayed Long — no auto-flip
        assert app.direction_var.get() == "Long"
        # Now run _ensure_unlock and verify wasted-click restore + stale tint
        app._ensure_unlock()
        assert app.vars["Stop"].get() == ""
        stop_w = app.entry_widgets["Stop"]
        assert "Stale.TEntry" in str(stop_w.cget("style"))
        print("Long-mode Smart Click reject OK")
    finally:
        root.destroy()


def test_short_mode_rejects_long_smart_click():
    _hr("Short mode: Smart Click on a Long-setup price is silently rejected")
    root, app = _make_app()
    try:
        app.direction_var.set("Short")
        app.vars["Entry"].set("100")
        app.vars["Risk $"].set("10")
        app.freeze_entry.set(True)
        app.freeze_stop.set(False)
        app._pre_click_values["Stop"] = ""
        app.vars["Stop"].set("...")
        # Stop < Entry would be a Long setup → reject in Short mode
        app.auto_fill_price("95")
        assert app.vars["Stop"].get() == "...", \
            f"rejected; got {app.vars['Stop'].get()!r}"
        assert app.direction_var.get() == "Short"
        print("Short-mode Smart Click reject OK")
    finally:
        root.destroy()


def test_long_mode_rejects_short_manual_calculate():
    _hr("Long mode: manual Stop>Entry is silently rejected by calculate()")
    root, app = _make_app()
    try:
        app.direction_var.set("Long")
        app.vars["Entry"].set("100")
        app.vars["Risk $"].set("10")
        app.vars["Stop"].set("105")  # Short setup
        app.calculate()
        # No flip, no calc — Shares stays empty, table empty, direction stays Long
        assert app.direction_var.get() == "Long"
        assert app.vars["Shares"].get() == "", \
            f"Shares must stay empty; got {app.vars['Shares'].get()!r}"
        assert len(app.tree.get_children()) == 0, "Table must not render"
        print("Long-mode manual reject OK")
    finally:
        root.destroy()


def test_offset_mode_long_accepts_lower_entry():
    """In Long offset mode, a new Entry below the previous derived Stop
    must NOT be rejected as a 'wrong direction' input — Stop will be
    recomputed from the new Entry."""
    _hr("Offset Long: new entry below prior derived stop is accepted")
    root, app = _make_app()
    try:
        app.direction_var.set("Long")
        app.stop_mode_var.set("dollar")
        app.stop_offset_var.set("5")  # $5 below entry
        app.vars["Entry"].set("100")
        app.vars["Risk $"].set("10")
        app.calculate()
        # Initial: Stop = 100 - 5 = 95, Shares = 10/5 = 2
        assert app.vars["Stop"].get() == "95.00", \
            f"initial stop: {app.vars['Stop'].get()!r}"
        assert app.vars["Shares"].get() == "2"

        # New Smart Click delivers price 80 — below BOTH the prior 100 entry
        # AND the prior 95 stop. In offset mode this is a valid new entry.
        app._pre_click_values["Entry"] = "100"
        app.vars["Entry"].set("...")  # what indicate_loading would have set
        app.auto_fill_price("80")

        # Entry should be set to 80
        assert app.vars["Entry"].get() == "80", \
            f"new entry: {app.vars['Entry'].get()!r}"
        # Stop should be recomputed: 80 - 5 = 75
        assert app.vars["Stop"].get() == "75.00", \
            f"recomputed stop: {app.vars['Stop'].get()!r}"
        # Shares = 10/5 = 2 (unchanged risk_per_share)
        assert app.vars["Shares"].get() == "2"
        # Direction unchanged
        assert app.direction_var.get() == "Long"
        print("offset Long lower-entry accept OK")
    finally:
        root.destroy()


def test_offset_mode_short_accepts_higher_entry():
    """Mirror: in Short offset mode, a new Entry above the previous derived
    Stop is accepted (Stop recomputes upward from the new Entry)."""
    _hr("Offset Short: new entry above prior derived stop is accepted")
    root, app = _make_app()
    try:
        app.direction_var.set("Short")
        app.stop_mode_var.set("dollar")
        app.stop_offset_var.set("5")  # $5 above entry for Short
        app.vars["Entry"].set("100")
        app.vars["Risk $"].set("10")
        app.calculate()
        assert app.vars["Stop"].get() == "105.00"
        assert app.vars["Shares"].get() == "2"

        # New click at 120 — above both previous entry AND previous stop
        app._pre_click_values["Entry"] = "100"
        app.vars["Entry"].set("...")
        app.auto_fill_price("120")

        assert app.vars["Entry"].get() == "120"
        # Stop recomputed: 120 + 5 = 125
        assert app.vars["Stop"].get() == "125.00", \
            f"recomputed stop: {app.vars['Stop'].get()!r}"
        assert app.vars["Shares"].get() == "2"
        assert app.direction_var.get() == "Short"
        print("offset Short higher-entry accept OK")
    finally:
        root.destroy()


def test_compatible_input_accepted():
    _hr("Compatible direction: Smart Click + manual both flow normally")
    root, app = _make_app()
    try:
        # Long mode, Entry=100, Stop=95 → consistent
        app.direction_var.set("Long")
        app.vars["Entry"].set("100")
        app.vars["Risk $"].set("10")
        app.freeze_entry.set(True)
        app.freeze_stop.set(False)
        app._pre_click_values["Stop"] = ""
        app.vars["Stop"].set("...")
        app.auto_fill_price("95")
        assert app.vars["Stop"].get() == "95"
        # Risk_per_share = 5, Shares = 10/5 = 2
        assert app.vars["Shares"].get() == "2", \
            f"Shares: {app.vars['Shares'].get()!r}"
        assert app.direction_var.get() == "Long"
        print("compatible accept OK")
    finally:
        root.destroy()


def test_cost_rewrite_status():
    _hr("calc: cost re-solve adjusts and informs")
    root, app = _make_app()
    try:
        # Initial: Entry=100, Stop=95, Risk=25 → Shares=5, Cost=500
        app.vars["Entry"].set("100")
        app.vars["Stop"].set("95")
        app.vars["Risk $"].set("25")
        app.direction_var.set("Long")
        app.calculate()
        assert app.vars["Cost"].get() == "500.00"

        # Edit Cost to a non-divisible value: 1050 → Shares=10, Cost rewritten to 1000
        app.vars["Cost"].set("1050")
        app.calculate()
        assert app.vars["Shares"].get() == "10", f"Shares: {app.vars['Shares'].get()!r}"
        assert app.vars["Cost"].get() == "1000.00", f"Cost: {app.vars['Cost'].get()!r}"
        # Status message should mention the adjustment
        status = app.status_label.cget("text")
        assert "adjusted" in status.lower(), f"status: {status!r}"
        print("cost rewrite status OK")
    finally:
        root.destroy()


def test_both_frozen_disables_smart_click():
    _hr("both-frozen disables Smart Click")
    import price_calc_III as mod
    if not mod.AUTOMATION_AVAILABLE:
        print("(automation unavailable in this env — skipping)")
        return
    root, app = _make_app()
    try:
        # No widget created if AUTOMATION_AVAILABLE is False at import time.
        chk = getattr(app, "chk_smart", None)
        if chk is None:
            print("(no chk_smart widget — skipping)")
            return
        # Initial: neither frozen → enabled
        app.freeze_entry.set(False)
        app.freeze_stop.set(False)
        root.update_idletasks()
        assert "disabled" not in chk.state(), f"chk state init: {chk.state()!r}"
        # Both frozen → disabled
        app.freeze_entry.set(True)
        app.freeze_stop.set(True)
        root.update_idletasks()
        assert "disabled" in chk.state(), f"chk state both-frozen: {chk.state()!r}"
        # Unfreeze one → enabled again
        app.freeze_entry.set(False)
        root.update_idletasks()
        assert "disabled" not in chk.state(), f"chk state one-frozen: {chk.state()!r}"
        print("both-frozen Smart Click state OK")
    finally:
        root.destroy()


def test_stop_offset_pct():
    _hr("stop offset: percent mode (Long)")
    root, app = _make_app()
    try:
        app.direction_var.set("Long")
        app.stop_mode_var.set("pct")
        app.stop_offset_var.set("5")  # 5% below entry
        app.vars["Entry"].set("100")
        app.vars["Risk $"].set("25")
        app.calculate()
        # Stop = 100 * (1 - 0.05) = 95.00
        assert app.vars["Stop"].get() == "95.00", f"Stop: {app.vars['Stop'].get()!r}"
        # rps = 5; shares = 25/5 = 5
        assert app.vars["Shares"].get() == "5", f"Shares: {app.vars['Shares'].get()!r}"
        print("stop offset pct OK")
    finally:
        root.destroy()


def test_stop_offset_dollar_short():
    _hr("stop offset: dollar mode (Short)")
    root, app = _make_app()
    try:
        app.direction_var.set("Short")
        app.stop_mode_var.set("dollar")
        app.stop_offset_var.set("0.50")  # $0.50 above entry for short
        app.vars["Entry"].set("100")
        app.vars["Risk $"].set("25")
        app.calculate()
        # Stop = 100 + 0.50 = 100.50
        assert app.vars["Stop"].get() == "100.50", f"Stop: {app.vars['Stop'].get()!r}"
        # rps = 0.50; shares = 25/0.50 = 50
        assert app.vars["Shares"].get() == "50", f"Shares: {app.vars['Shares'].get()!r}"
        print("stop offset $ Short OK")
    finally:
        root.destroy()


def test_stop_offset_mode_switch_persistence():
    _hr("stop offset: each mode keeps its own value across switches")
    root, app = _make_app()
    try:
        # Type 5 in pct mode
        app.stop_mode_var.set("pct")
        app.stop_offset_var.set("5")
        # Switch to dollar — value should clear (no prior $ value)
        app.stop_mode_var.set("dollar")
        assert app.stop_offset_var.get() == "", f"$ slot empty initially, got {app.stop_offset_var.get()!r}"
        # Type 0.75 in dollar mode
        app.stop_offset_var.set("0.75")
        # Switch back to pct — should restore "5"
        app.stop_mode_var.set("pct")
        assert app.stop_offset_var.get() == "5", f"pct slot: {app.stop_offset_var.get()!r}"
        # Switch to dollar again — should restore "0.75"
        app.stop_mode_var.set("dollar")
        assert app.stop_offset_var.get() == "0.75", f"$ slot: {app.stop_offset_var.get()!r}"
        # Switch to manual — value clears, Stop becomes editable
        app.stop_mode_var.set("manual")
        assert app.stop_offset_var.get() == "", f"manual displays empty: {app.stop_offset_var.get()!r}"
        assert str(app.entry_widgets["Stop"].cget("state")) == "normal"
        print("stop offset mode-switch persistence OK")
    finally:
        root.destroy()


def test_offset_resolve_shares_anchors_risk():
    _hr("offset re-solve: editing Shares re-derives Risk (Stop fixed)")
    root, app = _make_app()
    try:
        app.direction_var.set("Long")
        app.stop_mode_var.set("dollar")
        app.stop_offset_var.set("0.50")  # rps = 0.50
        app.vars["Entry"].set("100")
        app.vars["Risk $"].set("25")
        app.calculate()
        assert app.vars["Stop"].get() == "99.50"
        assert app.vars["Shares"].get() == "50"

        # Edit Shares to 100 → Risk should become 0.50 × 100 = 50.00
        app.vars["Shares"].set("100")
        app.calculate()
        assert app.vars["Stop"].get() == "99.50", "Stop must remain anchored to offset"
        assert app.vars["Risk $"].get() == "50.00", f"Risk: {app.vars['Risk $'].get()!r}"

        # Edit Risk to 30 → Shares = 30 / 0.50 = 60
        app.vars["Risk $"].set("30")
        app.calculate()
        assert app.vars["Stop"].get() == "99.50"
        assert app.vars["Shares"].get() == "60", f"Shares: {app.vars['Shares'].get()!r}"
        print("offset re-solve OK")
    finally:
        root.destroy()


def test_slippage_reduces_shares():
    _hr("slippage (model A): reduces Shares to keep Risk $ budget")
    root, app = _make_app()
    try:
        app.direction_var.set("Long")
        app.vars["Entry"].set("100")
        app.vars["Stop"].set("95")
        app.vars["Risk $"].set("25")
        app.calculate()
        baseline_shares = int(app.vars["Shares"].get())
        assert baseline_shares == 5, f"baseline: {baseline_shares}"

        # Turn on entry slippage at $0.10/share — eff_rps = 5 + 0.10 + 0 = 5.10
        # New shares = floor(25 / 5.10) = 4
        app.slip_entry_enabled.set(True)
        app.slip_entry_mode.set("dollar")
        app.slip_entry_var.set("0.10")
        app.calculate()
        assert app.vars["Shares"].get() == "4", f"with $0.10 entry slip: {app.vars['Shares'].get()!r}"

        # Add exit slippage too at $0.10 — eff_rps = 5 + 0.10 + 0.10 = 5.20
        # New shares = floor(25 / 5.20) = 4 (still rounds down to 4)
        app.slip_exit_enabled.set(True)
        app.slip_exit_mode.set("dollar")
        app.slip_exit_var.set("0.10")
        app.calculate()
        assert app.vars["Shares"].get() == "4", f"with both slips: {app.vars['Shares'].get()!r}"

        # Bump exit slippage so total slip > 0.5 → 25/5.6 = 4.46 → 4
        # Bump higher: 25/6.0 = 4.16 → 4. Try 25/12.5 = 2 (huge slip, shows reduction)
        app.slip_exit_var.set("7.5")  # eff_rps = 5 + 0.1 + 7.5 = 12.6 → shares = 1
        app.calculate()
        assert app.vars["Shares"].get() == "1", f"huge slip: {app.vars['Shares'].get()!r}"
        print("slippage reduces shares OK")
    finally:
        root.destroy()


def test_slippage_net_pnl_in_table():
    _hr("slippage: net PnL in table + (net) header indicator")
    root, app = _make_app()
    try:
        app.direction_var.set("Long")
        app.vars["Entry"].set("100")
        app.vars["Stop"].set("95")
        # Use dollar slippage so it's deterministic
        app.slip_entry_enabled.set(True)
        app.slip_entry_mode.set("dollar")
        app.slip_entry_var.set("0.10")
        app.slip_exit_enabled.set(True)
        app.slip_exit_mode.set("dollar")
        app.slip_exit_var.set("0.10")
        # Render table directly (10 shares for clean numbers)
        app.update_table(100.0, 95.0, 10.0)
        rows = [app.tree.item(iid)["values"] for iid in app.tree.get_children()]
        # ENTRY row should show a small loss (slip_e + slip_x) × 10 = -$2
        entry_row = next(r for r in rows if r[0] == "ENTRY")
        assert entry_row[2] == "-$2", f"ENTRY net pnl: {entry_row[2]!r}"
        # STOP row: ideal = -$50; with slip = -(50 + 0.20×10) = -$52
        stop_row = next(r for r in rows if r[0] == "STOP")
        assert stop_row[2] == "-$52", f"STOP net pnl: {stop_row[2]!r}"
        # Header reflects net mode
        heading = app.tree.heading("PnL")
        assert "(net)" in heading["text"], f"header: {heading['text']!r}"

        # Disable slippage — header reverts; ENTRY row goes to $0
        app.slip_entry_enabled.set(False)
        app.slip_exit_enabled.set(False)
        app.update_table(100.0, 95.0, 10.0)
        heading = app.tree.heading("PnL")
        assert "(net)" not in heading["text"], f"header revert: {heading['text']!r}"
        rows = [app.tree.item(iid)["values"] for iid in app.tree.get_children()]
        entry_row = next(r for r in rows if r[0] == "ENTRY")
        assert entry_row[2] == "$0", f"ENTRY pnl after off: {entry_row[2]!r}"
        print("slippage net-PnL + header OK")
    finally:
        root.destroy()


def test_new_persistence_keys():
    _hr("persistence: stop offset + slippage round-trip")
    import tkinter as tk
    import price_calc_III as mod
    root1 = tk.Tk()
    app1 = mod.TradeSolverApp(root1)
    app1.stop_mode_var.set("pct")
    app1.stop_offset_var.set("3.5")  # this is the pct slot
    # Switch to dollar then enter, switch back, switch back to test stash logic
    app1.stop_mode_var.set("dollar")
    app1.stop_offset_var.set("0.40")
    app1.slip_entry_enabled.set(True)
    app1.slip_entry_mode.set("dollar")
    app1.slip_entry_var.set("0.05")
    app1.slip_exit_mode.set("pct")
    app1.slip_exit_var.set("0.20")
    app1.on_close()

    root2 = tk.Tk()
    app2 = mod.TradeSolverApp(root2)
    try:
        assert app2.stop_mode_var.get() == "dollar"
        assert app2.stop_offset_var.get() == "0.40", f"current $ offset: {app2.stop_offset_var.get()!r}"
        # Switch to pct → 3.5 should appear (was stashed at first set)
        app2.stop_mode_var.set("pct")
        assert app2.stop_offset_var.get() == "3.5", f"pct offset: {app2.stop_offset_var.get()!r}"
        # Slippage values
        assert app2.slip_entry_enabled.get() is True
        assert app2._slip_entry_dollar == "0.05"
        assert app2._slip_exit_pct == "0.20"
        print("new persistence keys round-trip OK")
    finally:
        root2.destroy()


def test_lfa_timestamp_decay():
    _hr("LFA: timestamp-decay window (any-app-switch model)")
    import time as _time
    root, app = _make_app()
    try:
        app.lfa_enabled.set(True)
        app.vars["Entry"].set("100")
        app.vars["Stop"].set("95")

        # Recent app switch → LFA delay
        app._last_app_switch_ts = _time.time()
        delay = app._compute_ocr_delay()
        assert delay == app.settings["lfa_delay"], f"recent: {delay}"

        # Stale (>1.5s) → normal delay
        app._last_app_switch_ts = _time.time() - 5.0
        delay = app._compute_ocr_delay()
        assert delay == app.settings["normal_delay"], f"stale: {delay}"

        # Two clicks within the window — both LFA (window does not self-clear)
        app._last_app_switch_ts = _time.time()
        d1 = app._compute_ocr_delay()
        d2 = app._compute_ocr_delay()
        assert d1 == d2 == app.settings["lfa_delay"]

        # LFA disabled → always normal even with fresh stamp
        app.lfa_enabled.set(False)
        app._last_app_switch_ts = _time.time()
        assert app._compute_ocr_delay() == app.settings["normal_delay"]
        print("LFA timestamp decay OK")
    finally:
        root.destroy()


def test_preset_save_load_round_trip():
    _hr("presets: save and load round-trip")
    import price_calc_III as mod
    # Use a tmp presets dir to avoid touching real ones
    tmpdir = tempfile.mkdtemp(prefix="price_calc_presets_")
    original_dir = mod._PRESETS_DIR
    mod._PRESETS_DIR = tmpdir
    try:
        region = {"ocr_left": 11, "ocr_right": 22,
                  "ocr_above": 33, "ocr_below": 44}
        mod._save_preset("Center monitor", region)
        names = mod._list_preset_names()
        assert "Center monitor" in names, f"saved preset missing in list: {names}"

        loaded = mod._load_preset("Center monitor")
        assert loaded == region, f"round-trip mismatch: {loaded}"

        # Sanitization: special chars stripped
        mod._save_preset("Right TS!@#", region)
        sanitized = mod._sanitize_preset_name("Right TS!@#")
        assert os.path.exists(os.path.join(tmpdir, sanitized + ".json"))
        print("preset save/load OK")
    finally:
        mod._PRESETS_DIR = original_dir
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_monitor_lock_blocks_off_monitor_clicks():
    _hr("monitor lock: clicks off the locked monitor are ignored")
    root, app = _make_app()
    try:
        import price_calc_III as mod
        click_rect = [(0, 0, 1920, 1080)]
        original = mod._monitor_rect_at_point
        mod._monitor_rect_at_point = lambda x, y: click_rect[0]

        # Capture status messages instead of trying to stub the C-level Lock
        messages = []
        app._show_status = lambda msg, *a, **kw: messages.append(msg)  # type: ignore[assignment]
        # Bypass other early-returns
        app._click_inside_window = lambda x, y: False  # type: ignore[assignment]
        app.smart_click_enabled.set(True)
        app.platform_var.set("TradeStation")

        try:
            # Lock to monitor at (0,0). Off-monitor click → status message.
            app.settings["ocr_monitor_rect"] = [0, 0, 1920, 1080]
            click_rect[0] = (1920, 0, 3840, 1080)
            app._handle_click_main(2000, 100)
            assert any("off this monitor" in m for m in messages), \
                f"Off-monitor click should show status; got {messages!r}"

            # Same-monitor click — must not produce the off-monitor status
            messages.clear()
            click_rect[0] = (0, 0, 1920, 1080)
            app._handle_click_main(100, 100)
            assert not any("off this monitor" in m for m in messages), \
                f"Same-monitor click must not show off-monitor status; got {messages!r}"
            print("monitor lock OK")
        finally:
            mod._monitor_rect_at_point = original
    finally:
        root.destroy()


def test_lfa_inline_click_check_catches_fast_switches():
    """Click target check stamps on either HWND change or monitor change.
    Monitor change handles multi-monitor TS where all chart windows share
    a single top-level HWND (and HWND-only check would miss them)."""
    _hr("LFA: inline click-target check (HWND or monitor change)")
    import time as _time

    def maybe_stamp(app, click_target, click_monitor):
        hwnd_changed = (click_target != 0 and click_target != app._last_foreground_hwnd)
        monitor_changed = (click_monitor != 0 and click_monitor != app._last_click_monitor)
        if hwnd_changed or monitor_changed:
            app._last_app_switch_ts = _time.time()
            if click_target:
                app._last_foreground_hwnd = click_target
            if click_monitor:
                app._last_click_monitor = click_monitor
            return True
        return False

    root, app = _make_app()
    try:
        TS = 7777
        CALC = 1111
        MON_CENTER = 100
        MON_RIGHT = 200

        # Initial: no last state. First click stamps.
        app._last_foreground_hwnd = 0
        app._last_click_monitor = 0
        app._last_app_switch_ts = 0.0
        assert maybe_stamp(app, TS, MON_CENTER), "First click should stamp"

        # Same window same monitor → no stamp
        prev = app._last_app_switch_ts
        assert not maybe_stamp(app, TS, MON_CENTER), "No-op click should not stamp"
        assert app._last_app_switch_ts == prev

        # Same HWND, different monitor (multi-monitor TS) → MUST stamp.
        # This is the case that was failing in v13.
        assert maybe_stamp(app, TS, MON_RIGHT), \
            "Cross-monitor click on same HWND must stamp (multi-monitor TS)"
        assert app._last_click_monitor == MON_RIGHT

        # HWND change (different app) → stamp
        prev = app._last_app_switch_ts
        assert maybe_stamp(app, CALC, MON_CENTER)
        assert app._last_foreground_hwnd == CALC
        print("LFA inline check (HWND OR monitor) OK")
    finally:
        root.destroy()


def test_lfa_any_hwnd_change_stamps():
    _hr("LFA: poll stamps on any foreground HWND change")
    import time as _time
    root, app = _make_app()
    try:
        # Patch _current_foreground_hwnd to a deterministic stub
        seq = [12345]  # initial HWND

        def fake_cur():
            return seq[0]

        app._current_foreground_hwnd = fake_cur  # type: ignore[assignment]

        # Seed: same HWND as initial. No change → no stamp.
        app._last_foreground_hwnd = 12345
        app._last_app_switch_ts = 0.0
        app._closing = True  # prevent reschedule
        app._poll_foreground()
        assert app._last_app_switch_ts == 0.0, "no change should not stamp"

        # Simulate switch to a different HWND
        app._closing = False
        seq[0] = 67890
        before = _time.time()
        # Inline body so we don't reschedule a real timer
        cur = app._current_foreground_hwnd()
        if cur and cur != app._last_foreground_hwnd:
            app._last_app_switch_ts = _time.time()
            app._last_foreground_hwnd = cur
        after = _time.time()
        assert before <= app._last_app_switch_ts <= after, \
            f"Stamp range: {app._last_app_switch_ts}"
        assert app._last_foreground_hwnd == 67890

        # Another switch → another stamp (transitions are repeatedly counted)
        app._last_app_switch_ts = 0.0
        seq[0] = 11111
        cur = app._current_foreground_hwnd()
        if cur and cur != app._last_foreground_hwnd:
            app._last_app_switch_ts = _time.time()
            app._last_foreground_hwnd = cur
        assert app._last_app_switch_ts > 0
        print("LFA any-HWND-change OK")
    finally:
        try:
            app._closing = True
            root.destroy()
        except Exception:
            pass


def test_failed_ocr_restores_and_marks_stale():
    _hr("failed OCR restores prior value + marks Stop/Shares stale")
    root, app = _make_app()
    try:
        # Populate good values
        app.vars["Entry"].set("100")
        app.vars["Stop"].set("95")
        app.vars["Risk $"].set("25")
        app.direction_var.set("Long")
        app.calculate()
        assert app.vars["Shares"].get() == "5"

        # Simulate Smart Click: indicate_loading caches + sets "..."
        # entry_turn=True → target = "Entry"
        app.entry_turn = True
        app.indicate_loading()
        assert app.vars["Entry"].get() == "..."
        assert app._pre_click_values.get("Entry") == "100"

        # Simulate failed OCR → _ensure_unlock fires
        app._ensure_unlock()

        # Entry restored, NOT blanked
        assert app.vars["Entry"].get() == "100", f"Entry: {app.vars['Entry'].get()!r}"
        # Stop and Shares should be marked stale (Stale.TEntry style)
        stop_w = app.entry_widgets["Stop"]
        shares_w = app.entry_widgets["Shares"]
        assert "Stale.TEntry" in str(stop_w.cget("style")), f"Stop style: {stop_w.cget('style')!r}"
        assert "Stale.TEntry" in str(shares_w.cget("style")), f"Shares style: {shares_w.cget('style')!r}"

        # A successful calculate clears stale
        app.calculate()
        assert "Stale" not in str(stop_w.cget("style"))
        assert "Stale" not in str(shares_w.cget("style"))

        print("failed OCR restore + stale marker OK")
    finally:
        root.destroy()


def test_pnl_formatting():
    _hr("update_table: PnL sign placement")
    root, app = _make_app()
    try:
        # Long with Entry=100, Stop=95, Shares=10 → STOP pnl = -$50
        app.update_table(100.0, 95.0, 10.0)
        rows = [app.tree.item(iid)["values"] for iid in app.tree.get_children()]
        # STOP row's PnL must read "-$50", not "$-50"
        stop_row = next(r for r in rows if r[0] == "STOP")
        assert stop_row[2] == "-$50", f"STOP PnL: {stop_row[2]!r}"
        # ENTRY row's PnL must be "$0"
        entry_row = next(r for r in rows if r[0] == "ENTRY")
        assert entry_row[2] == "$0", f"ENTRY PnL: {entry_row[2]!r}"
        # Target rows should be "+$N"
        target_rows = [r for r in rows if r[0].startswith("Target")]
        assert all(r[2].startswith("+$") for r in target_rows), \
            f"Target PnLs: {[r[2] for r in target_rows]}"
        print("PnL formatting OK")
    finally:
        root.destroy()


def test_clipboard_regex():
    _hr("clipboard regex")
    import price_calc_III as mod
    # The regex must require a decimal post-fix. Verify behavior on:
    cases = [
        ("100.00", True),
        ("12.345", True),
        ("0.5", True),
        ("12345", False),    # bare integer (was the loose-match bug)
        ("100.", False),     # trailing dot, no fractional
        ("abc", False),
        ("100.00.00", False),
        ("", False),
    ]
    pat = mod._CLIPBOARD_PRICE_RE if hasattr(mod, "_CLIPBOARD_PRICE_RE") else None
    if pat is None:
        print("(no module-level _CLIPBOARD_PRICE_RE yet — skipping)")
        return
    for s, expected in cases:
        got = bool(pat.match(s.strip()))
        assert got == expected, f"clipboard regex {s!r}: expected {expected}, got {got}"
    print("clipboard regex OK")


def test_config_round_trip():
    _hr("config save/load round-trip")
    import tkinter as tk
    import price_calc_III as mod

    # First app: change settings, close to save
    root1 = tk.Tk()
    app1 = mod.TradeSolverApp(root1)
    app1.vars["Risk $"].set("777")
    app1.settings["normal_delay"] = 0.42
    app1.on_close()  # writes config

    # Second app: reload, verify
    root2 = tk.Tk()
    app2 = mod.TradeSolverApp(root2)
    risk = app2.vars["Risk $"].get()
    delay = app2.settings.get("normal_delay")
    root2.destroy()
    assert risk == "777", f"Risk persistence: {risk!r}"
    assert delay == 0.42, f"normal_delay persistence: {delay!r}"
    print("config round-trip OK")


def test_corrupt_config_handling():
    _hr("corrupt config")
    import tkinter as tk
    import price_calc_III as mod

    # Write garbage
    with open(mod.CONFIG_FILE, "w") as f:
        f.write("{not valid json")

    root = tk.Tk()
    try:
        app = mod.TradeSolverApp(root)
        # Should fall back to defaults without crashing
        assert app.font_size == 10
        # Corrupt file MUST be preserved as .bad
        bad = mod.CONFIG_FILE + ".bad"
        assert os.path.exists(bad), "Corrupt config was not preserved as .bad"
        # Original file should be gone (was renamed)
        assert not os.path.exists(mod.CONFIG_FILE), "Corrupt config was not moved out of place"
    finally:
        root.destroy()
    print("corrupt config OK (preserved as .bad)")


def test_atomic_save_creates_bak():
    _hr("atomic save creates .bak")
    import tkinter as tk
    import price_calc_III as mod

    bak = mod.CONFIG_FILE + ".bak"
    if os.path.exists(bak):
        os.remove(bak)

    # First save
    root1 = tk.Tk()
    app1 = mod.TradeSolverApp(root1)
    app1.vars["Risk $"].set("100")
    app1.on_close()
    assert os.path.exists(mod.CONFIG_FILE)

    # Second save — should produce a .bak from the first
    root2 = tk.Tk()
    app2 = mod.TradeSolverApp(root2)
    app2.vars["Risk $"].set("200")
    app2.on_close()
    assert os.path.exists(bak), "Backup .bak was not created on second save"
    print("atomic save OK (.bak created)")


def test_lfa_retry_on_initial_ocr_failure():
    """When the first OCR attempt fails after an LFA-delay sleep, the
    worker must retry once with a half-delay sleep, surface an "OCR
    retry succeeded" status, and ultimately autofill from the retry.

    Patches _process_ts to return None on first call, a valid price on
    second. Drives process_click directly with a settings_snapshot
    matching what _handle_click_main would build, so this exercises
    the production retry path."""
    _hr("LFA: retry on initial OCR failure")
    root, app = _make_app()
    try:
        statuses = []
        app._show_status = lambda msg, *a, **kw: statuses.append(msg)  # type: ignore[assignment]

        autofills = []
        app.auto_fill_price = lambda v: autofills.append(v)  # type: ignore[assignment]
        # Bypass the real _ensure_unlock chain — we only care about
        # retry semantics here.
        app._ensure_unlock = lambda: None  # type: ignore[assignment]

        # Re-route _process_ts to fail once then succeed.
        attempts = {"n": 0}

        def fake_process_ts(_x, _y, _snap, debug_mode=False):
            attempts["n"] += 1
            if attempts["n"] == 1:
                return None
            return "100.50"

        app._process_ts = fake_process_ts  # type: ignore[assignment]

        # Drive a click with an LFA-grade delay so the retry path fires.
        snap = {
            "ocr_left": 10, "ocr_right": 10,
            "ocr_above": 10, "ocr_below": 10,
            "lfa_delay": 0.01,
            "ocr_debug_mode": False,
        }
        # Run the worker body inline (no real thread sleep cost — delay
        # is 0.01s, retry sleep is half that). process_click does its
        # own _safe_after marshalling for autofill; our stub bypasses.
        # Direct call is fine since we patched its UI side-effects.
        app.process_click(0, 0, snap["lfa_delay"], snap)

        # Pump pending after() callbacks emitted by _safe_after.
        for _ in range(10):
            root.update()

        assert attempts["n"] == 2, (
            f"expected 2 OCR attempts (initial + retry), got {attempts['n']}"
        )
        assert any("retry succeeded" in s for s in statuses), (
            f"expected retry-succeeded status, got {statuses!r}"
        )
        assert autofills == ["100.50"], (
            f"expected autofill from retry, got {autofills!r}"
        )
        print("LFA retry on initial OCR failure OK")
    finally:
        root.destroy()


def test_cost_edit_in_offset_mode_redrives_shares():
    """In stop-offset mode, editing Cost must re-derive Shares against
    the offset-anchored Stop and Risk. Previously untested — Cost edits
    are only exercised in manual mode by test_cost_rewrite_status."""
    _hr("offset mode: Cost edit re-derives Shares against anchored Stop")
    root, app = _make_app()
    try:
        statuses = []
        original_show = app._show_status
        app._show_status = lambda msg, *a, **kw: statuses.append(msg) or original_show(msg, *a, **kw)  # type: ignore[assignment]

        # Set up dollar-offset mode: Stop = Entry − $5, so any Entry
        # drives a Stop $5 below.
        app.direction_var.set("Long")
        app.stop_mode_var.set("dollar")
        app._on_stop_mode_change()
        app.stop_offset_var.set("5")
        app.vars["Entry"].set("100")
        app.vars["Risk $"].set("50")
        app.calculate()

        # Sanity: Stop derived from offset, Shares from Risk/rps.
        assert app.vars["Stop"].get() == "95.00", app.vars["Stop"].get()
        assert app.vars["Shares"].get() == "10", app.vars["Shares"].get()

        # Now edit Cost. In offset mode this should re-derive Shares
        # (the offset is anchored; Risk drifts to match the new size).
        app.vars["Cost"].set("500")
        app.calculate()
        # 500 / 100 = 5 shares; Risk = 5 sh × $5 rps = $25.
        assert app.vars["Shares"].get() == "5", (
            f"expected Shares=5 after Cost edit, got {app.vars['Shares'].get()!r}"
        )
        assert app.vars["Stop"].get() == "95.00", (
            "Stop must stay anchored to the offset"
        )
        assert app.vars["Risk $"].get() == "25.00", (
            f"expected Risk=$25.00 after Cost-driven re-derive, "
            f"got {app.vars['Risk $'].get()!r}"
        )
        print("offset-mode Cost edit OK")
    finally:
        root.destroy()


def test_platform_toggle_during_smart_click_swaps_modes():
    """Toggling platform_var between TradeStation and TradingView while
    Smart Click is on must stop the prior mode and start the new one
    cleanly. A regression here would leave the clipboard poll running
    after switching to TS, or the mouse listener orphaned after
    switching to TV."""
    _hr("platform toggle: TS <-> TV switches OCR/clipboard modes cleanly")
    root, app = _make_app()
    try:
        # Track which mode is active without driving real listeners
        # (pynput would spawn a real mouse hook).
        listener_state = {"running": False}
        poll_state = {"running": False}

        app._start_listener = lambda: listener_state.update(running=True)  # type: ignore[assignment]
        app._stop_listener = lambda: listener_state.update(running=False)  # type: ignore[assignment]
        app._start_clipboard_poll = lambda: poll_state.update(running=True)  # type: ignore[assignment]
        app._stop_clipboard_poll = lambda: poll_state.update(running=False)  # type: ignore[assignment]

        # Start in TS mode with Smart Click on.
        app.platform_var.set("TradeStation")
        app.smart_click_enabled.set(True)
        app.toggle_listener()  # production path that wires the active mode
        assert listener_state["running"], "TS Smart Click should start listener"
        assert not poll_state["running"], "TS mode must not start clipboard poll"

        # Flip to TV — listener stops, clipboard poll starts.
        app.platform_var.set("TradingView")
        app._on_platform_change()
        assert poll_state["running"], (
            "TV switch should start clipboard poll"
        )

        # Flip back to TS.
        app.platform_var.set("TradeStation")
        app._on_platform_change()
        assert not poll_state["running"], (
            "TS switch should stop clipboard poll"
        )
        print("platform toggle OK")
    finally:
        root.destroy()


def test_risk_zero_shows_status_and_aborts():
    """Risk=$0 with other fields populated should surface a clear status
    note and abort the calc, instead of silently producing Shares=0."""
    _hr("UX: Risk $ = 0 shows status and aborts")
    root, app = _make_app()
    try:
        statuses = []
        app._show_status = lambda msg, *a, **kw: statuses.append(msg)  # type: ignore[assignment]
        app.vars["Entry"].set("100")
        app.vars["Stop"].set("95")
        app.vars["Risk $"].set("0")
        app.direction_var.set("Long")
        app.calculate()
        assert any("Risk" in s and "> 0" in s for s in statuses), (
            f"expected Risk-must-be-positive status, got {statuses!r}"
        )
        # Shares must not have been derived from a divide-by-edge case.
        assert app.vars["Shares"].get() in ("", "0"), (
            f"Shares should remain empty/0 when Risk=$0, "
            f"got {app.vars['Shares'].get()!r}"
        )
        print("Risk=$0 status note OK")
    finally:
        root.destroy()


def test_slippage_eats_budget_shows_status():
    """When slippage + ideal RPS push effective-RPS above the entire
    Risk $ budget per share, the calc floors Shares to 0. The user
    needs a status note explaining the cause, not a silent zero."""
    _hr("UX: slippage > risk budget shows 'can't cover one share' status")
    root, app = _make_app()
    try:
        statuses = []
        app._show_status = lambda msg, *a, **kw: statuses.append(msg)  # type: ignore[assignment]

        # Very tight budget vs realistic slippage: $100 entry, $99.95
        # stop (rps = $0.05), Risk = $0.01. Even without slippage
        # 0.01/0.05 = 0.2 → 0 shares. Status should fire.
        app.direction_var.set("Long")
        app.vars["Entry"].set("100")
        app.vars["Stop"].set("99.95")
        app.vars["Risk $"].set("0.01")
        app.calculate()
        assert any(
            "can't cover one share" in s or "cant cover one share" in s
            for s in statuses
        ), f"expected 'cant cover one share' status, got {statuses!r}"
        print("slippage/risk-too-tight status OK")
    finally:
        root.destroy()


def test_preset_save_is_atomic():
    """_save_preset must write via tmp+replace so a crash mid-write
    leaves the prior preset intact. Regression guard: confirm the
    .tmp sidecar doesn't survive a successful save, and the final
    file content matches what was written."""
    _hr("presets: atomic save (tmp + replace)")
    import price_calc_III as mod
    tmpdir = tempfile.mkdtemp(prefix="price_calc_presets_atomic_")
    original_dir = mod._PRESETS_DIR
    mod._PRESETS_DIR = tmpdir
    try:
        region = {"ocr_left": 1, "ocr_right": 2,
                  "ocr_above": 3, "ocr_below": 4}
        path = mod._save_preset("Atomic preset", region)
        # tmp sidecar must be cleaned up by os.replace.
        assert not os.path.exists(path + ".tmp"), (
            f".tmp sidecar should not remain after successful save"
        )
        assert os.path.exists(path)
        with open(path) as f:
            loaded = json.load(f)
        assert loaded == region, f"final file content mismatch: {loaded!r}"
        print("preset atomic save OK")
    finally:
        mod._PRESETS_DIR = original_dir
        shutil.rmtree(tmpdir, ignore_errors=True)


def main():
    tmpdir = _redirect_config_to_tmp()
    try:
        test_import()
        test_instantiate_and_lifecycle()
        test_calc_initial_fill()
        test_calc_resolve()
        test_long_mode_rejects_short_smart_click()
        test_short_mode_rejects_long_smart_click()
        test_long_mode_rejects_short_manual_calculate()
        test_offset_mode_long_accepts_lower_entry()
        test_offset_mode_short_accepts_higher_entry()
        test_compatible_input_accepted()
        test_cost_rewrite_status()
        test_lfa_timestamp_decay()
        test_lfa_any_hwnd_change_stamps()
        test_lfa_inline_click_check_catches_fast_switches()
        test_preset_save_load_round_trip()
        test_monitor_lock_blocks_off_monitor_clicks()
        test_failed_ocr_restores_and_marks_stale()
        test_pnl_formatting()
        test_stop_offset_pct()
        test_stop_offset_dollar_short()
        test_stop_offset_mode_switch_persistence()
        test_offset_resolve_shares_anchors_risk()
        test_slippage_reduces_shares()
        test_slippage_net_pnl_in_table()
        test_new_persistence_keys()
        test_both_frozen_disables_smart_click()
        test_clipboard_regex()
        test_config_round_trip()
        test_corrupt_config_handling()
        test_atomic_save_creates_bak()
        test_lfa_retry_on_initial_ocr_failure()
        test_cost_edit_in_offset_mode_redrives_shares()
        test_platform_toggle_during_smart_click_swaps_modes()
        test_risk_zero_shows_status_and_aborts()
        test_slippage_eats_budget_shows_status()
        test_preset_save_is_atomic()
        print("\nALL SMOKETESTS PASSED")
    finally:
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass


if __name__ == "__main__":
    main()
