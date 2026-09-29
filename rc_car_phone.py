"""WeDo RC for phones: drive the WeDo 2.0 car from any phone browser on your Wi-Fi.

This PC keeps the Bluetooth connection to the hubs and serves a touch controller page
to phones on the same Wi-Fi. It drives exactly like rc_car.pyw: same turning, and the
same calibration (rc_car_calibration.json, made with Calibrate… in rc_car.pyw).

Single mode: one hub. Dual mode: the 4x4 truck with a front and a rear hub, using the
truck calibration. The hubs connect in order: press the FRONT hub's green button first
(its light turns white), then the REAR hub's (red). It drives only while both are
connected. Switch modes on the phone page; it starts in the mode rc_car.pyw used last.

Run:   python rc_car_phone.py               (uses a random free port from 8000-8999)
       python rc_car_phone.py --port 8090   (use this port)
       python rc_car_phone.py --dual        (start in dual mode; --single for single)
Then press the green button on the hub(s), and open the printed http://<this PC>:<port>
address on your phone. (Close rc_car.pyw first: a hub takes one connection at a time.)

Safety: the phone re-sends the held buttons every ~100 ms, and this server stops the
car if the phone goes quiet for PHONE_TIMEOUT, so lifting your finger, locking the
phone or losing Wi-Fi stops the car. In dual mode, if either hub drops, both stop.
"""
import asyncio
import json
import os
import random
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader

HERE = os.path.dirname(os.path.abspath(__file__))
_loader = SourceFileLoader("rc_car", os.path.join(HERE, "rc_car.pyw"))
rc = module_from_spec(spec_from_loader("rc_car", _loader))
_loader.exec_module(rc)

PORT_RANGE = (8000, 8999)   # each run uses a random free port from here (--port N picks one)
PAGE = os.path.join(HERE, "rc_car_phone.html")
PHONE_TIMEOUT = 0.35     # stop if the phone sends nothing for this long while driving (s)
DEFAULT_SPEED = 70
DIRECTIONS = {"fwd", "back", "left", "right"}
LIGHTS = [name for name, _ in rc.LIGHTS]
PHONE_LED = {"car": "blue"}   # single mode's hub turns blue when driven from the phone


class Slot:
    """One hub position: the single car's hub, or dual mode's front or rear hub."""

    def __init__(self, key, title, led):
        self.key, self.title, self.led = key, title, PHONE_LED.get(key, led)
        self.hub = None
        self.target = (0, 0)   # (port 1, port 2) power this hub should have now
        self.status = "Starting…"

    def label(self):
        hub = self.hub   # may drop to None on the Bluetooth thread at any moment
        return f"{hub.name} …{hub.address[-5:]}" if hub and hub.address else ""

    def ask(self):
        """What to press to connect this hub."""
        if self.key == "car":
            return "Press the green button on the hub"
        return f"Press the green button on the {self.title.split()[0].upper()} hub (its light turns {self.led})"


