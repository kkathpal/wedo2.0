"""RC car control panel for a LEGO WeDo 2.0 hub.

Tank steering with one motor on port 1 and one on port 2. Use Setup →
Calibrate to tell the program which way your car actually moves; the
answer is saved to rc_car_calibration.json next to this file.

Keys: W/A/S/D or arrow keys to drive, Space = horn, Esc = stop.

Run:   double-click rc_car.pyw (no console window), or
       python rc_car.pyw   to see error messages in a terminal
"""

import asyncio
import json
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox

try:
    from wedo2 import MOTOR, WeDoHub
except ImportError as e:   # a double-clicked .pyw has no console, so say it in a window
    tk.Tk().withdraw()
    messagebox.showerror("WeDo RC Car", f"{e}\n\nInstall what's missing once with:\n"
                                        "    python -m pip install bleak")
    sys.exit(1)

BG = "#1b1c21"
PANEL = "#2a2c34"
PANEL_HOVER = "#353843"
FG = "#ececef"
MUTED = "#8f929c"
ACCENT = "#f5c518"   # LEGO yellow
GREEN = "#4cc96b"
RED = "#ef5350"
FONT = "Segoe UI"

KEYS = {"up": "fwd", "w": "fwd", "down": "back", "s": "back",
        "left": "left", "a": "left", "right": "right", "d": "right"}

LIGHTS = [("white", "#ffffff"), ("red", "#ff3b30"), ("orange", "#ff8a00"),
          ("yellow", "#ffd400"), ("green", "#34c759"), ("blue", "#0a84ff"),
          ("purple", "#a050ff"), ("off", "#3a3a3a")]

CAL_FILE = Path(__file__).with_name("rc_car_calibration.json")

# Calibration = (port 1, port 2) power signs that make the car go forward,
# and the signs that make it spin right. Default: port 1 motor mounted mirrored.
DEFAULT_CAL = ((-1, 1), (-1, -1))

# Raw (port 1, port 2) power signs each arrow key sends while calibrating
TEST_PATTERNS = {"fwd": (1, 1), "back": (-1, -1), "left": (-1, 1), "right": (1, -1)}
ARROWS = [("fwd", "▲"), ("back", "▼"), ("left", "◀"), ("right", "▶")]
MOTIONS = [("fwd", "Forward"), ("back", "Backward"), ("left", "Left"), ("right", "Right")]

# WeDo motors stall below about this much power, so a wheel that should move
# always gets at least this. Raise it if a wheel still sits still on turns.
MIN_POWER = 35
ARC_INSIDE = 0.5   # inside wheel speed while curving (0.5 = half of the outside wheel)


def motor_power(power):
    """Round to an int and lift any non-zero power up to MIN_POWER."""
    if not power:
        return 0
    return int(max(abs(power), MIN_POWER)) * (1 if power > 0 else -1)


def wheel_powers(pressed, speed):
    """(left, right) wheel power, + = forward, for the held directions ("fwd", "back",
    "left", "right"). Shared with rc_car_phone.py so the phone drives the same way."""
    forward = ("fwd" in pressed) - ("back" in pressed)
    turn = ("right" in pressed) - ("left" in pressed)
    if forward:
        # Drive in an arc: slow down the wheel on the inside of the turn,
        # keeping the outside wheel fast enough that the inside one doesn't stall
        if turn:
            speed = max(speed, MIN_POWER / ARC_INSIDE)
        left = speed * forward * (ARC_INSIDE if turn < 0 else 1)
        right = speed * forward * (ARC_INSIDE if turn > 0 else 1)
    else:
        # Spin in place at full speed: skidding the tyres sideways takes extra power
        left, right = speed * turn, -speed * turn
    return motor_power(left), motor_power(right)


