"""RC car control panel for LEGO WeDo 2.0 hubs.

Single mode: one hub, tank steering with one motor on port 1 and one on port 2.
Dual mode: one 4-motor car with two hubs. The front hub drives the front wheels and
the rear hub the rear wheels, from the same keys; it drives only while both are
connected. Each hub has its own Connect and Calibrate… (hold each arrow, click what
the car did); calibrations and the last mode are saved in rc_car_calibration.json
next to this file.

Keys: W/A/S/D or arrow keys to drive, Space = horn, Esc = stop.

Run:   double-click rc_car.pyw (no console window), or
       python rc_car.pyw   to see error messages in a terminal
"""

import asyncio
import json
import queue
import sys
import threading
import time
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

# Hub slots per mode: (slot, title, hub light on connect). In dual mode the car is a
# 4x4 truck with a motor per wheel: the front hub drives two wheels and the rear hub
# the other two, from the same keys. Their lights tell them apart, like a car's.
MODES = {"single": [("car", "Hub", "white")],
         "dual": [("front", "Front hub", "white"), ("rear", "Rear hub", "red")]}

# 4x4 truck calibration: for each hub port, which wheel it drives and which way.
# Only the wheel's side matters for driving (both left wheels get the same power).
WHEELS = [("FL", "Front L"), ("FR", "Front R"), ("RL", "Rear L"), ("RR", "Rear R")]
TRUCK_PORTS = [("front", 1), ("front", 2), ("rear", 1), ("rear", 2)]
# Per slot, per port: (wheel, sign); sign -1 = motor mounted mirrored (runs reversed)
DEFAULT_TRUCK = {"front": (("FL", -1), ("FR", 1)), "rear": (("RL", -1), ("RR", 1))}

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
TEST_MIN_MS = 1000   # a calibration Test click runs the motor at least this long (hold for longer)
KEY_RELEASE_MS = 40  # a key release counts only if no press follows within this (auto-repeat)


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