class Controller:
    """Owns the hub connections; turns phone button states into motor power."""

    def __init__(self, mode):
        self.lock = threading.Lock()
        self.loop = None
        self.mode = mode
        self.mode_changed = None    # asyncio.Event, set when the phone switches mode
        self.slots = {key: Slot(key, title, led)
                      for slots in rc.MODES.values() for key, title, led in slots}
        self.held = set()
        self.test = None            # calibration test running: {slot: (port 1, port 2) power}
        self.speed = DEFAULT_SPEED
        self.wheels = (0, 0)        # (left, right) wheel power, for the page's meters
        self.last_command = 0.0
        self.cal_mtime = None
        self._reload_calibration()

    def _active(self):
        """The hub slots the current mode drives."""
        return [self.slots[key] for key, _, _ in rc.MODES[self.mode]]

    # ----- driving (called from the web server threads) -----
    def _reload_calibration(self):
        """Pick up a new calibration saved by rc_car.pyw while this runs."""
        try:
            mtime = os.path.getmtime(rc.CAL_FILE)
        except OSError:
            mtime = None
        if mtime != self.cal_mtime or self.cal_mtime is None:
            self.cal, self.calibrated = rc.load_calibration()
            self.truck, self.truck_saved = rc.load_truck()
            self.cal_mtime = mtime

    def drive(self, held, speed=None):
        with self.lock:
            if isinstance(speed, (int, float)):
                self.speed = int(max(rc.MIN_POWER, min(100, speed)))
            self.held = {d for d in held if d in DIRECTIONS}
            self.test = None
            self.last_command = time.monotonic()
            self._update()

    def stop(self):
        with self.lock:
            self._halt()

    def _halt(self):
        """Stop driving and testing (call with the lock held)."""
        self.held = set()
        self.test = None
        self._update()

    def _update(self):
        self._reload_calibration()
        active = self._active()
        if self.test is not None:   # calibrating: raw test power, no steering or calibration
            self.wheels = (0, 0)
            for slot in self.slots.values():
                slot.target = self.test.get(slot.key, (0, 0)) if slot in active else (0, 0)
            return
        # Dual mode drives only with both hubs, so the truck never drags a dead axle
        ready = all(slot.hub for slot in active)
        self.wheels = rc.wheel_powers(self.held, self.speed) if ready else (0, 0)
        for slot in self.slots.values():
            if slot not in active:
                slot.target = (0, 0)
            elif self.mode == "dual":
                slot.target = rc.truck_port_powers(*self.wheels, self.truck[slot.key])
            else:
                slot.target = rc.port_powers(*self.wheels, self.cal)

    def set_mode(self, mode):
        """Switch single/dual: lets the current hubs go and starts connecting the new mode's."""
        with self.lock:
            if mode not in rc.MODES or mode == self.mode:
                return
            self.mode = mode
            self._halt()
        try:
            rc.write_settings(mode=mode)   # rc_car.pyw opens in the same mode
        except OSError:
            pass
        loop, changed = self.loop, self.mode_changed
        if loop and changed:
            loop.call_soon_threadsafe(changed.set)

    def _safety(self):
        """Stop the car if the phone that's driving goes quiet (Wi-Fi drop, app switch...)."""
        while True:
            time.sleep(0.05)
            if (self.held or self.test) and time.monotonic() - self.last_command > PHONE_TIMEOUT:
                self.stop()

    # ----- calibrating from the phone (same steps as rc_car.pyw's windows) -----
    def cal_test(self, data):
        """Run one calibration test while the phone keeps re-sending it:
        single {"kind": "arrow", "arrow": "fwd"}: that arrow's raw pattern on the hub;
        dual {"kind": "port", "port": ["front", 1]}: that one motor forward;
        dual {"kind": "check", "held": [...], "truck": {...}}: drive with unsaved truck settings."""
        with self.lock:
            self.held = set()
            self.last_command = time.monotonic()
            power = rc.motor_power(self.speed)
            kind, test = data.get("kind"), {}
            if kind == "arrow" and self.mode == "single" and data.get("arrow") in rc.TEST_PATTERNS:
                a, b = rc.TEST_PATTERNS[data["arrow"]]
                test["car"] = (a * power, b * power)
            elif kind == "port" and self.mode == "dual":
                port = tuple(data.get("port") or ())
                if port in rc.TRUCK_PORTS:
                    target = [0, 0]
                    target[port[1] - 1] = power
                    test[port[0]] = tuple(target)
            elif kind == "check" and self.mode == "dual":
                truck = parse_truck(data.get("truck"))
                held = {d for d in data.get("held", []) if d in DIRECTIONS}
                if truck and all(slot.hub for slot in self._active()):
                    left, right = rc.wheel_powers(held, self.speed)
                    test = {key: rc.truck_port_powers(left, right, truck[key]) for key in ("front", "rear")}
            self.test = test or None
            self._update()

    def save_calibration(self, data):
        """Save a calibration made on the phone. Returns an error message, or "" if saved."""
        try:
            if data.get("mode") == "dual":
                truck = parse_truck(data.get("truck"))
                if not truck:
                    return "Pick a wheel and a direction for every motor (each wheel once)."
                rc.save_truck(truck)
            else:
                cal, reason = rc.cal_from_choice(data.get("choice"))
                if not cal:
                    return reason
                rc.save_calibration(cal)
        except OSError as e:
            return f"Couldn't save the calibration file: {e.strerror or e}"
        with self.lock:
            self.cal_mtime = -1   # re-read now
            self._halt()
        return ""

    def _connected(self):
        return [slot.hub for slot in self._active() if slot.hub]

    def horn(self):
        hubs = self._connected()
        if hubs and self.loop:   # one hub is enough (two would beep out of step)
            asyncio.run_coroutine_threadsafe(hubs[0].beep(392, 250), self.loop)

    def light(self, name):
        for hub in self._connected():
            asyncio.run_coroutine_threadsafe(hub.led(name), self.loop)

    def state(self):
        with self.lock:
            self._reload_calibration()
        hubs = []
        for slot in self._active():
            hub = slot.hub
            motors = list(dict(hub.ports).values()).count(rc.MOTOR) if hub else 0
            hubs.append({"title": slot.title, "connected": hub is not None,
                         "status": f"Connected · {slot.label()}" if hub else slot.status,
                         "warning": "" if not hub or motors == 2 else "plug a motor into both ports"})
        ready = all(h["connected"] for h in hubs)
        return {
            "connected": ready,
            "mode": self.mode,
            "hubs": hubs,
            "status": next((h["status"] for h in hubs if not h["connected"]), "Connected"),
            "left": self.wheels[0], "right": self.wheels[1],
            "speed": self.speed, "min_speed": rc.MIN_POWER,
            "lights": [{"name": n, "color": c} for n, c in rc.LIGHTS],
            "calibrated": self.truck_saved if self.mode == "dual" else self.calibrated,
            "calibration": {   # the current settings, to pre-fill the phone's Calibrate screen
                "choice": rc.predict_choice(self.cal),
                "truck": {key: [list(p) for p in self.truck[key]] for key in ("front", "rear")},
                "test_ms": rc.TEST_MIN_MS,
            },
            "warning": "  ·  ".join(f"{h['title']}: {h['warning']}" for h in hubs if h["warning"]),
        }

    # ----- Bluetooth (runs on the main thread's asyncio loop) -----
    async def run(self):
        self.loop = asyncio.get_running_loop()
        threading.Thread(target=self._safety, daemon=True).start()
        while True:
            self.mode_changed = asyncio.Event()
            active = self._active()
            tasks = [asyncio.create_task(self._keep_connected(slot, active[:i]))
                     for i, slot in enumerate(active)]
            try:
                await self.mode_changed.wait()
            finally:   # mode switch, or Ctrl+C: stop the motors and let the hubs go
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    async def _keep_connected(self, slot, before):
        """Keep one hub connected and fed with its target power. A hub waits until the
        slots `before` it are connected, so in dual mode the first green button pressed
        is always the front hub's."""
        while True:
            if any(s.hub is None for s in before):
                slot.status = f"Waiting for the {before[-1].title.lower()} first"
                await asyncio.sleep(0.2)
                continue
            slot.status = slot.ask()
            hub = rc.WeDoHub()
            taken = [s.hub.address for s in self.slots.values() if s.hub and s.hub.address]
            try:
                await hub.connect(timeout=20, exclude=taken)
            except asyncio.CancelledError:
                await self._let_go(hub)
                raise
            except RuntimeError:   # scan timed out: keep looking
                continue
            except Exception as e:   # Bluetooth off, adapter busy, ...
                slot.status = f"Bluetooth problem: {e}"
                print(slot.status)
                await asyncio.sleep(2)
                continue
            slot.hub = hub
            print(f"{slot.title} connected ({slot.label()})")
            self.stop()   # dual mode may be ready to drive now
            try:
                await hub.led(slot.led)
                sent = (None, None)
                while hub.client.is_connected:
                    target = slot.target
                    if target[0] != sent[0]:
                        await hub.motor(1, target[0])
                    if target[1] != sent[1]:
                        await hub.motor(2, target[1])
                    sent = target
                    await asyncio.sleep(0.02)
                print(f"{slot.title} disconnected: press its green button to reconnect")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"{slot.title} connection lost: {e}")
            finally:
                slot.hub = None
                self.stop()   # in dual mode this stops the other hub too
                await self._let_go(hub)

    @staticmethod
    async def _let_go(hub):
        try:
            await asyncio.wait_for(hub.disconnect(), 3)
        except Exception:
            pass