def port_powers(left, right, cal):
    """(port 1, port 2) power: each port gets its wheel's power, flipped if that motor
    runs reversed (see valid_calibration)."""
    forward_signs, right_signs = cal
    return tuple(f * (left if f == r else right) for f, r in zip(forward_signs, right_signs))


def valid_calibration(forward, right):
    """Forward and spin-right must drive the two motors in different combinations."""
    signs = (1, -1)
    return (len(forward) == len(right) == 2 and all(x in signs for x in (*forward, *right))
            and forward[0] * forward[1] != right[0] * right[1])


def load_calibration():
    """Return (calibration, saved?) from the calibration file, or the default."""
    try:
        data = json.loads(CAL_FILE.read_text())
        forward, right = tuple(data["forward"]), tuple(data["right"])
        if valid_calibration(forward, right):
            return (forward, right), True
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return DEFAULT_CAL, False


class RCCarApp:
    def __init__(self, root):
        self.root = root
        self.hub = None
        self.state = "disconnected"   # disconnected | connecting | connected
        self.pressed = set()          # active directions: fwd/back/left/right
        self.target = (0, 0)          # (port 1 power, port 2 power) wanted right now
        self.events = queue.Queue()   # messages from the Bluetooth thread to the UI
        self.cal, self.calibrated = load_calibration()
        self.cal_win = None           # CalibrationWindow while it is open

        # Bluetooth runs on its own asyncio loop in a background thread
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()

        self._build_ui()
        self._bind_keys()
        self._poll()

    # ---------------- UI ----------------

    def _build_ui(self):
        r = self.root
        r.title("WeDo RC Car")
        r.configure(bg=BG, padx=20, pady=16)
        r.resizable(False, False)

        # Connection bar
        top = tk.Frame(r, bg=BG)
        top.pack(fill="x")
        self.dot = tk.Canvas(top, width=12, height=12, bg=BG, highlightthickness=0)
        self.dot_id = self.dot.create_oval(1, 1, 11, 11, fill=MUTED, outline="")
        self.dot.pack(side="left")
        self.status = tk.Label(top, text="Not connected", bg=BG, fg=FG, font=(FONT, 11))
        self.status.pack(side="left", padx=8)
        self.connect_btn = self._button(top, "Connect", self._toggle_connection, primary=True)
        self.connect_btn.pack(side="right")
        self.ports_label = tk.Label(r, text="", bg=BG, fg=MUTED, font=(FONT, 9), anchor="w")
        self.ports_label.pack(fill="x", pady=(4, 10))

        # D-pad
        pad = tk.Frame(r, bg=BG)
        pad.pack(pady=6)
        self.pad = {}
        self._pad_button(pad, "▲", "fwd", 0, 1)
        self._pad_button(pad, "◀", "left", 1, 0)
        self._pad_button(pad, "▶", "right", 1, 2)
        self._pad_button(pad, "▼", "back", 2, 1)
        stop = tk.Label(pad, text="STOP", width=6, height=3, bg=RED, fg="white",
                        font=(FONT, 11, "bold"), cursor="hand2")
        stop.grid(row=1, column=1, padx=4, pady=4)
        stop.bind("<ButtonPress-1>", lambda e: self._stop())

        # Speed
        speed_row = tk.Frame(r, bg=BG)
        speed_row.pack(fill="x", pady=(12, 4))
        tk.Label(speed_row, text="Speed", bg=BG, fg=FG, font=(FONT, 10)).pack(side="left")
        self.speed = tk.IntVar(value=70)
        speed_text = tk.Label(speed_row, text="70%", width=5, bg=BG, fg=FG, font=(FONT, 10))
        speed_text.pack(side="right")

        def on_speed(value):
            speed_text.configure(text=f"{value}%")
            self._update_drive()

        tk.Scale(speed_row, from_=MIN_POWER, to=100, orient="horizontal", variable=self.speed,
                 showvalue=False, length=240, width=14, sliderlength=22, bg=ACCENT,
                 troughcolor=PANEL, highlightthickness=0, activebackground=ACCENT,
                 sliderrelief="flat", bd=0, takefocus=0, command=on_speed
                 ).pack(side="right", padx=8)

        # Motor power meters
        meters = tk.Frame(r, bg=BG)
        meters.pack(fill="x", pady=6)
        self.meter_left = self._meter(meters, "Left")
        self.meter_right = self._meter(meters, "Right")

        # Horn + lights
        actions = tk.Frame(r, bg=BG)
        actions.pack(fill="x", pady=(10, 4))
        self._button(actions, "📯  Horn (Space)", self._horn).pack(side="left")
        lights = tk.Frame(r, bg=BG)
        lights.pack(fill="x", pady=6)
        tk.Label(lights, text="Lights", bg=BG, fg=FG, font=(FONT, 10)).pack(side="left", padx=(0, 8))
        for name, color in LIGHTS:
            sw = tk.Canvas(lights, width=24, height=24, bg=BG, highlightthickness=0, cursor="hand2")
            sw.create_oval(2, 2, 22, 22, fill=color, outline=PANEL_HOVER, width=2)
            sw.pack(side="left", padx=2)
            sw.bind("<Button-1>", lambda e, n=name: self._set_light(n))

        # Setup
        setup = tk.LabelFrame(r, text=" Setup ", bg=BG, fg=MUTED, font=(FONT, 9), bd=1, relief="groove")
        setup.pack(fill="x", pady=(12, 6))
        self.cal_label = tk.Label(setup, text="", bg=BG, fg=FG, font=(FONT, 9),
                                  justify="left", anchor="w")
        self.cal_label.pack(side="left", fill="x", expand=True, padx=6, pady=6)
        self._button(setup, "Calibrate…", self._open_calibration).pack(side="right", padx=6, pady=6)
        self._show_calibration()

        tk.Label(r, text="W A S D / arrow keys to drive  ·  Space horn  ·  Esc stop",
                 bg=BG, fg=MUTED, font=(FONT, 9)).pack(pady=(8, 0))

    def _button(self, parent, text, command, primary=False):
        bg, fg = (ACCENT, "#111") if primary else (PANEL, FG)
        b = tk.Label(parent, text=text, bg=bg, fg=fg, font=(FONT, 10, "bold"),
                     padx=14, pady=6, cursor="hand2")
        b.bind("<Button-1>", lambda e: command())
        return b

    def _pad_button(self, parent, text, direction, row, col):
        b = tk.Label(parent, text=text, width=4, height=2, bg=PANEL, fg=FG,
                     font=(FONT, 20, "bold"), cursor="hand2")
        b.grid(row=row, column=col, padx=4, pady=4)
        b.bind("<ButtonPress-1>", lambda e: self._press(direction))
        b.bind("<ButtonRelease-1>", lambda e: self._release(direction))
        self.pad[direction] = b

    def _meter(self, parent, label):
        f = tk.Frame(parent, bg=BG)
        f.pack(side="left", expand=True, fill="x")
        tk.Label(f, text=label, bg=BG, fg=MUTED, font=(FONT, 9)).pack(anchor="w")
        c = tk.Canvas(f, width=160, height=16, bg=PANEL, highlightthickness=0)
        c.pack(anchor="w")
        c.create_line(80, 0, 80, 16, fill=MUTED)
        bar = c.create_rectangle(80, 2, 80, 14, fill=ACCENT, outline="")
        text = tk.Label(f, text="0", bg=BG, fg=FG, font=(FONT, 9))
        text.pack(anchor="w")
        return c, bar, text

    def _draw_meter(self, meter, power):
        c, bar, text = meter
        c.coords(bar, 80, 2, 80 + power * 0.78, 14)
        c.itemconfigure(bar, fill=ACCENT if power >= 0 else RED)
        text.configure(text=f"{power:+d}" if power else "0")

    def _set_status(self, text, color):
        self.status.configure(text=text)
        self.dot.itemconfigure(self.dot_id, fill=color)

    def _show_calibration(self, note=""):
        forward, right = self.cal
        parts = []
        for port, (f, r) in enumerate(zip(forward, right), start=1):
            wheel = "left" if f == r else "right"   # spinning right = left wheel forward
            parts.append(f"Port {port} = {wheel} wheel" + (" (reversed)" if f < 0 else ""))
        status = note or ("Calibrated" if self.calibrated else "Not calibrated (using default)")
        self.cal_label.configure(text=status + "\n" + "  ·  ".join(parts))

    # ---------------- calibration ----------------

    def _open_calibration(self):
        if self.cal_win:
            self.cal_win.win.lift()
            return
        self._stop()
        self.cal_win = CalibrationWindow(self)

    def _set_calibration(self, cal):
        self.cal = cal
        try:
            CAL_FILE.write_text(json.dumps({"forward": cal[0], "right": cal[1]}))
            self.calibrated = True
            self._show_calibration()
        except OSError as e:
            self._show_calibration(f"Calibrated (couldn't save file: {e.strerror})")

    # ---------------- driving ----------------

    def _bind_keys(self):
        self.root.bind("<KeyPress>", self._on_key_down)
        self.root.bind("<KeyRelease>", self._on_key_up)
        self.root.bind("<space>", lambda e: self._horn())
        self.root.bind("<Escape>", lambda e: self._stop())
        # Safety: stop if the window loses focus while a key is held
        self.root.bind("<FocusOut>", lambda e: e.widget is self.root and self._stop())

    def _on_key_down(self, event):
        direction = KEYS.get(event.keysym.lower())
        if direction:
            self._press(direction)

    def _on_key_up(self, event):
        direction = KEYS.get(event.keysym.lower())
        if direction:
            self._release(direction)

    def _press(self, direction):
        if direction not in self.pressed:
            self.pressed.add(direction)
            self._update_drive()

    def _release(self, direction):
        if direction in self.pressed:
            self.pressed.discard(direction)
            self._update_drive()

    def _stop(self):
        self.pressed.clear()
        self._update_drive()

    def _update_drive(self):
        if self.cal_win:
            return   # the calibration window is driving the motors
        p = self.pressed
        left, right = wheel_powers(p, self.speed.get())
        self._draw_meter(self.meter_left, left)
        self._draw_meter(self.meter_right, right)
        for d, b in self.pad.items():
            b.configure(bg=ACCENT if d in p else PANEL, fg="#111" if d in p else FG)
        self.target = port_powers(left, right, self.cal)

    # ---------------- hub actions (run on the Bluetooth thread) ----------------

    def _submit(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def _toggle_connection(self):
        if self.state == "disconnected":
            self.state = "connecting"
            self._set_status("Searching… press the green button on the hub", ACCENT)
            self.connect_btn.configure(text="Cancel")
            self._connect_future = self._submit(self._connect())
        elif self.state == "connecting":
            self._connect_future.cancel()
            self.events.put(("disconnected", None))
        elif self.hub:
            self._stop()
            self._submit(self.hub.disconnect())

    async def _connect(self):
        hub = WeDoHub()
        hub.on_disconnect = lambda: self.events.put(("disconnected", None))
        try:
            await hub.connect()
        except Exception as e:
            self.events.put(("error", str(e)))
            return
        self.hub = hub
        self.events.put(("connected", None))
        await hub.led("white")
        await self._drive_loop(hub)

    async def _drive_loop(self, hub):
        """Send motor power to the hub whenever the wanted power changes."""
        sent = (None, None)
        while hub.client.is_connected:
            target = self.target
            if target != sent:
                if target[0] != sent[0]:
                    await hub.motor(1, target[0])
                if target[1] != sent[1]:
                    await hub.motor(2, target[1])
                sent = target
            await asyncio.sleep(0.02)

    def _horn(self):
        if self.hub and self.state == "connected":
            self._submit(self.hub.beep(392, 250))

    def _set_light(self, name):
        if self.hub and self.state == "connected":
            self._submit(self.hub.led(name))

    # ---------------- UI updates from the Bluetooth thread ----------------

    def _poll(self):
        while not self.events.empty():
            kind, info = self.events.get()
            if kind == "connected":
                self.state = "connected"
                self._set_status("Connected", GREEN)
                self.connect_btn.configure(text="Disconnect")
            elif kind in ("disconnected", "error"):
                self.state = "disconnected"
                self.hub = None
                self._stop()
                self._set_status(info or "Disconnected", RED if kind == "error" else MUTED)
                self.connect_btn.configure(text="Connect")
                self.ports_label.configure(text="")

        if self.hub and self.state == "connected":
            ports = dict(self.hub.ports)
            parts = [f"Port {n}: {'motor' if ports.get(n) == MOTOR else 'empty' if n not in ports else 'sensor'}"
                     for n in (1, 2)]
            warn = "" if list(ports.values()).count(MOTOR) == 2 else "   ⚠ plug a motor into both ports"
            self.ports_label.configure(text="   ·   ".join(parts) + warn)

        self.root.after(50, self._poll)

    def close(self):
        if self.hub:
            try:
                self._submit(self.hub.disconnect()).result(timeout=3)
            except Exception:
                pass
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.root.destroy()


class CalibrationWindow:
    """Hold an arrow to send a raw test pattern to the motors, then record
    which way the car actually moved."""

    def __init__(self, app):
        self.app = app
        self.held = set()

        # Start from what the current calibration says each test pattern does
        forward, right = app.cal
        flip = lambda signs: tuple(-x for x in signs)
        predicted = {forward: "fwd", flip(forward): "back", right: "right", flip(right): "left"}
        self.choice = {key: predicted[pattern] for key, pattern in TEST_PATTERNS.items()}

        w = self.win = tk.Toplevel(app.root)
        w.title("Calibrate steering")
        w.configure(bg=BG, padx=20, pady=16)
        w.resizable(False, False)
        w.transient(app.root)

        tk.Label(w, text="Hold an arrow key (or its Test button) and watch the car.\n"
                         "Then click what the car actually did.",
                 bg=BG, fg=FG, font=(FONT, 10), justify="left").pack(anchor="w")
        if app.state != "connected":
            tk.Label(w, text="Not connected: connect to the hub first so the car can move.",
                     bg=BG, fg=RED, font=(FONT, 9)).pack(anchor="w", pady=(4, 0))

        grid = tk.Frame(w, bg=BG)
        grid.pack(pady=10)
        self.arrows = {}
        self.chips = {}
        for row, (key, arrow) in enumerate(ARROWS):
            a = tk.Label(grid, text=arrow, width=3, bg=PANEL, fg=FG, font=(FONT, 14, "bold"))
            a.grid(row=row, column=0, padx=4, pady=3, sticky="ns")
            self.arrows[key] = a
            test = tk.Label(grid, text="Test", bg=PANEL, fg=FG, font=(FONT, 9, "bold"),
                            padx=10, cursor="hand2")
            test.grid(row=row, column=1, padx=(0, 14), pady=3, sticky="ns")
            test.bind("<ButtonPress-1>", lambda e, k=key: self._press(k))
            test.bind("<ButtonRelease-1>", lambda e, k=key: self._release(k))
            for col, (motion, name) in enumerate(MOTIONS, start=2):
                chip = tk.Label(grid, text=name, width=9, pady=5, font=(FONT, 9), cursor="hand2")
                chip.grid(row=row, column=col, padx=2, pady=3)
                chip.bind("<Button-1>", lambda e, k=key, m=motion: self._choose(k, m))
                self.chips[key, motion] = chip

        self.msg = tk.Label(w, text="", bg=BG, fg=MUTED, font=(FONT, 9), anchor="w", justify="left")
        self.msg.pack(fill="x")
        buttons = tk.Frame(w, bg=BG)
        buttons.pack(fill="x", pady=(10, 0))
        self.save_btn = app._button(buttons, "Save", self._save, primary=True)
        self.save_btn.pack(side="right")
        app._button(buttons, "Cancel", self.close).pack(side="right", padx=8)

        w.bind("<KeyPress>", self._on_key_down)
        w.bind("<KeyRelease>", self._on_key_up)
        w.bind("<Escape>", lambda e: self.close())
        # Safety: stop testing if this window loses focus while a key is held
        w.bind("<FocusOut>", lambda e: e.widget is w and self._stop_test())
        w.protocol("WM_DELETE_WINDOW", self.close)
        w.focus_force()
        self._refresh()

    # ---- testing ----

    def _on_key_down(self, event):
        key = KEYS.get(event.keysym.lower())
        if key and key not in self.held:
            self._press(key)

    def _on_key_up(self, event):
        key = KEYS.get(event.keysym.lower())
        if key:
            self._release(key)

    def _press(self, key):
        self.held.add(key)
        self._drive()

    def _release(self, key):
        self.held.discard(key)
        self._drive()

    def _stop_test(self):
        self.held.clear()
        self._drive()

    def _drive(self):
        """Run the held arrow's raw pattern (only one at a time, so the result is clear)."""
        if len(self.held) == 1:
            key = next(iter(self.held))
            power = self.app.speed.get()
            a, b = TEST_PATTERNS[key]
            self.app.target = (motor_power(a * power), motor_power(b * power))
        else:
            self.app.target = (0, 0)
        for key, label in self.arrows.items():
            on = key in self.held
            label.configure(bg=ACCENT if on else PANEL, fg="#111" if on else FG)

    # ---- choosing ----

    def _choose(self, key, motion):
        # Each motion belongs to one arrow, so take it away from any other arrow
        for other, chosen in self.choice.items():
            if chosen == motion:
                self.choice[other] = None
        self.choice[key] = motion
        self._refresh()

    def _result(self):
        """Return (calibration, None) if the choices make sense, else (None, reason)."""
        by_motion = {m: k for k, m in self.choice.items() if m}
        if len(by_motion) < 4:
            return None, "Pick a motion for every arrow."
        pair = {self.choice["fwd"], self.choice["back"]}
        if pair not in ({"fwd", "back"}, {"left", "right"}):
            return None, ("⚠ ▲ and ▼ are exact opposites, so they must be Forward + Backward\n"
                          "or Left + Right. Test them again (and check both motors are plugged in).")
        return (TEST_PATTERNS[by_motion["fwd"]], TEST_PATTERNS[by_motion["right"]]), None

    def _refresh(self):
        for (key, motion), chip in self.chips.items():
            on = self.choice[key] == motion
            chip.configure(bg=ACCENT if on else PANEL, fg="#111" if on else FG)
        cal, reason = self._result()
        if cal:
            self.msg.configure(text="✓ Looks good. Click Save.", fg=GREEN)
        else:
            self.msg.configure(text=reason, fg=RED if reason.startswith("⚠") else MUTED)
        self.save_btn.configure(bg=ACCENT if cal else PANEL, fg="#111" if cal else MUTED)

    def _save(self):
        cal, _ = self._result()
        if cal:
            self.app._set_calibration(cal)
            self.close()

    def close(self):
        self.held.clear()
        self.app.target = (0, 0)
        self.app.cal_win = None
        self.win.destroy()
        self.app._update_drive()


if __name__ == "__main__":
    try:  # crisp text on high-DPI Windows screens
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    app = RCCarApp(root)
    root.protocol("WM_DELETE_WINDOW", app.close)
    root.mainloop()