def read_settings():
    """Everything in the calibration file: single mode's calibration at the top level
    (as it always was, so rc_car_phone.py reads it unchanged), dual mode's under
    "truck", and the last mode."""
    try:
        data = json.loads(CAL_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_settings(**changes):
    """Update some keys of the calibration file, keeping the rest."""
    CAL_FILE.write_text(json.dumps({**read_settings(), **changes}))


def load_calibration(slot="car"):
    """Return (calibration, saved?) for a hub slot from the calibration file, or the default."""
    data = read_settings()
    section = data if slot == "car" else data.get(slot, {})
    try:
        forward, right = tuple(section["forward"]), tuple(section["right"])
        if valid_calibration(forward, right):
            return (forward, right), True
    except (KeyError, TypeError):
        pass
    return DEFAULT_CAL, False


def save_calibration(cal):
    """Save single mode's calibration."""
    forward, right = cal
    write_settings(forward=forward, right=right)


def valid_truck(truck):
    """Every hub port has a wheel and a direction, and no wheel is used twice."""
    try:
        ports = [truck[slot][port - 1] for slot, port in TRUCK_PORTS]
        wheels = [wheel for wheel, _ in ports]
        return (all(w in dict(WHEELS) and s in (1, -1) for w, s in ports)
                and len(set(wheels)) == len(wheels))
    except (KeyError, IndexError, TypeError, ValueError):
        return False


def load_truck():
    """Return (truck calibration, saved?) from the calibration file, or the default."""
    saved = read_settings().get("truck")
    try:
        truck = {slot: tuple((wheel, sign) for wheel, sign in saved[slot]) for slot in ("front", "rear")}
        if valid_truck(truck):
            return truck, True
    except (KeyError, TypeError, ValueError):
        pass
    return DEFAULT_TRUCK, False


def save_truck(truck):
    write_settings(truck={slot: [list(p) for p in ports] for slot, ports in truck.items()})


def truck_port_powers(left, right, ports):
    """(port 1, port 2) power for one truck hub: each port gets its wheel's side power,
    flipped if that motor runs reversed."""
    return tuple(sign * (left if wheel[1] == "L" else right) for wheel, sign in ports)


class KeyRepeatFilter:
    """Turns key events into clean press/release calls. macOS and Linux repeat a held
    key as release+press pairs, which would stop and restart the motors many times a
    second, so a release only counts if no press follows within KEY_RELEASE_MS.
    (Windows repeats only presses; the press handlers ignore those.)"""

    def __init__(self, widget, on_press, on_release):
        self.widget, self.on_press, self.on_release = widget, on_press, on_release
        self.pending = {}   # key -> after() id of its delayed release

    def press(self, key):
        if key in self.pending:   # auto-repeat: the key never really went up
            self.widget.after_cancel(self.pending.pop(key))
        self.on_press(key)

    def release(self, key):
        if key in self.pending:
            self.widget.after_cancel(self.pending[key])
        self.pending[key] = self.widget.after(KEY_RELEASE_MS, lambda: self._released(key))

    def _released(self, key):
        self.pending.pop(key, None)
        self.on_release(key)

    def cancel(self):
        for after_id in self.pending.values():
            self.widget.after_cancel(after_id)
        self.pending.clear()


class HubSlot:
    """One hub position (the single car's hub, or dual mode's front or rear hub)."""

    def __init__(self, key, title, led):
        self.key, self.title, self.led = key, title, led
        self.hub = None
        self.state = "disconnected"   # disconnected | connecting | connected
        self.future = None            # the connect coroutine while it runs
        self.target = (0, 0)          # (port 1 power, port 2 power) wanted right now
        self.cal, self.calibrated = load_calibration(key)

    def hub_label(self):
        """Hub name plus the end of its Bluetooth address, which tells two hubs apart."""
        if not self.hub or not self.hub.address:
            return ""
        return f"{self.hub.name} …{self.hub.address[-5:]}"


class RCCarApp:
    def __init__(self, root):
        self.root = root
        self.pressed = set()          # active directions: fwd/back/left/right
        self.keys = KeyRepeatFilter(root, self._press, self._release)
        self.events = queue.Queue()   # messages from the Bluetooth thread to the UI
        self.slots = {key: HubSlot(key, title, led)
                      for slots in MODES.values() for key, title, led in slots}
        mode = read_settings().get("mode")
        self.mode = mode if mode in MODES else "single"
        self.truck, self.truck_saved = load_truck()
        self.cal_win = None           # CalibrationWindow while it is open

        # Bluetooth runs on its own asyncio loop in a background thread
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()

        self._build_ui()
        self._show_mode()
        self._bind_keys()
        self._poll()

    def _active(self):
        """The hub slots the current mode drives."""
        return [self.slots[key] for key, _, _ in MODES[self.mode]]

    # ---------------- UI ----------------

    def _build_ui(self):
        r = self.root
        r.title("WeDo RC Car")
        r.configure(bg=BG, padx=20, pady=16)
        r.resizable(False, False)

        # Mode: one hub, or two hubs driving one 4-motor car
        mode_row = tk.Frame(r, bg=BG)
        mode_row.pack(fill="x")
        tk.Label(mode_row, text="Mode", bg=BG, fg=FG, font=(FONT, 10)).pack(side="left", padx=(0, 10))
        self.mode_btns = {}
        for mode, text in (("single", "Single · 1 hub"), ("dual", "Dual · 2 hubs")):
            b = self._button(mode_row, text, lambda m=mode: self._set_mode(m))
            b.pack(side="left", padx=(0, 6))
            self.mode_btns[mode] = b
        self.mode_hint = tk.Label(r, text="4×4 truck: front hub on the front motors, rear hub on the "
                                          "rear motors.\nConnect both, then calibrate the truck.",
                                  bg=BG, fg=MUTED, font=(FONT, 9), justify="left", anchor="w")

        # One card per hub; the mode decides which are shown
        self.slot_box = tk.Frame(r, bg=BG)
        self.slot_box.pack(fill="x", pady=(10, 4))
        for slot in self.slots.values():
            self._build_slot(slot)
        truck = self.truck_card = tk.Frame(self.slot_box, bg=PANEL, padx=10, pady=8)
        tk.Label(truck, text="4×4 truck", bg=PANEL, fg=FG, font=(FONT, 10, "bold")).pack(anchor="w")
        row = tk.Frame(truck, bg=PANEL)
        row.pack(fill="x", pady=(2, 0))
        self.truck_label = tk.Label(row, text="", bg=PANEL, fg=FG, font=(FONT, 9), justify="left", anchor="w")
        self.truck_label.pack(side="left", fill="x", expand=True)
        self._button(row, "Calibrate truck…", self._open_truck_calibration, bg=PANEL_HOVER).pack(side="right")
        self._show_truck()

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
        self.drive_note = tk.Label(r, text="", bg=BG, fg=ACCENT, font=(FONT, 9))
        self.drive_note.pack()

        # Speed
        speed_row = tk.Frame(r, bg=BG)
        speed_row.pack(fill="x", pady=(8, 4))
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

        # Motor power meters (per side: in dual mode front and rear wheels match)
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

        tk.Label(r, text="W A S D / arrow keys to drive  ·  Space horn  ·  Esc stop",
                 bg=BG, fg=MUTED, font=(FONT, 9)).pack(pady=(8, 0))

    def _build_slot(self, slot):
        card = slot.card = tk.Frame(self.slot_box, bg=PANEL, padx=10, pady=8)
        top = tk.Frame(card, bg=PANEL)
        top.pack(fill="x")
        slot.dot = tk.Canvas(top, width=12, height=12, bg=PANEL, highlightthickness=0)
        slot.dot_id = slot.dot.create_oval(1, 1, 11, 11, fill=MUTED, outline="")
        slot.dot.pack(side="left")
        tk.Label(top, text=slot.title, bg=PANEL, fg=FG, font=(FONT, 10, "bold")).pack(side="left", padx=(8, 8))
        slot.status = tk.Label(top, text="Not connected", bg=PANEL, fg=MUTED, font=(FONT, 9),
                               wraplength=230, justify="left")
        slot.status.pack(side="left")
        slot.connect_btn = self._button(top, "Connect", lambda: self._toggle_connection(slot), primary=True)
        slot.connect_btn.pack(side="right")
        slot.ports_label = tk.Label(card, text="", bg=PANEL, fg=MUTED, font=(FONT, 9), anchor="w")
        slot.ports_label.pack(fill="x", pady=(4, 0))
        if slot.key != "car":
            return   # dual mode's hubs are calibrated together, as the truck
        cal_row = tk.Frame(card, bg=PANEL)
        cal_row.pack(fill="x", pady=(2, 0))
        slot.cal_label = tk.Label(cal_row, text="", bg=PANEL, fg=FG, font=(FONT, 9),
                                  justify="left", anchor="w")
        slot.cal_label.pack(side="left", fill="x", expand=True)
        self._button(cal_row, "Calibrate…", lambda: self._open_calibration(slot),
                     bg=PANEL_HOVER).pack(side="right")
        self._show_calibration(slot)

    def _button(self, parent, text, command, primary=False, bg=None):
        bg, fg = (ACCENT, "#111") if primary else (bg or PANEL, FG)
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

    def _set_status(self, slot, text, color):
        slot.status.configure(text=text, fg=FG if color == GREEN else color)
        slot.dot.itemconfigure(slot.dot_id, fill=color)

    def _show_calibration(self, slot, note=""):
        forward, right = slot.cal
        parts = []
        for port, (f, r) in enumerate(zip(forward, right), start=1):
            wheel = "left" if f == r else "right"   # spinning right = left wheel forward
            parts.append(f"Port {port} = {wheel}" + (" (rev)" if f < 0 else ""))
        status = note or ("Calibrated" if slot.calibrated else "Not calibrated (default)")
        slot.cal_label.configure(text=status + "\n" + "  ·  ".join(parts))

    def _show_truck(self, note=""):
        names = dict(WHEELS)
        lines = [note or ("Calibrated" if self.truck_saved else "Not calibrated (default)")]
        for key, title, _ in MODES["dual"]:
            parts = [f"port {port} = {names[wheel].lower()}" + (" (rev)" if sign < 0 else "")
                     for port, (wheel, sign) in enumerate(self.truck[key], start=1)]
            lines.append(f"{title}: " + ",  ".join(parts))
        self.truck_label.configure(text="\n".join(lines))

    # ---------------- mode ----------------

    def _set_mode(self, mode):
        if mode == self.mode:
            return
        if self.cal_win:
            self.cal_win.close()
        for slot in self._active():   # the other mode uses other hub positions
            self._disconnect(slot)
        self.mode = mode
        try:
            write_settings(mode=mode)
        except OSError:
            pass
        self._show_mode()
        self._update_drive()

    def _show_mode(self):
        for mode, b in self.mode_btns.items():
            on = mode == self.mode
            b.configure(bg=ACCENT if on else PANEL, fg="#111" if on else FG)
        if self.mode == "dual":
            self.mode_hint.pack(fill="x", pady=(6, 0), after=self.mode_btns["dual"].master)
        else:
            self.mode_hint.pack_forget()
        for slot in self.slots.values():
            slot.card.pack_forget()
        self.truck_card.pack_forget()
        for slot in self._active():
            slot.card.pack(fill="x", pady=(0, 8))
        if self.mode == "dual":
            self.truck_card.pack(fill="x", pady=(0, 8))

    # ---------------- calibration ----------------

    def _open_calibration(self, slot):
        if self.cal_win:
            self.cal_win.win.lift()
            return
        self._stop()
        self.cal_win = CalibrationWindow(self, slot)

    def _set_calibration(self, slot, cal):
        slot.cal = cal
        try:
            save_calibration(cal)
            slot.calibrated = True
            self._show_calibration(slot)
        except OSError as e:
            self._show_calibration(slot, f"Calibrated (couldn't save file: {e.strerror})")

    def _open_truck_calibration(self):
        if self.cal_win:
            self.cal_win.win.lift()
            return
        self._stop()
        self.cal_win = TruckCalibrationWindow(self)

    def _set_truck(self, truck):
        self.truck = truck
        try:
            save_truck(truck)
            self.truck_saved = True
            self._show_truck()
        except OSError as e:
            self._show_truck(f"Calibrated (couldn't save file: {e.strerror})")

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
            self.keys.press(direction)

    def _on_key_up(self, event):
        direction = KEYS.get(event.keysym.lower())
        if direction:
            self.keys.release(direction)

    def _press(self, direction):
        if direction not in self.pressed:
            self.pressed.add(direction)
            self._update_drive()

    def _release(self, direction):
        if direction in self.pressed:
            self.pressed.discard(direction)
            self._update_drive()

    def _stop(self):
        self.keys.cancel()
        self.pressed.clear()
        self._update_drive()

    def _update_drive(self):
        if self.cal_win:
            return   # the calibration window is driving the motors
        p = self.pressed
        active = self._active()
        # Dual mode drives only with both hubs, so the car never drags a dead axle
        ready = all(slot.state == "connected" for slot in active)
        left, right = wheel_powers(p, self.speed.get()) if ready else (0, 0)
        self._draw_meter(self.meter_left, left)
        self._draw_meter(self.meter_right, right)
        for d, b in self.pad.items():
            b.configure(bg=ACCENT if d in p else PANEL, fg="#111" if d in p else FG)
        for slot in self.slots.values():   # 4x4: front and rear wheels on a side get the same power
            if slot not in active:
                slot.target = (0, 0)
            elif self.mode == "dual":
                slot.target = truck_port_powers(left, right, self.truck[slot.key])
            else:
                slot.target = port_powers(left, right, slot.cal)
        waiting = "Connect both hubs to drive" if len(active) > 1 else "Connect the hub to drive"
        self.drive_note.configure(text=waiting if p and not ready else "")

    # ---------------- hub actions (run on the Bluetooth thread) ----------------

    def _submit(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def _toggle_connection(self, slot):
        if slot.state == "disconnected":
            slot.state = "connecting"
            self._set_status(slot, "Searching… press the green button on this hub", ACCENT)
            slot.connect_btn.configure(text="Cancel")
            slot.future = self._submit(self._connect(slot))
        else:
            self._disconnect(slot)

    def _disconnect(self, slot):
        if slot.state == "connecting":
            slot.future.cancel()
            self.events.put(("disconnected", slot.key, None))
        elif slot.state == "connected" and slot.hub:
            self._stop()
            self._submit(slot.hub.disconnect())

    async def _connect(self, slot):
        hub = WeDoHub()
        hub.on_disconnect = lambda: self.events.put(("disconnected", slot.key, None))
        # Never pick up a hub another slot already has
        taken = [s.hub.address for s in self.slots.values() if s.hub and s is not slot and s.hub.address]
        try:
            await hub.connect(exclude=taken)
        except Exception as e:
            self.events.put(("error", slot.key, str(e)))
            return
        slot.hub = hub
        self.events.put(("connected", slot.key, None))
        await hub.led(slot.led)
        await self._drive_loop(hub, slot)

    async def _drive_loop(self, hub, slot):
        """Send motor power to the hub whenever the wanted power changes."""
        sent = (None, None)
        while hub.client.is_connected:
            target = slot.target
            if target != sent:
                if target[0] != sent[0]:
                    await hub.motor(1, target[0])
                if target[1] != sent[1]:
                    await hub.motor(2, target[1])
                sent = target
            await asyncio.sleep(0.02)

    def _connected(self):
        return [slot for slot in self._active() if slot.state == "connected" and slot.hub]

    def _horn(self):
        connected = self._connected()
        if connected:   # one hub is enough (two would beep out of step)
            self._submit(connected[0].hub.beep(392, 250))

    def _set_light(self, name):
        for slot in self._connected():
            self._submit(slot.hub.led(name))

    # ---------------- UI updates from the Bluetooth thread ----------------

    def _poll(self):
        while not self.events.empty():
            kind, key, info = self.events.get()
            slot = self.slots[key]
            if kind == "connected":
                slot.state = "connected"
                self._set_status(slot, f"Connected · {slot.hub_label()}", GREEN)
                slot.connect_btn.configure(text="Disconnect")
                self._update_drive()   # dual mode may be ready to drive now
            elif kind in ("disconnected", "error"):
                slot.state = "disconnected"
                slot.hub = None
                self._stop()   # in dual mode this also stops the other hub
                self._set_status(slot, info or "Not connected", RED if kind == "error" else MUTED)
                slot.connect_btn.configure(text="Connect")
                slot.ports_label.configure(text="")

        for slot in self._connected():
            ports = dict(slot.hub.ports)
            parts = [f"Port {n}: {'motor' if ports.get(n) == MOTOR else 'empty' if n not in ports else 'sensor'}"
                     for n in (1, 2)]
            warn = "" if list(ports.values()).count(MOTOR) == 2 else "   ⚠ plug a motor into both ports"
            slot.ports_label.configure(text="   ·   ".join(parts) + warn)

        self.root.after(50, self._poll)

    def close(self):
        for slot in self.slots.values():
            if slot.hub:
                try:
                    self._submit(slot.hub.disconnect()).result(timeout=3)
                except Exception:
                    pass
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.root.destroy()


class CalibrationWindow:
    """Hold an arrow to send a raw test pattern to one hub's motors, then record
    which way the car actually moved."""

    def __init__(self, app, slot):
        self.app = app
        self.slot = slot
        self.held = set()
        self.test_started = 0.0
        self.test_stop_id = None   # after() id that ends a short Test click

        # Start from what the current calibration says each test pattern does
        forward, right = slot.cal
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
        if slot.state != "connected":
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
            test.bind("<ButtonPress-1>", lambda e, k=key: self._test_press(k))
            test.bind("<ButtonRelease-1>", lambda e, k=key: self._test_release(k))
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

        self.keys = KeyRepeatFilter(w, self._press, self._release)
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
        if key:
            self.keys.press(key)

    def _on_key_up(self, event):
        key = KEYS.get(event.keysym.lower())
        if key:
            self.keys.release(key)

    def _press(self, key):
        if key in self.held:   # key auto-repeat
            return
        self.held.add(key)
        self._drive()

    def _release(self, key):
        self.held.discard(key)
        self._drive()

    def _test_press(self, key):
        if self.test_stop_id:
            self.win.after_cancel(self.test_stop_id)
            self.test_stop_id = None
            self.held.clear()
        self.test_started = time.monotonic()
        self._press(key)

    def _test_release(self, key):
        """A Test click runs at least TEST_MIN_MS, so the car moves far enough to see."""
        left_ms = TEST_MIN_MS - (time.monotonic() - self.test_started) * 1000
        if left_ms > 0:
            self.test_stop_id = self.win.after(int(left_ms), lambda: self._end_test(key))
        else:
            self._release(key)

    def _end_test(self, key):
        self.test_stop_id = None
        self._release(key)

    def _stop_test(self):
        self.keys.cancel()
        if self.test_stop_id:
            self.win.after_cancel(self.test_stop_id)
            self.test_stop_id = None
        self.held.clear()
        self._drive()

    def _drive(self):
        """Run the held arrow's raw pattern on this hub only (one arrow at a time,
        so the result is clear)."""
        if len(self.held) == 1:
            key = next(iter(self.held))
            power = self.app.speed.get()
            a, b = TEST_PATTERNS[key]
            self.slot.target = (motor_power(a * power), motor_power(b * power))
        else:
            self.slot.target = (0, 0)
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
            self.app._set_calibration(self.slot, cal)
            self.close()

    def close(self):
        self.keys.cancel()
        if self.test_stop_id:
            self.win.after_cancel(self.test_stop_id)
        self.held.clear()
        self.slot.target = (0, 0)
        self.app.cal_win = None
        self.win.destroy()
        self.app._update_drive()


class TruckCalibrationWindow:
    """4x4 truck calibration: run one hub port at a time and click which wheel turned
    and which way it rolled; then drive with the arrows to check before saving."""

    def __init__(self, app):
        self.app = app
        self.testing = None   # (slot, port) whose motor is running
        self.test_started = 0.0
        self.test_stop_id = None   # after() id that ends a short Test click
        self.held = set()     # arrows held for the check drive
        self.wheel = {(s, p): app.truck[s][p - 1][0] for s, p in TRUCK_PORTS}
        self.sign = {(s, p): app.truck[s][p - 1][1] for s, p in TRUCK_PORTS}

        w = self.win = tk.Toplevel(app.root)
        w.title("Calibrate 4×4 truck")
        w.configure(bg=BG, padx=20, pady=16)
        w.resizable(False, False)
        w.transient(app.root)

        tk.Label(w, text="Lift the truck so its wheels are off the ground.\n"
                         "Hold Test (or keys 1–4): one motor turns. Click which wheel turned\n"
                         "and which way it rolled.",
                 bg=BG, fg=FG, font=(FONT, 10), justify="left").pack(anchor="w")
        missing = [s.title.lower() for s in (app.slots["front"], app.slots["rear"]) if s.state != "connected"]
        if missing:
            tk.Label(w, text=f"Not connected: connect the {' and '.join(missing)} first.",
                     bg=BG, fg=RED, font=(FONT, 9)).pack(anchor="w", pady=(4, 0))

        grid = tk.Frame(w, bg=BG)
        grid.pack(pady=10, anchor="w")
        self.tests, self.wheel_chips, self.sign_chips = {}, {}, {}
        for row, key in enumerate(TRUCK_PORTS):
            slot, port = key
            tk.Label(grid, text=f"{row + 1}   {app.slots[slot].title} · port {port}", bg=BG, fg=FG,
                     font=(FONT, 9, "bold"), anchor="w").grid(row=row, column=0, sticky="w", padx=(0, 8))
            test = tk.Label(grid, text="Test", bg=PANEL, fg=FG, font=(FONT, 9, "bold"), padx=10, cursor="hand2")
            test.grid(row=row, column=1, padx=(0, 12), pady=3, sticky="ns")
            test.bind("<ButtonPress-1>", lambda e, k=key: self._test_press(k))
            test.bind("<ButtonRelease-1>", lambda e: self._test_release())
            self.tests[key] = test
            for col, (wheel, name) in enumerate(WHEELS, start=2):
                chip = tk.Label(grid, text=name, width=7, pady=5, font=(FONT, 9), cursor="hand2")
                chip.grid(row=row, column=col, padx=2, pady=3)
                chip.bind("<Button-1>", lambda e, k=key, wh=wheel: self._choose_wheel(k, wh))
                self.wheel_chips[key, wheel] = chip
            for col, (sign, name) in enumerate(((1, "Forward"), (-1, "Backward")), start=7):
                chip = tk.Label(grid, text=name, width=9, pady=5, font=(FONT, 9), cursor="hand2")
                chip.grid(row=row, column=col, padx=(12 if sign > 0 else 2, 2), pady=3)
                chip.bind("<Button-1>", lambda e, k=key, s=sign: self._choose_sign(k, s))
                self.sign_chips[key, sign] = chip

        tk.Label(w, text="Then check: hold ▲ ▼ ◀ ▶ to drive the truck with these settings.\n"
                         "On ▲ all four wheels should roll forward.",
                 bg=BG, fg=MUTED, font=(FONT, 9), justify="left").pack(anchor="w")
        self.msg = tk.Label(w, text="", bg=BG, fg=MUTED, font=(FONT, 9), anchor="w", justify="left")
        self.msg.pack(fill="x", pady=(6, 0))
        buttons = tk.Frame(w, bg=BG)
        buttons.pack(fill="x", pady=(10, 0))
        self.save_btn = app._button(buttons, "Save", self._save, primary=True)
        self.save_btn.pack(side="right")
        app._button(buttons, "Cancel", self.close).pack(side="right", padx=8)

        self.keys = KeyRepeatFilter(w, self._key_press, self._key_release)
        w.bind("<KeyPress>", self._on_key_down)
        w.bind("<KeyRelease>", self._on_key_up)
        w.bind("<Escape>", lambda e: self.close())
        # Safety: stop testing if this window loses focus while a key is held
        w.bind("<FocusOut>", lambda e: e.widget is w and self._stop_test())
        w.protocol("WM_DELETE_WINDOW", self.close)
        w.focus_force()
        self._refresh()

    # ---- testing ----

    @staticmethod
    def _key_of(event):
        """"1".."4" for the Test keys, "fwd"/"back"/"left"/"right" for the arrows, or None."""
        return event.char if event.char in ("1", "2", "3", "4") else KEYS.get(event.keysym.lower())

    def _on_key_down(self, event):
        key = self._key_of(event)
        if key:
            self.keys.press(key)

    def _on_key_up(self, event):
        key = self._key_of(event)
        if key:
            self.keys.release(key)

    def _key_press(self, key):
        if key.isdigit():
            self._test_press(TRUCK_PORTS[int(key) - 1])
        elif key not in self.held:
            self.held.add(key)
            self._drive()

    def _key_release(self, key):
        if key.isdigit():
            self._test_release()
        else:
            self.held.discard(key)
            self._drive()

    def _test_press(self, key):
        """Start one motor. It runs while held, and at least TEST_MIN_MS, so a quick
        click still turns the wheel far enough to see."""
        slot = self.app.slots[key[0]]
        if slot.state != "connected":
            self.msg.configure(text=f"The {slot.title.lower()} isn't connected: connect it first.", fg=RED)
            return
        if self.test_stop_id:
            self.win.after_cancel(self.test_stop_id)
            self.test_stop_id = None
        if key != self.testing:   # (a held key repeats its press)
            self.test_started = time.monotonic()
            self._test(key)
            self.msg.configure(text=f"{slot.title} · port {key[1]} is running: which wheel turned, "
                                    f"and which way?", fg=ACCENT)

    def _test_release(self):
        if not self.testing:
            return
        left_ms = TEST_MIN_MS - (time.monotonic() - self.test_started) * 1000
        if left_ms > 0:
            self.test_stop_id = self.win.after(int(left_ms), self._end_test)
        else:
            self._end_test()

    def _end_test(self):
        self.test_stop_id = None
        self._test(None)
        self._refresh()

    def _test(self, key):
        if key != self.testing:
            self.testing = key
            self._drive()

    def _stop_test(self):
        self.keys.cancel()
        if self.test_stop_id:
            self.win.after_cancel(self.test_stop_id)
            self.test_stop_id = None
        self.testing = None
        self.held.clear()
        self._drive()

    def _drive(self):
        """One motor forward while its Test is held, else the check drive with the
        chosen settings (only once they're complete)."""
        power = self.app.speed.get()
        targets = {"front": [0, 0], "rear": [0, 0]}
        if self.testing:
            slot, port = self.testing
            targets[slot][port - 1] = motor_power(power)
        elif self.held:
            truck, _ = self._result()
            if truck:
                left, right = wheel_powers(self.held, power)
                targets = {slot: truck_port_powers(left, right, truck[slot]) for slot in targets}
        for slot, target in targets.items():
            self.app.slots[slot].target = tuple(target)
        for key, test in self.tests.items():
            on = key == self.testing
            test.configure(bg=ACCENT if on else PANEL, fg="#111" if on else FG)

    # ---- choosing ----

    def _choose_wheel(self, key, wheel):
        # Each wheel has one motor, so take it away from any other port
        for other, chosen in self.wheel.items():
            if chosen == wheel:
                self.wheel[other] = None
        self.wheel[key] = wheel
        self._refresh()

    def _choose_sign(self, key, sign):
        self.sign[key] = sign
        self._refresh()

    def _result(self):
        """Return (truck calibration, None) if every port is filled in, else (None, reason)."""
        if any(self.wheel[k] is None for k in TRUCK_PORTS):
            return None, "Pick the wheel each motor turns."
        truck = {slot: tuple((self.wheel[slot, p], self.sign[slot, p]) for p in (1, 2))
                 for slot in ("front", "rear")}
        if not valid_truck(truck):
            return None, "Pick Forward or Backward for every motor."
        return truck, None

    def _refresh(self):
        for (key, wheel), chip in self.wheel_chips.items():
            on = self.wheel[key] == wheel
            chip.configure(bg=ACCENT if on else PANEL, fg="#111" if on else FG)
        for (key, sign), chip in self.sign_chips.items():
            on = self.sign[key] == sign
            chip.configure(bg=ACCENT if on else PANEL, fg="#111" if on else FG)
        truck, reason = self._result()
        if truck:
            self.msg.configure(text="✓ Complete. Check with the arrows, then click Save.", fg=GREEN)
        else:
            self.msg.configure(text=reason, fg=MUTED)
        self.save_btn.configure(bg=ACCENT if truck else PANEL, fg="#111" if truck else MUTED)

    def _save(self):
        truck, _ = self._result()
        if truck:
            self.app._set_truck(truck)
            self.close()

    def close(self):
        self._stop_test()
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
    if sys.platform == "darwin":   # macOS Tk assumes 72 dpi, which draws text ~25% small
        root.tk.call("tk", "scaling", 96 / 72)
    app = RCCarApp(root)
    root.protocol("WM_DELETE_WINDOW", app.close)
    root.mainloop()