def parse_truck(data):
    """A truck calibration sent by the phone ({"front": [[wheel, sign], ...], "rear": ...}),
    or None if it isn't complete and valid."""
    try:
        truck = {key: tuple((wheel, int(sign)) for wheel, sign in data[key]) for key in ("front", "rear")}
    except (KeyError, TypeError, ValueError):
        return None
    return truck if rc.valid_truck(truck) else None


def mode_from_args():
    """--dual / --single on the command line, else the mode rc_car.pyw used last."""
    if "--dual" in sys.argv:
        return "dual"
    if "--single" in sys.argv:
        return "single"
    saved = rc.read_settings().get("mode")
    return saved if saved in rc.MODES else "single"



controller = Controller(mode_from_args())


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows, address reuse lets a second copy take a port that's already in use
    # (and phones then reach either one), so there a busy port must fail instead.
    allow_reuse_address = sys.platform != "win32"

    def handle_error(self, request, client_address):
        # A phone closing the tab or switching apps mid-request is normal; don't print tracebacks.
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    def _send(self, body, content_type):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj):
        self._send(json.dumps(obj).encode(), "application/json")

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            with open(PAGE, "rb") as f:   # read each time so edits show up on refresh
                self._send(f.read(), "text/html; charset=utf-8")
        elif self.path == "/state":
            self._json(controller.state())
        else:
            self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self.send_error(400, "bad json")
        if self.path == "/drive":
            controller.drive(data.get("held", []), data.get("speed"))
        elif self.path == "/stop":
            controller.stop()
        elif self.path == "/horn":
            controller.horn()
        elif self.path == "/light":
            if data.get("name") in LIGHTS:
                controller.light(data["name"])
        elif self.path == "/mode":
            controller.set_mode(data.get("mode"))
        elif self.path == "/cal/test":
            controller.cal_test(data)
        elif self.path == "/cal/save":
            error = controller.save_calibration(data)
            return self._json({**controller.state(), "saved": not error, "error": error})
        else:
            return self.send_error(404)
        self._json(controller.state())

    def log_message(self, *args):   # keep the console quiet (10 requests a second)
        pass


def lan_addresses():
    """This PC's addresses a phone on the same network could reach."""
    found = []
    try:   # the interface the default route uses (no packets are sent)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            found.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        infos = []
    for info in infos:
        ip = info[4][0]
        if ip not in found and not ip.startswith("127."):
            found.append(ip)
    return found


def port_from_args():
    """The number after --port on the command line, or None."""
    args = sys.argv[1:]
    if "--port" in args:
        i = args.index("--port")
        if i + 1 < len(args) and args[i + 1].isdigit():
            return int(args[i + 1])
        sys.exit("Use:  python rc_car_phone.py --port 8090")
    return None


def start_server(wanted=None):
    """The web server on the `wanted` port, or on a random free port in PORT_RANGE.
    Trying to open the server is the check, so no other program can take the port
    between checking and using it."""
    if wanted:
        try:
            return Server(("0.0.0.0", wanted), Handler)
        except OSError as e:
            sys.exit(f"Can't use port {wanted} ({e.strerror or e}).\n"
                     f"Something else is using it: pick another, or leave out --port for a random free one.")
    ports = list(range(PORT_RANGE[0], PORT_RANGE[1] + 1))
    random.shuffle(ports)
    for port in ports:
        try:
            return Server(("0.0.0.0", port), Handler)
        except OSError:
            continue   # in use: try another
    sys.exit(f"No free port between {PORT_RANGE[0]} and {PORT_RANGE[1]}.")


def main():
    server = start_server(port_from_args())
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    dual = controller.mode == "dual"
    print(f"\nWeDo RC phone controller ({'dual: 4x4 truck with 2 hubs' if dual else 'single hub'})")
    for ip in lan_addresses():
        print(f"  open on your phone:  http://{ip}:{port}")
    print("  (phone must be on the same Wi-Fi; allow Python through the firewall if asked)")
    if dual:
        print("  Press the FRONT hub's green button first (light turns white), then the REAR hub's (red).")
    else:
        print("  Press the green button on the hub to connect.")
    print("  Switch single/dual on the phone page.  Ctrl+C to quit.")
    try:
        # Bluetooth on the main thread: bleak is happiest there on Windows
        asyncio.run(controller.run())
    except KeyboardInterrupt:
        pass
    finally:
        print("stopping…")
        server.shutdown()


if __name__ == "__main__":
    main()
